"""
NarrativePilot — decision logic + risk management.

Turns narrative scores into BUY / HOLD / SELL decisions and enforces the
hackathon's risk rules:

    * max position size   = 10% of current portfolio value per entry
    * stop-loss           = close a position down 8% from entry
    * max drawdown cap    = stop opening new positions once the portfolio is
                            20% below its peak value (risk-off)

Decision thresholds on a narrative's 0-100 score:

    score > 70   -> BUY  the strongest token of the narrative
    40 <= score  -> HOLD (keep an existing position, do not enter)
    score < 40   -> SELL any open position belonging to the narrative
"""

from dataclasses import dataclass, field
from typing import Optional

from cmc_client import NARRATIVES

ENTRY_THRESHOLD = 70.0
# Reduced-conviction band: [REDUCED_THRESHOLD, ENTRY_THRESHOLD] allows a
# half-size entry instead of a plain HOLD.
REDUCED_THRESHOLD = 55.0
EXIT_THRESHOLD = 40.0
MIN_TRADE_USD = 1.0

# symbol -> narrative lookup
TOKEN_NARRATIVE = {sym: n for n, syms in NARRATIVES.items() for sym in syms}


# ----------------------------------------------------------------------
# Portfolio state
# ----------------------------------------------------------------------

@dataclass
class Position:
    symbol: str
    narrative: str
    qty: float           # token units held
    entry_price: float   # USD price at entry
    cost_usd: float      # USD invested (cost basis)


@dataclass
class Portfolio:
    initial_capital_usd: float
    cash_usd: float = 0.0
    positions: dict[str, Position] = field(default_factory=dict)
    peak_value_usd: float = 0.0

    def __post_init__(self):
        if self.cash_usd == 0.0:
            self.cash_usd = self.initial_capital_usd
        if self.peak_value_usd == 0.0:
            self.peak_value_usd = self.initial_capital_usd

    # --- valuation -----------------------------------------------------

    def total_value(self, prices: dict[str, float]) -> float:
        held = sum(
            pos.qty * prices.get(sym, pos.entry_price)
            for sym, pos in self.positions.items()
        )
        return self.cash_usd + held

    def update_peak(self, value: float) -> None:
        if value > self.peak_value_usd:
            self.peak_value_usd = value

    def drawdown_pct(self, value: float) -> float:
        """Fractional drop from the peak value ever reached (0.0-1.0)."""
        if self.peak_value_usd <= 0:
            return 0.0
        return max(0.0, (self.peak_value_usd - value) / self.peak_value_usd)

    def position_pnl_pct(self, symbol: str, price: float) -> float:
        pos = self.positions.get(symbol)
        if not pos or pos.entry_price <= 0:
            return 0.0
        return (price - pos.entry_price) / pos.entry_price

    # --- mutations (called after the executor confirms a fill) ---------

    def apply_buy(self, symbol: str, narrative: str, amount_usd: float, price: float) -> None:
        qty = amount_usd / price if price > 0 else 0.0
        self.cash_usd -= amount_usd
        self.positions[symbol] = Position(
            symbol=symbol,
            narrative=narrative,
            qty=qty,
            entry_price=price,
            cost_usd=amount_usd,
        )

    def apply_sell(self, symbol: str, price: float) -> float:
        """Closes the position, returns realized PnL in USD."""
        pos = self.positions.pop(symbol)
        proceeds = pos.qty * price
        self.cash_usd += proceeds
        return proceeds - pos.cost_usd


# ----------------------------------------------------------------------
# Decision object
# ----------------------------------------------------------------------

@dataclass
class TradeDecision:
    action: str                       # "BUY" | "SELL" | "HOLD"
    narrative: str
    token: Optional[str]
    price: float
    reason: str
    amount_usd: float = 0.0           # set for BUY
    score: float = 0.0


# ----------------------------------------------------------------------
# Trader
# ----------------------------------------------------------------------

