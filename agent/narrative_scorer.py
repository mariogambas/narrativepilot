"""
NarrativePilot — narrative scoring engine (CMC Agent Hub edition).

Scores each narrative returned by the Hub 0-100 from its real metrics, then
dampens every score by a market-regime risk factor. Decision thresholds 70/40
are preserved (the trader consumes the scores unchanged).

Weights:
    rotation (vwPerf)   35%   -- narrative outperforming the market (lead alpha)
    momentum (mcap)     25%   -- multi-timeframe market-cap change
    volume change       20%   -- 24h volume surge
    social breadth      20%   -- unique social authors (relative across narratives)

Regime filter (lives here because trader.py is untouched): a factor in
[0.6, 1.0] from fear&greed + funding + liquidations multiplies all scores, so
dangerous regimes pull narratives below the 70 entry threshold automatically.
"""

import statistics
from typing import Optional

from cmc_client import BSC_LIQUID_IDS

# ----------------------------------------------------------------------
# Weights and tuning constants
# ----------------------------------------------------------------------

WEIGHT_ROTATION = 0.35
WEIGHT_MOMENTUM = 0.25
WEIGHT_VOLUME = 0.20
WEIGHT_SOCIAL = 0.20

# Full-scale points: a move of this size maps to the 0 or 100 extreme.
ROTATION_FULL_SCALE = 5.0    # ±5%  vwPerf blend -> ±50 pts around 50
MOMENTUM_FULL_SCALE = 10.0   # ±10% mcap blend  -> ±50 pts around 50
VOLUME_FULL_SCALE = 25.0     # ±25% vol change  -> ±50 pts around 50

REGIME_FLOOR = 0.6           # scores never dampened below 60% of raw


# ----------------------------------------------------------------------
# Pure normalization helpers (module-level for easy unit testing)
# ----------------------------------------------------------------------

def _centered(value: float, full_scale: float) -> float:
    """Map a signed % onto 0-100 centred at 50; ±full_scale -> 0/100."""
    return max(0.0, min(100.0, 50.0 + (value / full_scale) * 50.0))


def rotation_score(blend_pct: float) -> float:
    return _centered(blend_pct, ROTATION_FULL_SCALE)


def momentum_score(blend_pct: float) -> float:
    return _centered(blend_pct, MOMENTUM_FULL_SCALE)


def volume_score(pct: float) -> float:
    return _centered(pct, VOLUME_FULL_SCALE)


def social_score(authors: int, lo: int, hi: int) -> float:
    """Min-max normalize author count across the batch of narratives."""
    if hi <= lo:
        return 50.0
    return max(0.0, min(100.0, (authors - lo) / (hi - lo) * 100.0))


def compute_regime_factor(regime: dict) -> tuple[float, dict]:
    """
    Market-wide risk multiplier in [REGIME_FLOOR, 1.0]. Extremes in fear&greed,
    funding, or recent liquidations reduce risk appetite (fewer entries).
    """
    fg = regime.get("fear_greed", 50)
    funding = abs(regime.get("funding_rate", 0.0))
    liq = regime.get("liquidations_24h", 0.0)

    if fg <= 20 or fg >= 80:
        fg_factor = 0.75          # extreme fear (don't chase) / extreme greed (blow-off)
    elif fg <= 30 or fg >= 70:
        fg_factor = 0.90
    else:
        fg_factor = 1.00

    if funding > 0.10:
        funding_factor = 0.80     # crowded leverage, flush risk
    elif funding > 0.03:
        funding_factor = 0.90
    else:
        funding_factor = 1.00

    if liq > 1_000_000_000:
        liq_factor = 0.70         # >$1B liquidated in 24h = high volatility
    elif liq > 500_000_000:
        liq_factor = 0.85
    else:
        liq_factor = 1.00

    factor = max(REGIME_FLOOR, fg_factor * funding_factor * liq_factor)
    return factor, {
        "fg_factor": fg_factor,
        "funding_factor": funding_factor,
        "liq_factor": liq_factor,
    }


def select_bsc_token(top_coins: list[dict]) -> Optional[str]:
    """
    Pick the strongest BSC-liquid coin from a narrative's top coins.
    Priority: BNB first; otherwise the BSC-liquid coin with the best 7d move.
    Returns None if none of the top coins trade on BSC.
    """
    candidates = [c for c in top_coins if c.get("symbol") in BSC_LIQUID_IDS]
    if not candidates:
        return None
    for c in candidates:
        if c.get("symbol") == "BNB":
            return "BNB"
    best = max(candidates, key=lambda c: c.get("price_change_7d", 0.0))
    return best.get("symbol")


# ----------------------------------------------------------------------
# Scorer
# ----------------------------------------------------------------------

