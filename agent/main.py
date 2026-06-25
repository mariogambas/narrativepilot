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
import sys
import time
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
# Create logs/ on import so a fresh clone never fails on first write.
os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)


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
            slippage=float(os.getenv("SLIPPAGE", "3.0")),
        )
        self.portfolio = self._recover_portfolio()
        self.cycle = 0
        self._running = True
        self.last_trade_time: float = self._recover_last_trade_time()

    # ------------------------------------------------------------------

    def _recover_portfolio(self) -> Portfolio:
        """On startup, replay every BUY/SELL in trades.log so a restart
        doesn't reset open positions to empty (which would defeat
        anti-pyramiding and break PnL tracking). Falls back to a fresh
        Portfolio if no log exists yet.

        Deliberately ignores the `positions` snapshot field: trades.log is a
        single append-only file that can span schema changes across a long
        run (older lines stored `positions` as a plain list of symbol
        strings, not the current list-of-dicts shape), so trusting its shape
        is fragile. Only the flat, stable fields on each trade line are used:
        action, token, narrative, amount_usd, price. `narrative` falls back
        to "" for older lines written before that field existed.

        Replaying through apply_buy/apply_sell (instead of trusting any
        snapshot) also reconstructs cash_usd exactly as it was mutated live.
        Two consecutive BUYs for the same token with no SELL between them
        naturally collapse into a single position (apply_buy overwrites the
        dict entry), matching real trading semantics.
        """
        portfolio = Portfolio(initial_capital_usd=self.initial_capital)
        try:
            with open(LOG_PATH, "r", encoding="utf-8") as f:
                lines = f.readlines()
        except FileNotFoundError:
            return portfolio

        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue

            action = record.get("action")
            token = record.get("token")
            if not token or action not in ("BUY", "SELL"):
                continue
            # A failed execution (TWAK error, no liquidity, etc.) is still
            # logged for visibility, but nothing was actually bought/sold
            # on-chain — replaying it would fabricate a position or vanish
            # a real one. Skip anything that recorded an error.
            if record.get("error"):
                continue

            # Defensive: some lines are malformed or hand-edited (e.g. a
            # manually-inserted "recovery" entry with price/amount missing
            # or null). A real successful trade always has positive numeric
            # amount_usd and price — anything else can't be replayed safely,
            # so skip it loudly rather than crash or fabricate a 0-qty
            # position that would silently zero out the portfolio's value.
            try:
                amount_usd = float(record.get("amount_usd") or 0.0)
                price = float(record.get("price") or 0.0)
            except (TypeError, ValueError):
                print(f"  [WARN] skipping unreplayable log line for {token} "
                      f"(bad amount_usd/price): {line[:200]}", flush=True)
                continue
            if amount_usd <= 0 or price <= 0:
                print(f"  [WARN] skipping unreplayable log line for {token} "
                      f"(amount_usd={amount_usd}, price={price}): {line[:200]}", flush=True)
                continue

            if action == "BUY":
                portfolio.apply_buy(
                    token, record.get("narrative", ""), amount_usd, price,
                )
            elif action == "SELL" and token in portfolio.positions:
                portfolio.apply_sell(token, price)

        return portfolio

    def _recover_last_trade_time(self) -> float:
        """On startup, read trades.log to find the last BUY/SELL timestamp.

        Survives PC restarts: if the agent restarted but a real trade happened
        recently, we won't fire a spurious forced trade on the first cycle.
        Falls back to time.time() if no log exists yet.
        """
        try:
            with open(LOG_PATH, "r", encoding="utf-8") as f:
                lines = f.readlines()
            for line in reversed(lines):
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                    if record.get("action") in ("BUY", "SELL"):
                        ts_str = record.get("timestamp", "")
                        dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                        return dt.timestamp()
                except (json.JSONDecodeError, ValueError):
                    continue
        except FileNotFoundError:
            pass
        return time.time()

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
        hours_since_last_trade = (time.time() - self.last_trade_time) / 3600
        force_trade = hours_since_last_trade >= 23.0
        decisions = self.trader.decide(scores, prices, self.portfolio, force_trade=force_trade)

        executed = []
        for d in decisions:
            if d.action not in ("BUY", "SELL"):
                continue
            res = await self.executor.execute(d)
            if res.success and not res.error:
                if d.action == "BUY" and res.qty > 0:
                    self.portfolio.apply_buy(d.token, d.narrative, res.amount_usd, res.fill_price)
                elif d.action == "SELL" and d.token in self.portfolio.positions:
                    self.portfolio.apply_sell(d.token, res.fill_price)
                self.last_trade_time = time.time()
            executed.append((d, res))

        value = self.portfolio.total_value(prices)
        self.portfolio.update_peak(value)
        base = self._base_record(scores, value, prices)

        # Log warning decisions from forced trade path (HOLD with special reason)
        forced_warnings = [
            d for d in decisions
            if d.action == "HOLD" and "FORCED_DAILY_TRADE" in d.reason
        ]
        for d in forced_warnings:
            print(f"  [WARN] {d.reason}", flush=True)

        # One log line per executed trade; if nothing traded, one HOLD line.
        if executed:
            for d, res in executed:
                record = {
                    **base,
                    "action": d.action,
                    "token": d.token,
                    "narrative": d.narrative,
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

        self._print_cycle(scores, executed, value, hours_since_last_trade)

    def _print_cycle(self, scores: dict, executed: list, value: float, hours_since_trade: float = 0.0) -> None:
        ns = scores["narrative_scores"]
        scoreline = "  ".join(f"{k}={v}" for k, v in ns.items())
        mr = scores.get("market_regime", {})
        force_tag = "  [FORCE_TRADE_ACTIVE]" if hours_since_trade >= 23.0 else ""
        print(f"\n[cycle {self.cycle}] {_utc_now()}  ({self.mode}){force_tag}", flush=True)
        print(f"  last_trade: {hours_since_trade:.1f}h ago", flush=True)
        if mr:
            print(f"  regime: fear&greed={mr.get('fear_greed')} ({mr.get('fear_greed_label')})  "
                  f"factor={mr.get('regime_factor')}", flush=True)
        print(f"  scores: {scoreline}", flush=True)
        print(f"  portfolio: ${value:,.2f}   positions: {list(self.portfolio.positions)}", flush=True)
        if not executed:
            print("  action: HOLD (no actionable signal)", flush=True)
        for d, res in executed:
            status = "ok" if (res.success and not res.error) else f"FAILED ({res.error})"
            print(f"  action: {d.action} {d.token} ${res.amount_usd:.2f} -> {status}", flush=True)

    async def run(self) -> None:
        interval = self.scan_minutes * 60
        print(f"NarrativePilot starting in {self.mode.upper()} mode.", flush=True)
        print(f"Capital ${self.initial_capital:.2f} | scan every {self.scan_minutes:g} min "
              f"| log -> {LOG_PATH}", flush=True)
        if self.mode == "testnet":
            print("Testnet: trades are SIMULATED. No funds at risk, no key needed.\n", flush=True)

        while self._running:
            try:
                await self.run_cycle()
            except Exception as e:
                print(f"  [cycle {self.cycle}] error: {e}", flush=True)
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


def _run_tests() -> None:
    """python agent/main.py --test"""
    import tempfile

    global LOG_PATH

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

    os.environ.setdefault("CMC_API_KEY", "test_key_unused")

    def make_agent(records: list[dict]) -> "Agent":
        tmpdir = tempfile.mkdtemp()
        fake_log = os.path.join(tmpdir, "trades.log")
        with open(fake_log, "w", encoding="utf-8") as f:
            for rec in records:
                f.write(json.dumps(rec) + "\n")
        global LOG_PATH
        old_log_path = LOG_PATH
        LOG_PATH = fake_log
        try:
            return Agent(mode="testnet")
        finally:
            LOG_PATH = old_log_path

    print("\n[1] Two BUYs for the same token, no SELL -> single open position")
    agent1 = make_agent([
        {"action": "BUY", "token": "ETH", "narrative": "Ethereum Ecosystem",
         "amount_usd": 50.0, "price": 1800.0},
        {"action": "BUY", "token": "ETH", "narrative": "Layer 1",
         "amount_usd": 30.0, "price": 1850.0},
    ])
    check("ETH appears exactly once after two BUYs with no SELL",
          list(agent1.portfolio.positions.keys()) == ["ETH"],
          extra=f"got {list(agent1.portfolio.positions.keys())}")
    check("recovered position reflects the LATEST buy's entry price",
          abs(agent1.portfolio.positions["ETH"].entry_price - 1850.0) < 1e-9,
          extra=f"got {agent1.portfolio.positions['ETH'].entry_price}")
    check("narrative taken from that BUY's own `narrative` field (renamed narrative)",
          agent1.portfolio.positions["ETH"].narrative == "Layer 1",
          extra=f"got {agent1.portfolio.positions['ETH'].narrative}")

    print("\n[2] BUY followed by a later SELL -> position closed on recovery")
    agent2 = make_agent([
        {"action": "BUY", "token": "CAKE", "narrative": "Binance Ecosystem",
         "amount_usd": 10.0, "price": 2.5},
        {"action": "SELL", "token": "CAKE", "amount_usd": 12.0, "price": 3.0},
    ])
    check("CAKE not in positions after a later SELL",
          "CAKE" not in agent2.portfolio.positions,
          extra=f"got {list(agent2.portfolio.positions.keys())}")

    print("\n[3] Failed executions (TWAK errors) must not be replayed as real trades")
    agent_failed = make_agent([
        # Mirrors the real production incident: a BUY that failed because
        # twak wasn't found in PATH still gets logged for visibility, with
        # action=BUY/token=ETH but a non-null `error`. Replaying it would
        # fabricate an ETH position and wrongly drain cash_usd, producing a
        # phantom drawdown later (total_value would then miss the real
        # position value entirely once the position doesn't exist).
        {"action": "BUY", "token": "ETH", "narrative": "Ethereum Ecosystem",
         "amount_usd": 26.70, "price": 1800.0,
         "error": "twak rc=1: twak binary not found at /home/mario/.npm-global/bin/twak"},
        {"action": "BUY", "token": "UNI", "narrative": "DeFi",
         "amount_usd": 53.40, "price": 7.20,
         "error": "twak rc=1: twak binary not found at /home/mario/.npm-global/bin/twak"},
    ])
    check("failed BUYs are not replayed into positions",
          len(agent_failed.portfolio.positions) == 0,
          extra=f"got {list(agent_failed.portfolio.positions.keys())}")
    check("failed BUYs don't drain cash_usd on recovery",
          abs(agent_failed.portfolio.cash_usd - agent_failed.initial_capital) < 1e-9,
          extra=f"got {agent_failed.portfolio.cash_usd}")

    print("\n[3b] A successful BUY followed by a FAILED sell attempt keeps the position open")
    agent_failed_sell = make_agent([
        {"action": "BUY", "token": "ETH", "narrative": "Ethereum Ecosystem",
         "amount_usd": 50.0, "price": 1800.0},
        {"action": "SELL", "token": "ETH", "narrative": "Ethereum Ecosystem",
         "amount_usd": 50.0, "price": 1750.0,
         "error": "twak rc=1: twak binary not found at /home/mario/.npm-global/bin/twak"},
    ])
    check("failed SELL does not remove the still-open position",
          "ETH" in agent_failed_sell.portfolio.positions,
          extra=f"got {list(agent_failed_sell.portfolio.positions.keys())}")

    print("\n[3c] Malformed/hand-edited log line (null price) is skipped, not crashed or zeroed")
    agent_malformed = make_agent([
        # Mirrors the real corrupted line found in production: a manually
        # inserted "recovery" entry with narrative="ETH Recovery" and
        # price=null. float(None) raises TypeError if not guarded — this
        # must not crash startup, and must not fabricate a 0-qty position
        # that would silently zero out the portfolio's reported value.
        {"action": "BUY", "token": "ETH", "narrative": "ETH Recovery",
         "amount_usd": 130.29, "price": None},
    ])
    check("malformed line with null price doesn't crash recovery",
          "ETH" not in agent_malformed.portfolio.positions,
          extra=f"got {list(agent_malformed.portfolio.positions.keys())}")
    check("cash_usd untouched when the only log line is unreplayable",
          abs(agent_malformed.portfolio.cash_usd - agent_malformed.initial_capital) < 1e-9,
          extra=f"got {agent_malformed.portfolio.cash_usd}")

    print("\n[4] Old-schema log lines (positions as a plain string list) don't crash recovery")
    agent3b = make_agent([
        # Mimics older log entries written before `narrative` existed and
        # before `positions` became a list of dicts — just symbol strings.
        # _recover_portfolio must ignore `positions` entirely and not choke
        # on its shape.
        {"action": "BUY", "token": "ETH", "amount_usd": 26.70, "price": 1800.0,
         "open_positions": ["ETH"], "positions": ["ETH"]},
        {"action": "BUY", "token": "UNI", "amount_usd": 53.40, "price": 7.20,
         "open_positions": ["ETH", "UNI"], "positions": ["ETH", "UNI"]},
    ])
    check("old-schema positions field (list of strings) doesn't raise",
          set(agent3b.portfolio.positions.keys()) == {"ETH", "UNI"},
          extra=f"got {list(agent3b.portfolio.positions.keys())}")
    check("recovered narrative defaults to '' for lines missing the field",
          agent3b.portfolio.positions["ETH"].narrative == "",
          extra=f"got {agent3b.portfolio.positions['ETH'].narrative!r}")

    print("\n[5] No log file yet -> fresh portfolio, no crash")
    tmpdir = tempfile.mkdtemp()
    missing_log = os.path.join(tmpdir, "does_not_exist.log")
    old_log_path = LOG_PATH
    LOG_PATH = missing_log
    try:
        agent3 = Agent(mode="testnet")
    finally:
        LOG_PATH = old_log_path
    check("fresh portfolio when log is missing",
          len(agent3.portfolio.positions) == 0
          and agent3.portfolio.cash_usd == agent3.initial_capital)

    print(f"\n{'='*50}")
    print(f"  {passed} passed, {failed} failed")
    print(f"{'='*50}")
    if failed:
        raise SystemExit(1)


if __name__ == "__main__":
    if "--test" in sys.argv:
        _run_tests()
    else:
        try:
            asyncio.run(_main())
        except KeyboardInterrupt:
            print("\nInterrupted.")