class Trader:
    def __init__(
        self,
        max_position_pct: float,
        stop_loss_pct: float,
        max_drawdown_pct: float,
    ):
        self.max_position_pct = max_position_pct
        self.stop_loss_pct = stop_loss_pct
        self.max_drawdown_pct = max_drawdown_pct

    def decide(
        self,
        scores_result: dict,
        prices: dict[str, float],
        portfolio: Portfolio,
    ) -> list[TradeDecision]:
        decisions: list[TradeDecision] = []
        sold: set[str] = set()

        scores = scores_result["narrative_scores"]
        best_tokens = scores_result["best_tokens"]

        # --- update valuation & risk state --------------------------------
        value = portfolio.total_value(prices)
        portfolio.update_peak(value)
        drawdown = portfolio.drawdown_pct(value)
        risk_off = drawdown >= self.max_drawdown_pct

        # --- 1) stop-loss sweep (highest priority, overrides everything) --
        for sym, pos in list(portfolio.positions.items()):
            price = prices.get(sym, pos.entry_price)
            pnl = portfolio.position_pnl_pct(sym, price)
            if pnl <= -self.stop_loss_pct:
                decisions.append(TradeDecision(
                    action="SELL",
                    narrative=pos.narrative,
                    token=sym,
                    price=price,
                    amount_usd=pos.qty * price,
                    reason=f"Stop-loss hit: position {pnl*100:+.1f}% <= -{self.stop_loss_pct*100:.0f}%.",
                ))
                sold.add(sym)

        # --- 2) per-narrative score-based decisions -----------------------
        for narrative, score in scores.items():
            # SELL: score below exit threshold -> close any open position(s)
            if score < EXIT_THRESHOLD:
                for sym, pos in list(portfolio.positions.items()):
                    if pos.narrative == narrative and sym not in sold:
                        price = prices.get(sym, pos.entry_price)
                        decisions.append(TradeDecision(
                            action="SELL",
                            narrative=narrative,
                            token=sym,
                            price=price,
                            amount_usd=pos.qty * price,
                            reason=f"{narrative} score {score} < exit threshold {EXIT_THRESHOLD:.0f}.",
                            score=score,
                        ))
                        sold.add(sym)
                continue

            # BUY: full conviction above ENTRY_THRESHOLD (10%), reduced
            # conviction in [REDUCED_THRESHOLD, ENTRY_THRESHOLD] (half size).
            if score > ENTRY_THRESHOLD:
                size_pct = self.max_position_pct
                entry_label = f"score {score} > threshold {ENTRY_THRESHOLD:.0f}"
            elif score >= REDUCED_THRESHOLD:
                size_pct = self.max_position_pct / 2
                entry_label = (f"REDUCED_CONVICTION: score {score} in "
                               f"[{REDUCED_THRESHOLD:.0f}, {ENTRY_THRESHOLD:.0f}] band")
            else:
                # 40 <= score < 55 -> plain HOLD
                decisions.append(TradeDecision(
                    action="HOLD", narrative=narrative, token=best_tokens.get(narrative),
                    price=0.0,
                    reason=f"{narrative} score {score} in hold band [{EXIT_THRESHOLD:.0f}, {REDUCED_THRESHOLD:.0f}).",
                    score=score,
                ))
                continue

            token = best_tokens.get(narrative)
            price = prices.get(token, 0.0) if token else 0.0

            already_held = any(
                p.narrative == narrative and s not in sold
                for s, p in portfolio.positions.items()
            )

            if already_held:
                decisions.append(TradeDecision(
                    action="HOLD", narrative=narrative, token=token, price=price,
                    reason=f"{narrative} {entry_label} but already holding; not pyramiding.",
                    score=score,
                ))
            elif token in sold:
                decisions.append(TradeDecision(
                    action="HOLD", narrative=narrative, token=token, price=price,
                    reason=f"{token} was just sold this cycle (stop-loss/exit); not re-entering same token.",
                    score=score,
                ))
            elif risk_off:
                decisions.append(TradeDecision(
                    action="HOLD", narrative=narrative, token=token, price=price,
                    reason=f"Risk-off: drawdown {drawdown*100:.1f}% >= cap {self.max_drawdown_pct*100:.0f}%. New entries blocked.",
                    score=score,
                ))
            elif not token or price <= 0:
                decisions.append(TradeDecision(
                    action="HOLD", narrative=narrative, token=token, price=price,
                    reason=f"{narrative} {entry_label} but no tradable token/price available.",
                    score=score,
                ))
            else:
                amount = min(size_pct * value, portfolio.cash_usd)
                if amount < MIN_TRADE_USD:
                    decisions.append(TradeDecision(
                        action="HOLD", narrative=narrative, token=token, price=price,
                        reason=f"{narrative} {entry_label} but insufficient cash (${portfolio.cash_usd:.2f}).",
                        score=score,
                    ))
                else:
                    decisions.append(TradeDecision(
                        action="BUY", narrative=narrative, token=token, price=price,
                        amount_usd=round(amount, 2),
                        reason=f"{narrative} {entry_label}. Buying strongest token {token} "
                               f"at {size_pct*100:.0f}% sizing.",
                        score=score,
                    ))

        return decisions