class NarrativeScorer:
    def update_and_score(self, signals: dict) -> dict:
        narratives = signals.get("narratives", [])
        regime = signals.get("regime", {})

        regime_factor, regime_breakdown = compute_regime_factor(regime)

        # Social breadth is relative across the batch.
        author_counts = [n.get("social_authors", 0) for n in narratives] or [0]
        lo, hi = min(author_counts), max(author_counts)

        narrative_scores: dict[str, float] = {}
        breakdown: dict[str, dict] = {}
        best_tokens: dict[str, Optional[str]] = {}

        for n in narratives:
            name = n["name"]

            rot_blend = 0.5 * n["vw_perf_24h"] + 0.3 * n["vw_perf_7d"] + 0.2 * n["vw_perf_30d"]
            mom_blend = 0.5 * n["mcap_change_24h"] + 0.3 * n["mcap_change_7d"] + 0.2 * n["mcap_change_30d"]

            s_rot = rotation_score(rot_blend)
            s_mom = momentum_score(mom_blend)
            s_vol = volume_score(n["volume_change_24h"])
            s_soc = social_score(n.get("social_authors", 0), lo, hi)

            raw = (
                WEIGHT_ROTATION * s_rot
                + WEIGHT_MOMENTUM * s_mom
                + WEIGHT_VOLUME * s_vol
                + WEIGHT_SOCIAL * s_soc
            )
            final = raw * regime_factor

            narrative_scores[name] = round(final, 1)
            best_tokens[name] = select_bsc_token(n.get("top_coins", []))
            breakdown[name] = {
                "vw_perf_pct": round(rot_blend, 2),
                "momentum_pct": round(mom_blend, 2),
                "volume_change_pct": round(n["volume_change_24h"], 2),
                "social_authors": n.get("social_authors", 0),
                "sub_scores": {
                    "rotation": round(s_rot, 1),
                    "momentum": round(s_mom, 1),
                    "volume": round(s_vol, 1),
                    "social": round(s_soc, 1),
                },
                "regime_factor": round(regime_factor, 3),
                "raw_score": round(raw, 1),
                "weighted_score": round(final, 1),
            }

        market_regime = {
            "fear_greed": regime.get("fear_greed"),
            "fear_greed_label": regime.get("fear_greed_label", ""),
            "altseason": regime.get("altseason"),
            "btc_dominance_pct": regime.get("btc_dominance"),
            "funding_rate_pct": regime.get("funding_rate"),
            "liquidations_24h": regime.get("liquidations_24h"),
            "regime_factor": round(regime_factor, 3),
            "regime_breakdown": regime_breakdown,
        }

        return {
            "narrative_scores": narrative_scores,
            "signal_breakdown": breakdown,
            "best_tokens": best_tokens,
            "market_regime": market_regime,
        }


# ----------------------------------------------------------------------
# Unit tests: python agent/narrative_scorer.py
# ----------------------------------------------------------------------

