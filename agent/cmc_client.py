"""
NarrativePilot — CoinMarketCap Agent Hub client (MCP).

Talks to the CMC Agent Hub over MCP (JSON-RPC via HTTP POST, method tools/call).
The Hub is stateless here: no `initialize` handshake and no session id are
needed, and the same CMC API key authenticates via the X-CMC-MCP-API-KEY header.

The double-JSON quirk (verified live): the JSON-RPC envelope's
    result.content[0].text
is itself a JSON *string* that must be parsed a second time.

Tools used (all on the free tier):
    trending_crypto_narratives          -> dynamic narrative ranking + top coins
    get_global_metrics_latest           -> fear&greed, altseason, BTC dominance
    get_global_crypto_derivatives_metrics -> funding rate, liquidations
    get_crypto_quotes_latest            -> absolute USD prices (requires CMC id)
    search_cryptos                      -> resolve symbol -> CMC id
"""

import asyncio
import json
import re
from typing import Optional

import httpx

DEFAULT_MCP_URL = "https://mcp.coinmarketcap.com/mcp"

# Legacy alias kept so `from cmc_client import NARRATIVES` in trader.py keeps
# working. Narratives are now dynamic (from the Hub); this is intentionally
# empty and unused (trader's TOKEN_NARRATIVE derived from it is dead code).
NARRATIVES: dict[str, list[str]] = {}

# Tokens with liquidity on PancakeSwap / BSC — the tradeable universe for the
# token selector and for price lookups. Maps symbol -> CMC id.
# MUST stay in sync with executor.TOKEN_ADDRESSES (verified live, June 2026):
# these 4 are the only symbols with active PancakeSwap V2 pairs.
BSC_LIQUID_IDS: dict[str, int] = {
    "BNB": 1839,
    "CAKE": 7186,
    "ETH": 1027,
    "SOL": 5426,
}


# ----------------------------------------------------------------------
# Parsing helpers
# ----------------------------------------------------------------------

def parse_pct(s) -> float:
    """'+4.68%' -> 4.68, '-0.7%' -> -0.7, '' / None -> 0.0."""
    if s is None:
        return 0.0
    if isinstance(s, (int, float)):
        return float(s)
    s = s.strip().replace("%", "").replace("+", "")
    try:
        return float(s)
    except ValueError:
        return 0.0


_MULT = {"T": 1e12, "B": 1e9, "M": 1e6, "K": 1e3}


def parse_money(s) -> float:
    """'2.18 T' -> 2.18e12, '59.74 B' -> 5.974e10, plain number -> float."""
    if s is None:
        return 0.0
    if isinstance(s, (int, float)):
        return float(s)
    s = s.strip().replace("$", "").replace(",", "")
    m = re.match(r"^([0-9.]+)\s*([TBMK])?$", s, re.IGNORECASE)
    if not m:
        try:
            return float(s)
        except ValueError:
            return 0.0
    num = float(m.group(1))
    suffix = (m.group(2) or "").upper()
    return num * _MULT.get(suffix, 1.0)


def zip_rows(obj: dict) -> list[dict]:
    """{'headers':[...], 'rows':[[...],...]} -> [ {header: value}, ... ]."""
    if not isinstance(obj, dict):
        return []
    headers = obj.get("headers", [])
    return [dict(zip(headers, row)) for row in obj.get("rows", [])]


# ----------------------------------------------------------------------
# Client
# ----------------------------------------------------------------------

