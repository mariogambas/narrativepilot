"""
NarrativePilot — autonomous narrative-rotation trading agent.

Orchestrates the full pipeline on a fixed cadence:

    fetch CMC signals -> score narratives -> decide -> execute -> log

Run:
    python agent/main.py --mode testnet     # real signals, simulated trades
    python agent/main.py --mode mainnet     # real signals, real swaps

Speed up for a demo with SCAN_INTERVAL_MINUTES=1 in .env.
"""

import argparse
import asyncio
import json
import os
import signal
from datetime import datetime, timezone

import aiofiles
from dotenv import load_dotenv

from cmc_client import CMCClient
from narrative_scorer import NarrativeScorer
from trader import Trader, Portfolio

from executor import TradeExecutor

LOG_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "logs",
    "trades.log",
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


class Agent:
    def __init__(self, mode: str):
        load_dotenv()
        self.mode = mode

        api_key = os.getenv("CMC_API_KEY", "")
        if not api_key:
            raise SystemExit("CMC_API_KEY not set in .env")

        self.initial_capital = float(os.getenv("INITIAL_CAPITAL_USD", "100"))
        self.scan_minutes = float(os.getenv("SCAN_INTERVAL_MINUTES", "15"))

        self.cmc = CMCClient(api_key)
        self.scorer = NarrativeScorer()
        self.trader = Trader(
            max_position_pct=float(os.getenv("MAX_POSITION_PCT", "0.10")),
            stop_loss_pct=float(os.getenv("STOP_LOSS_PCT", "0.08")),
            max_drawdown_pct=float(os.getenv("MAX_DRAWDOWN_PCT", "0.20")),
        )
        self.executor = TradeExecutor(
            mode=mode,
            rpc_url=os.getenv("BSC_RPC_URL", ""),
            # private key is only read in mainnet; testnet passes it through but
            # the executor never touches it.
            private_key=os.getenv("WALLET_PRIVATE_KEY", "") if mode == "mainnet" else "",
            slippage=float(os.getenv("SLIPPAGE", "0.01")),
        )
        self.portfolio = Portfolio(initial_capital_usd=self.initial_capital)
        self.cycle = 0
        self._running = True

    # ------------------------------------------------------------------

    async def _write_log(self, record: dict) -> None:
        async with aiofiles.open(LOG_PATH, "a", encoding="utf-8") as f:
            await f.write(json.dumps(record) + "\n")

    def _base_record(self, scores: dict, value: float, prices: dict) -> dict:
        positions = []
        for sym, pos in self.portfolio.positions.items():
            cur = prices.get(sym, pos.entry_price)
            pnl = (cur - pos.entry_price) / pos.entry_price if pos.entry_price > 0 else 0.0
            positions.append({
                "symbol": sym,
                "narrative": pos.narrative,
                "entry_price": round(pos.entry_price, 8),
                "current_price": round(cur, 8),
                "qty": round(pos.qty, 8),
                "value_usd": round(pos.qty * cur, 2),
                "pnl_pct": round(pnl * 100, 2),
            })
        return {
            "timestamp": _utc_now(),
            "cycle": self.cycle,
            "mode": self.mode,
            "narrative_scores": scores["narrative_scores"],
            "signal_breakdown": scores["signal_breakdown"],
            "market_regime": scores.get("market_regime", {}),
            "portfolio_value_usd": round(value, 2),
            "open_positions": list(self.portfolio.positions.keys()),
            "positions": positions,
        }

    async def run_cycle(self) -> None:
        self.cycle += 1
        # 1) pull dynamic narratives + market regime from the Hub
        signals = await self.cmc.fetch_all_signals()
        # 2) score narratives and pick the strongest BSC-liquid token per narrative
        scores = self.scorer.update_and_score(signals)
        # 3) fetch USD prices only for the selected tokens + open positions
        needed = {t for t in scores["best_tokens"].values() if t}
        needed |= set(self.portfolio.positions.keys())
        prices = await self.cmc.get_prices(needed)
        # 4) decide (trader/risk logic unchanged)
        decisions = self.trader.decide(scores, prices, self.portfolio)

        executed = []
        for d in decisions:
            if d.action not in ("BUY", "SELL"):
                continue
            res = self.executor.execute(d)
            if res.success and not res.error:
                if d.action == "BUY" and res.qty > 0:
                    self.portfolio.apply_buy(d.token, d.narrative, res.amount_usd, res.fill_price)
                elif d.action == "SELL" and d.token in self.portfolio.positions:
                    self.portfolio.apply_sell(d.token, res.fill_price)
            executed.append((d, res))

        value = self.portfolio.total_value(prices)
        self.portfolio.update_peak(value)
        base = self._base_record(scores, value, prices)

        # One log line per executed trade; if nothing traded, one HOLD line.
        if executed:
            for d, res in executed:
                record = {
                    **base,
                    "action": d.action,
                    "token": d.token,
                    "amount_usd": round(res.amount_usd, 2),
                    "price": round(res.fill_price, 8),
                    "reason": d.reason,
                    "tx_hash": res.tx_hash,
                    "simulated": res.simulated,
                    "error": res.error,
                }
                await self._write_log(record)
        else:
            ns = scores["narrative_scores"]
            if ns:
                top = max(ns, key=ns.get)
                reason = f"No actionable signal. Top narrative {top} @ {ns[top]}."
            else:
                reason = "No narratives returned by the Hub this cycle."
            record = {
                **base,
                "action": "HOLD",
                "token": None,
                "amount_usd": 0.0,
                "price": 0.0,
                "reason": reason,
                "tx_hash": "",
                "simulated": (self.mode != "mainnet"),
                "error": None,
            }
            await self._write_log(record)

        self._print_cycle(scores, executed, value)

    def _print_cycle(self, scores: dict, executed: list, value: float) -> None:
        ns = scores["narrative_scores"]
        scoreline = "  ".join(f"{k}={v}" for k, v in ns.items())
        mr = scores.get("market_regime", {})
        print(f"\n[cycle {self.cycle}] {_utc_now()}  ({self.mode})")
        if mr:
            print(f"  regime: fear&greed={mr.get('fear_greed')} ({mr.get('fear_greed_label')})  "
                  f"factor={mr.get('regime_factor')}")
        print(f"  scores: {scoreline}")
        print(f"  portfolio: ${value:,.2f}   positions: {list(self.portfolio.positions)}")
        if not executed:
            print("  action: HOLD (no actionable signal)")
        for d, res in executed:
            status = "ok" if (res.success and not res.error) else f"FAILED ({res.error})"
            print(f"  action: {d.action} {d.token} ${res.amount_usd:.2f} -> {status}")

    async def run(self) -> None:
        interval = self.scan_minutes * 60
        print(f"NarrativePilot starting in {self.mode.upper()} mode.")
        print(f"Capital ${self.initial_capital:.2f} | scan every {self.scan_minutes:g} min "
              f"| log -> {LOG_PATH}")
        if self.mode == "testnet":
            print("Testnet: trades are SIMULATED. No funds at risk, no key needed.\n")

        while self._running:
            try:
                await self.run_cycle()
            except Exception as e:
                print(f"  [cycle {self.cycle}] error: {e}")
            if not self._running:
                break
            try:
                await asyncio.sleep(interval)
            except asyncio.CancelledError:
                break

        self._shutdown_summary()

    def stop(self) -> None:
        self._running = False

    def _shutdown_summary(self) -> None:
        value = self.portfolio.cash_usd + sum(
            p.cost_usd for p in self.portfolio.positions.values()
        )
        pnl = value - self.initial_capital
        print("\n" + "=" * 50)
        print("NarrativePilot stopped.")
        print(f"  cycles run:      {self.cycle}")
        print(f"  open positions:  {list(self.portfolio.positions)}")
        print(f"  cash:            ${self.portfolio.cash_usd:,.2f}")
        print(f"  est. PnL:        ${pnl:+,.2f} ({pnl/self.initial_capital*100:+.1f}%)")
        print("=" * 50)


async def _main() -> None:
    parser = argparse.ArgumentParser(description="NarrativePilot trading agent")
    parser.add_argument(
        "--mode",
        choices=["testnet", "mainnet"],
        default=os.getenv("TRADING_MODE", "testnet"),
        help="testnet = simulated trades (default), mainnet = real swaps",
    )
    args = parser.parse_args()

    agent = Agent(mode=args.mode)

    # Graceful Ctrl+C / SIGTERM.
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, agent.stop)
        except NotImplementedError:
            # Windows: add_signal_handler is unsupported — KeyboardInterrupt
            # from asyncio.run handles Ctrl+C instead.
            pass

    await agent.run()


if __name__ == "__main__":
    try:
        asyncio.run(_main())
    except KeyboardInterrupt:
        print("\nInterrupted.")