def _run_tests() -> None:
    passed = 0
    failed = 0

    def check(name: str, cond: bool, extra: str = "") -> None:
        nonlocal passed, failed
        if cond:
            passed += 1
            print(f"  PASS  {name}")
        else:
            failed += 1
            print(f"  FAIL  {name}  {extra}")

    print("\n[1] Normalization helpers")
    check("rotation 0 -> 50", rotation_score(0) == 50.0)
    check("rotation +5 -> 100", rotation_score(5) == 100.0)
    check("rotation -5 -> 0", rotation_score(-5) == 0.0)
    check("momentum 0 -> 50", momentum_score(0) == 50.0)
    check("momentum +10 -> 100", momentum_score(10) == 100.0)
    check("volume +25 -> 100", volume_score(25) == 100.0)
    check("volume 0 -> 50", volume_score(0) == 50.0)
    check("social min -> 0", social_score(10, 10, 100) == 0.0)
    check("social max -> 100", social_score(100, 10, 100) == 100.0)
    check("social mid -> 50", social_score(55, 10, 100) == 50.0)
    check("social equal batch -> 50", social_score(30, 30, 30) == 50.0)

    print("\n[2] Regime filter")
    neutral, _ = compute_regime_factor({"fear_greed": 50, "funding_rate": 0.0, "liquidations_24h": 0})
    fearf, _ = compute_regime_factor({"fear_greed": 16, "funding_rate": 0.0, "liquidations_24h": 0})
    liqf, _ = compute_regime_factor({"fear_greed": 50, "funding_rate": 0.0, "liquidations_24h": 2e9})
    floorf, _ = compute_regime_factor({"fear_greed": 16, "funding_rate": 0.5, "liquidations_24h": 2e9})
    check("neutral regime -> 1.0", neutral == 1.0)
    check("extreme fear -> 0.75", fearf == 0.75, extra=f"got {fearf}")
    check("big liquidations -> 0.70", liqf == 0.70, extra=f"got {liqf}")
    check("multiple extremes floored at 0.6", floorf == 0.6, extra=f"got {floorf}")

    print("\n[3] BSC token selector")
    coins_bnb = [{"symbol": "BTC", "price_change_7d": 2}, {"symbol": "BNB", "price_change_7d": -1},
                 {"symbol": "CAKE", "price_change_7d": 5}]
    coins_nobnb = [{"symbol": "BTC", "price_change_7d": 2}, {"symbol": "CAKE", "price_change_7d": 5},
                   {"symbol": "LINK", "price_change_7d": 9}]
    coins_none = [{"symbol": "BTC", "price_change_7d": 2}, {"symbol": "RENDER", "price_change_7d": 9}]
    check("BNB prioritized", select_bsc_token(coins_bnb) == "BNB")
    check("else best 7d BSC-liquid (LINK)", select_bsc_token(coins_nobnb) == "LINK",
          extra=f"got {select_bsc_token(coins_nobnb)}")
    check("none BSC-liquid -> None", select_bsc_token(coins_none) is None)

    print("\n[4] Full scoring with mocked Hub narratives")
    strong = {
        "name": "Binance Ecosystem", "slug": "binance-ecosystem", "rank": 1,
        "market_cap_usd": 2.18e12,
        "mcap_change_24h": 8.0, "mcap_change_7d": 10.0, "mcap_change_30d": 12.0,
        "volume_change_24h": 30.0,
        "vw_perf_24h": 6.0, "vw_perf_7d": 5.0, "vw_perf_30d": 4.0,
        "social_authors": 100,
        "top_coins": [{"symbol": "BTC", "price_change_7d": -2},
                      {"symbol": "BNB", "price_change_7d": 3},
                      {"symbol": "CAKE", "price_change_7d": 8}],
    }
    weak = {
        "name": "Quiet Cats", "slug": "quiet-cats", "rank": 2,
        "market_cap_usd": 1e8,
        "mcap_change_24h": -3.0, "mcap_change_7d": -2.0, "mcap_change_30d": -1.0,
        "volume_change_24h": -10.0,
        "vw_perf_24h": -2.0, "vw_perf_7d": -1.0, "vw_perf_30d": -1.0,
        "social_authors": 10,
        "top_coins": [{"symbol": "WIF", "price_change_7d": -5}],
    }

    scorer = NarrativeScorer()

    # neutral regime
    res = scorer.update_and_score({
        "narratives": [strong, weak],
        "regime": {"fear_greed": 50, "fear_greed_label": "Neutral",
                   "funding_rate": 0.0, "liquidations_24h": 0,
                   "altseason": 50, "btc_dominance": 55.0},
    })
    s_strong = res["narrative_scores"]["Binance Ecosystem"]
    s_weak = res["narrative_scores"]["Quiet Cats"]
    print(f"\n    strong = {s_strong}   weak = {s_weak}")
    print(f"    strong breakdown: {res['signal_breakdown']['Binance Ecosystem']}")
    print(f"    best_tokens: {res['best_tokens']}")
    print(f"    market_regime: {res['market_regime']['regime_factor']} "
          f"(fg {res['market_regime']['fear_greed']})")

    check("strong narrative > 70 (entry)", s_strong > 70, extra=f"got {s_strong}")
    check("weak narrative < 40 (exit)", s_weak < 40, extra=f"got {s_weak}")
    check("selector picks BNB for strong", res["best_tokens"]["Binance Ecosystem"] == "BNB")
    check("weak narrative token None (WIF not BSC-liquid)",
          res["best_tokens"]["Quiet Cats"] is None)
    check("regime_factor 1.0 in neutral", res["market_regime"]["regime_factor"] == 1.0)

    # extreme-fear regime: same narratives, scores dampened
    res2 = scorer.update_and_score({
        "narratives": [strong, weak],
        "regime": {"fear_greed": 16, "fear_greed_label": "Extreme fear",
                   "funding_rate": 0.0, "liquidations_24h": 0,
                   "altseason": 46, "btc_dominance": 58.2},
    })
    s_strong2 = res2["narrative_scores"]["Binance Ecosystem"]
    print(f"\n    under extreme fear: strong = {s_strong2} (was {s_strong})")
    check("extreme fear dampens score", s_strong2 < s_strong, extra=f"{s_strong2} vs {s_strong}")
    check("dampened ~= raw * 0.75",
          abs(s_strong2 - res2["signal_breakdown"]["Binance Ecosystem"]["raw_score"] * 0.75) < 0.2)

    print(f"\n{'='*50}")
    print(f"  {passed} passed, {failed} failed")
    print(f"{'='*50}")
    if failed:
        raise SystemExit(1)


if __name__ == "__main__":
    _run_tests()