class CMCClient:
    def __init__(self, api_key: str, mcp_url: str = DEFAULT_MCP_URL):
        self.mcp_url = mcp_url
        self._headers = {
            "X-CMC-MCP-API-KEY": api_key,
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        self._id_cache: dict[str, int] = dict(BSC_LIQUID_IDS)

    # --- low-level MCP call -------------------------------------------

    async def _call(self, tool: str, arguments: Optional[dict] = None) -> object:
        """tools/call -> returns the inner (double-parsed) payload."""
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": tool, "arguments": arguments or {}},
        }
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                self.mcp_url, headers=self._headers, json=payload, timeout=60
            )
        resp.raise_for_status()

        envelope = self._decode_envelope(resp)
        if "error" in envelope:
            raise RuntimeError(f"MCP error for {tool}: {envelope['error']}")

        text = envelope["result"]["content"][0]["text"]
        if isinstance(text, str) and text.lstrip().lower().startswith("error:"):
            raise RuntimeError(f"{tool} returned: {text.strip()}")

        # Second parse (the double-JSON). search_cryptos already returns a list.
        if isinstance(text, str):
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                return text
        return text

    @staticmethod
    def _decode_envelope(resp: httpx.Response) -> dict:
        """Handle plain JSON or (defensively) an SSE-framed JSON-RPC reply."""
        ctype = resp.headers.get("content-type", "")
        if "text/event-stream" in ctype:
            for line in resp.text.splitlines():
                line = line.strip()
                if line.startswith("data:"):
                    chunk = line[5:].strip()
                    if chunk and chunk != "[DONE]":
                        try:
                            return json.loads(chunk)
                        except json.JSONDecodeError:
                            continue
            raise RuntimeError("Could not decode SSE MCP response")
        return resp.json()

    # --- Signal 1: dynamic narratives ---------------------------------

    async def get_trending_narratives(self) -> list[dict]:
        data = await self._call("trending_crypto_narratives")
        rows = zip_rows(data.get("categoryList", {})) if isinstance(data, dict) else []

        narratives: list[dict] = []
        seen: set[str] = set()
        for r in rows:
            slug = r.get("slug")
            if slug in seen:
                continue
            seen.add(slug)

            top_coins = []
            for c in zip_rows(r.get("topCoinList", {})):
                top_coins.append({
                    "symbol": c.get("coinSymbol", ""),
                    "name": c.get("coinName", ""),
                    "price_change_7d": parse_pct(c.get("priceChangePercent7d")),
                })

            narratives.append({
                "name": r.get("categoryName", slug or "?"),
                "slug": slug,
                "rank": r.get("trendingRank"),
                "market_cap_usd": parse_money(r.get("marketCapUsd")),
                "mcap_change_24h": parse_pct(r.get("marketCapChangePercentage24h")),
                "mcap_change_7d": parse_pct(r.get("marketCapChangePercentage7d")),
                "mcap_change_30d": parse_pct(r.get("marketCapChangePercentage30d")),
                "volume_change_24h": parse_pct(r.get("volumeChangePercentage24h")),
                "vw_perf_24h": parse_pct(r.get("volumeWeightedPricePerfVsCryptoMarketCap24h")),
                "vw_perf_7d": parse_pct(r.get("volumeWeightedPricePerfVsCryptoMarketCap7d")),
                "vw_perf_30d": parse_pct(r.get("volumeWeightedPricePerfVsCryptoMarketCap30d")),
                "social_authors": int(r.get("socialKeywordUniqueAuthorCount") or 0),
                "top_coins": top_coins,
            })
        return narratives

    # --- Signal 2: market regime --------------------------------------

    async def get_global_metrics(self) -> dict:
        d = await self._call("get_global_metrics_latest")
        sent = d.get("sentiment", {}).get("fear_greed", {}).get("current", {})
        alt = d.get("rotation", {}).get("altcoin_season", {}).get("current", {})
        btc_dom = d.get("dominance", {}).get("btc", {}).get("current")
        funding = d.get("leverage", {}).get("funding_rate", {}).get("average", {})
        return {
            "fear_greed": int(sent.get("index") or 50),
            "fear_greed_label": sent.get("value") or "",
            "altseason": int(alt.get("index") or 50),
            "btc_dominance": parse_pct(btc_dom),
            "funding_avg": parse_pct(funding.get("current")),
        }

    async def get_derivatives(self) -> dict:
        d = await self._call("get_global_crypto_derivatives_metrics")
        liq = d.get("btc_liquidations", {}).get("total_usd_24h", {})
        return {
            "funding_rate": parse_pct(d.get("fundingRate", {}).get("current")),
            "oi_change_24h": parse_pct(
                d.get("totalOpenInterest", {}).get("percentage_change_24h")
            ),
            "liquidations_24h": parse_money(liq.get("total")),
        }

    # --- Prices (requires CMC ids) ------------------------------------

    async def _resolve_id(self, symbol: str) -> Optional[int]:
        if symbol in self._id_cache:
            return self._id_cache[symbol]
        try:
            res = await self._call("search_cryptos", {"query": symbol})
        except RuntimeError:
            return None
        if isinstance(res, list):
            for item in res:
                if str(item.get("symbol", "")).upper() == symbol.upper():
                    cid = int(item["id"])
                    self._id_cache[symbol] = cid
                    return cid
        return None

    async def get_prices(self, symbols) -> dict[str, float]:
        """{symbol: usd_price} for the given symbols via get_crypto_quotes_latest."""
        symbols = [s for s in dict.fromkeys(symbols) if s]
        if not symbols:
            return {}

        ids: dict[int, str] = {}
        for sym in symbols:
            cid = await self._resolve_id(sym)
            if cid is not None:
                ids[cid] = sym
        if not ids:
            return {}

        data = await self._call(
            "get_crypto_quotes_latest", {"id": ",".join(str(i) for i in ids)}
        )
        prices: dict[str, float] = {}
        for row in zip_rows(data if isinstance(data, dict) else {}):
            sym = row.get("symbol")
            price = row.get("price")
            if sym and price is not None:
                prices[sym] = float(price)
        return prices

    # --- batch all per-cycle signals ----------------------------------

    async def fetch_all_signals(self) -> dict:
        narratives, regime, derivs = await asyncio.gather(
            self.get_trending_narratives(),
            self.get_global_metrics(),
            self.get_derivatives(),
        )
        regime = {**regime, **derivs}
        return {"narratives": narratives, "regime": regime}