# ----------------------------------------------------------------------
# Unit tests: python agent/trader.py
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

    trader = Trader(max_position_pct=0.10, stop_loss_pct=0.08, max_drawdown_pct=0.20)

    print("\n[1] BUY on strong score with correct 10% sizing")
    pf = Portfolio(initial_capital_usd=100.0)
    scores = {
        "narrative_scores": {"ai_tokens": 92.6, "meme_coins": 25.4},
        "best_tokens": {"ai_tokens": "RENDER", "meme_coins": "DOGE"},
    }
    prices = {"RENDER": 8.0, "DOGE": 0.15}
    decs = trader.decide(scores, prices, pf)
    buy = next((d for d in decs if d.action == "BUY"), None)
    check("emits a BUY", buy is not None)
    check("BUY token is RENDER", buy and buy.token == "RENDER")
    check("BUY size is 10% of $100 = $10", buy and abs(buy.amount_usd - 10.0) < 1e-6,
          extra=f"got {buy.amount_usd if buy else None}")
    # meme score 25.4 < 40 but no position -> nothing to sell, just no buy
    check("no BUY for weak meme narrative",
          not any(d.action == "BUY" and d.narrative == "meme_coins" for d in decs))

    print("\n[2] Apply the BUY, then SELL when score collapses")
    pf.apply_buy("RENDER", "ai_tokens", buy.amount_usd, prices["RENDER"])
    check("position opened", "RENDER" in pf.positions)
    check("cash reduced to $90", abs(pf.cash_usd - 90.0) < 1e-6, extra=f"{pf.cash_usd}")

    scores2 = {
        "narrative_scores": {"ai_tokens": 30.0, "meme_coins": 25.0},
        "best_tokens": {"ai_tokens": "RENDER", "meme_coins": "DOGE"},
    }
    decs2 = trader.decide(scores2, {"RENDER": 9.0}, pf)  # price up, but score collapsed
    sell = next((d for d in decs2 if d.action == "SELL"), None)
    check("emits a SELL when score < 40", sell is not None and sell.token == "RENDER")
    check("SELL reason cites exit threshold", sell and "exit threshold" in sell.reason)

    print("\n[3] Stop-loss overrides a still-bullish score")
    pf3 = Portfolio(initial_capital_usd=100.0)
    pf3.apply_buy("WLD", "ai_tokens", 10.0, 2.0)   # entry at $2.00
    scores3 = {
        "narrative_scores": {"ai_tokens": 95.0, "meme_coins": 50.0},
        "best_tokens": {"ai_tokens": "WLD", "meme_coins": "DOGE"},
    }
    # price drops to $1.80 = -10%, beyond -8% stop
    decs3 = trader.decide(scores3, {"WLD": 1.80}, pf3)
    sl = next((d for d in decs3 if d.action == "SELL" and d.token == "WLD"), None)
    check("stop-loss SELL fires even with score 95", sl is not None)
    check("stop-loss reason cites stop-loss", sl and "Stop-loss" in sl.reason)
    check("no BUY/HOLD-buy for the stopped-out token in same cycle",
          not any(d.action == "BUY" and d.token == "WLD" for d in decs3))

    print("\n[4] Drawdown cap blocks new entries")
    pf4 = Portfolio(initial_capital_usd=100.0)
    pf4.peak_value_usd = 100.0
    pf4.cash_usd = 75.0   # value now $75 -> 25% drawdown from peak, beyond 20% cap
    scores4 = {
        "narrative_scores": {"ai_tokens": 90.0, "meme_coins": 30.0},
        "best_tokens": {"ai_tokens": "RENDER", "meme_coins": "DOGE"},
    }
    decs4 = trader.decide(scores4, {"RENDER": 8.0}, pf4)
    check("no BUY while in drawdown risk-off",
          not any(d.action == "BUY" for d in decs4))
    hold = next((d for d in decs4 if d.narrative == "ai_tokens"), None)
    check("risk-off reason is logged", hold and "Risk-off" in hold.reason,
          extra=f"{hold.reason if hold else None}")

    print("\n[5] Position size never exceeds available cash")
    # Capital is tied up in a meme position ($97), only $3 cash free.
    # Value is ~$100 so 10% sizing wants $10, but cash caps the BUY at $3.
    pf5 = Portfolio(initial_capital_usd=100.0)
    pf5.apply_buy("DOGE", "meme_coins", 97.0, 0.10)  # cash -> $3, value still ~$100
    scores5 = {
        "narrative_scores": {"ai_tokens": 90.0, "meme_coins": 50.0},  # meme HOLD, keep DOGE
        "best_tokens": {"ai_tokens": "RENDER", "meme_coins": "DOGE"},
    }
    decs5 = trader.decide(scores5, {"RENDER": 8.0, "DOGE": 0.10}, pf5)
    buy5 = next((d for d in decs5 if d.action == "BUY"), None)
    check("10% sizing wants $10 but BUY is capped at $3 cash",
          buy5 and abs(buy5.amount_usd - 3.0) < 1e-6,
          extra=f"got {buy5.amount_usd if buy5 else None}")

    print("\n[6] Reduced-conviction band [55, 70]")
    # score 60 -> half-size BUY (5% of $100 = $5)
    pf6 = Portfolio(initial_capital_usd=100.0)
    scores6 = {
        "narrative_scores": {"Binance Ecosystem": 60.0},
        "best_tokens": {"Binance Ecosystem": "CAKE"},
    }
    decs6 = trader.decide(scores6, {"CAKE": 1.30}, pf6)
    rb = next((d for d in decs6 if d.action == "BUY"), None)
    check("score 60 -> BUY emitted", rb is not None)
    check("reduced BUY sized at 5% of $100 = $5",
          rb and abs(rb.amount_usd - 5.0) < 1e-6,
          extra=f"got {rb.amount_usd if rb else None}")
    check("reason flags REDUCED_CONVICTION",
          rb and "REDUCED_CONVICTION" in rb.reason,
          extra=f"reason: {rb.reason if rb else None}")

    # score 54 -> plain HOLD (below the reduced band)
    pf6b = Portfolio(initial_capital_usd=100.0)
    scores6b = {
        "narrative_scores": {"Binance Ecosystem": 54.0},
        "best_tokens": {"Binance Ecosystem": "CAKE"},
    }
    decs6b = trader.decide(scores6b, {"CAKE": 1.30}, pf6b)
    check("score 54 -> HOLD, no BUY",
          not any(d.action == "BUY" for d in decs6b))
    hold6b = next((d for d in decs6b if d.action == "HOLD"), None)
    check("score 54 reason cites hold band", hold6b and "hold band" in hold6b.reason,
          extra=f"reason: {hold6b.reason if hold6b else None}")

    # score 60 with an open position in the narrative -> HOLD (no pyramiding)
    pf6c = Portfolio(initial_capital_usd=100.0)
    pf6c.apply_buy("CAKE", "Binance Ecosystem", 5.0, 1.30)
    decs6c = trader.decide(scores6, {"CAKE": 1.30}, pf6c)
    check("score 60 with open position -> no BUY (no pyramiding)",
          not any(d.action == "BUY" for d in decs6c))
    hold6c = next((d for d in decs6c if d.action == "HOLD"), None)
    check("no-pyramiding reason logged", hold6c and "not pyramiding" in hold6c.reason)

    # full-conviction path unchanged: score 90 still buys 10%
    pf6d = Portfolio(initial_capital_usd=100.0)
    scores6d = {
        "narrative_scores": {"Binance Ecosystem": 90.0},
        "best_tokens": {"Binance Ecosystem": "CAKE"},
    }
    decs6d = trader.decide(scores6d, {"CAKE": 1.30}, pf6d)
    fb = next((d for d in decs6d if d.action == "BUY"), None)
    check("score 90 still buys at 10% ($10)",
          fb and abs(fb.amount_usd - 10.0) < 1e-6,
          extra=f"got {fb.amount_usd if fb else None}")
    check("full-conviction reason has no REDUCED flag",
          fb and "REDUCED_CONVICTION" not in fb.reason)

    # reduced entries also blocked under drawdown risk-off
    pf6e = Portfolio(initial_capital_usd=100.0)
    pf6e.peak_value_usd = 100.0
    pf6e.cash_usd = 75.0   # 25% drawdown -> risk_off
    decs6e = trader.decide(scores6, {"CAKE": 1.30}, pf6e)
    check("reduced entry blocked by risk-off drawdown",
          not any(d.action == "BUY" for d in decs6e))

    print(f"\n{'='*50}")
    print(f"  {passed} passed, {failed} failed")
    print(f"{'='*50}")
    if failed:
        raise SystemExit(1)


if __name__ == "__main__":
    _run_tests()