# ----------------------------------------------------------------------
# Smoke test (live): python agent/cmc_client.py
# ----------------------------------------------------------------------

if __name__ == "__main__":
    import os
    from dotenv import load_dotenv

    load_dotenv()
    api_key = os.getenv("CMC_MCP_API_KEY") or os.getenv("CMC_API_KEY", "")
    if not api_key:
        raise SystemExit("CMC_API_KEY not set in .env")

    client = CMCClient(api_key, os.getenv("CMC_MCP_URL", DEFAULT_MCP_URL))

    async def _smoke():
        sig = await client.fetch_all_signals()

        print("Trending narratives (deduped):")
        for n in sig["narratives"]:
            coins = ", ".join(c["symbol"] for c in n["top_coins"][:5])
            print(f"  #{n['rank']} {n['name'][:30]:30s} "
                  f"vwPerf7d={n['vw_perf_7d']:+6.2f}%  vol24h={n['volume_change_24h']:+7.2f}%  "
                  f"social={n['social_authors']:>4}  top: {coins}")

        r = sig["regime"]
        print("\nMarket regime:")
        print(f"  fear&greed={r['fear_greed']} ({r['fear_greed_label']})  "
              f"altseason={r['altseason']}  btc_dom={r['btc_dominance']:.2f}%")
        print(f"  funding={r['funding_rate']:.5f}%  oi_chg_24h={r['oi_change_24h']:+.2f}%  "
              f"liq_24h=${r['liquidations_24h']:,.0f}")

        # price lookup for a couple of BSC-liquid tokens
        prices = await client.get_prices(["BNB", "CAKE"])
        print("\nPrices (BSC-liquid):", {k: round(v, 4) for k, v in prices.items()})

    asyncio.run(_smoke())
