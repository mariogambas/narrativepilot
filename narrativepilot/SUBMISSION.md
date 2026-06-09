# NarrativePilot AI — BNB Hack Submission

## Project Title

**NarrativePilot AI: Autonomous Narrative-Driven Trading on BNB Chain**

---

## Problem Statement

Most crypto trading bots rely on lagging indicators (RSI, MACD, moving averages) or predict price movements directly. These approaches are reactive — they detect moves *after* they've started. By then, liquidity has shifted, momentum has weakened, and execution is expensive.

**The real signal arrives earlier:** Narrative rotations. When traders and capital begin to favor one market narrative over another (e.g., "AI tokens are having a moment" over "memes are cooling"), it shows up in on-chain sentiment, volume shifts, and market dominance *before* it's fully priced in.

We built an agent that detects these rotations in real-time and acts before the broader market catches on.

---

## Solution Overview

**NarrativePilot AI** is an autonomous agent that:

1. **Fetches trending crypto narratives** from the CMC Agent Hub every 15 minutes (Binance Ecosystem, Layer 1, AI tokens, Solana Ecosystem, etc.)

2. **Scores each narrative** on 4 independent dimensions:
   - **Rotation (35%):** How much the narrative is outperforming the broader market
   - **Momentum (25%):** Market cap growth across 24h/7d/30d
   - **Volume (20%):** Trading activity surge as a liquidity proxy
   - **Social (20%):** Unique author mentions on CMC (engagement signal)

3. **Applies a market regime filter** to avoid dangerous trades:
   - If fear & greed is extreme (≤20 or ≥80), reduce confidence by 25%
   - If funding rates are high (leveraged market), reduce by 10%
   - If liquidation volume is massive (>$500M), reduce by 15%
   - This prevents the agent from buying into cascades

4. **Executes trades on BNB Chain** with strict risk management:
   - Max position: 10% of portfolio per narrative
   - Stop-loss: Auto-exit at -8% per position
   - Portfolio drawdown cap: 20% max from peak
   - Only trades tokens with verified PancakeSwap V2 liquidity

5. **Logs every cycle** to JSON for full auditability. Judges can replay any decision.

---

## Why This Approach?

**Differentiation from other bots:**

- **Not just on-chain analysis** (too slow; needs aggregation); we use CMC's precomputed narratives (structured, LLM-ready)
- **Not just sentiment** (too noisy); we combine 4 signals to filter false positives
- **Not just momentum** (goes into bubbles); we add a regime filter to bail in risky markets
- **Not just technical** (lagging); we detect narrative *shifts* which lead price

**Example:** On June 9, 2026, the market was in Extreme Fear (fear & greed = 16). A bot purely on technical signals might have given up. Our agent, seeing that certain narratives were *still* outperforming despite the fear, identifies pockets of strength — and only enters them with reduced confidence. That's where alpha lives.

---

## Technical Stack

- **Data source:** CMC Agent Hub (official MCP interface), providing:
  - `trending_crypto_narratives()` — ranked narratives + on-chain metrics
  - `get_global_metrics_latest()` — market regime (fear & greed, altcoin season, BTC dominance)
  - `get_global_crypto_derivatives_metrics()` — funding rates, liquidation volume
  - `get_crypto_quotes_latest()` — current prices

- **Execution:** web3.py + eth-account for local key signing; PancakeSwap V2 Router on BSC mainnet

- **Storage:** Local JSON logs (testnet); on-chain wallet state (mainnet)

- **Dashboard:** HTML5 + Chart.js reading `logs/trades.log` in real-time (30s refresh)

- **Language:** Python 3.12+, async/await for concurrent API calls

---

## Validation Results

### Unit Tests (All Pass)

- **narrative_scorer.py:** 25/25 tests (scoring logic, regime filter, token selection)
- **trader.py:** 14/14 tests (decision tree, stop-loss, drawdown cap, position sizing)
- **executor.py:** 7/7 tests (BUY/SELL simulation, price resolution, edge cases)

### Smoke Tests (Live CMC Data)

Ran 2+ cycles on testnet with live CMC data:

```
Cycle 1:
  CMC narratives: Layer 1, Binance Ecosystem, SEC/CFTC Digital Commodities, US Strategic Crypto Reserve
  Regime: Extreme Fear (fear & greed 16, factor 0.75)
  Scores: Layer 1=35.8, Binance Ecosystem=34.7, ...
  Decision: HOLD (no score >= 70 after regime filter)
  
Cycle 2:
  (Regime still Extreme Fear)
  Scores: Similar
  Decision: HOLD
  
Result: Agent correctly identifies it's too risky to trade, preserves capital.
```

### Liquidity Verification

Verified live on BSC:
- **WBNB (BNB):** 0xbb4CdB9CBd36B01bD1cBaEBF2De08d9173bc095c — $8.76M in WBNB/BUSD pair
- **CAKE:** 0x0e09fabb73bd3ade0a17ecc321fd13a19e81ce82 — High liquidity (native PancakeSwap token)
- **ETH (Binance-Peg):** 0x2170ed0880ac9a755fd29b2688956bd959f933f8 — High liquidity (official Binance peg)
- **SOL (Binance-Peg):** 0x570a5d26f7765ecb712c0924e4de545b89fd43df — High liquidity (official Binance peg)

All 4 tokens have active PancakeSwap V2 pairs and can be traded.

### Dashboard

Live demo of the dashboard (showing portfolio, scores, decisions, equity curve) running on http://localhost:8080/dashboard/index.html. Aesthetics follow Binance branding (black #0B0E11, yellow #FCD535, green/red for gains/losses).

---

## Live Trading Plan (June 22–28, 2026)

1. **Pre-launch (June 21):** Deploy with ~$100–$500 USD in BNB on testnet wallet
2. **Launch morning (June 22):** Run `python agent/main.py --mode mainnet` on a stable machine
3. **Monitor:** Check dashboard every few hours; review JSON logs for any anomalies
4. **Adapt (if needed):** If market regime changes drastically (e.g., Fed announcement), the regime filter adjusts automatically; no manual tweaking needed

**Risk posture:** Conservative. The agent only trades if regime allows *and* signal is strong. In current conditions (Extreme Fear), it would HOLD and wait. This is intentional — preserve capital until conditions align.

---

## Stack Utilization

✅ **CoinMarketCap Agent Hub:** Primary data source for narratives and regime metrics
✅ **BNB Chain:** Trade execution via PancakeSwap V2 on BSC mainnet
✅ **Trust Wallet Agent Kit patterns:** Local key signing (similar to Trust Wallet's self-custody model)

**Bonus integrations (eligible for partner prizes):**
- CMC Agent Hub: $2k prize pool ← **This project qualifies**
- BNB Stack: Uses BNB Chain for execution ← **This project qualifies**
- Trust Wallet: Local key signing pattern ← **Conceptually aligned**

---

## Code Repository

```
narrativepilot/
├── agent/
│   ├── __init__.py
│   ├── main.py              (orchestrator, loop)
│   ├── cmc_client.py        (CMC Hub MCP client)
│   ├── narrative_scorer.py  (4-signal scoring + regime filter)
│   ├── trader.py            (decision engine + risk mgmt)
│   └── executor.py          (testnet simulation / mainnet execution)
├── dashboard/
│   └── index.html           (live monitoring UI)
├── logs/
│   └── trades.log           (JSON audit trail)
├── .env.example             (configuration template)
├── requirements.txt         (dependencies)
├── README.md                (user guide)
├── ARCHITECTURE.md          (technical deep-dive)
└── [this file]              (submission)
```

All code is deterministic, logged, and auditable. No hidden decision logic.

---

## What Success Looks Like

**For this hackathon (June 22–28):**

- Agent runs continuously without crashes
- Executes at least 1–2 trades (if regime permits) or holds prudently (if it doesn't)
- Dashboard shows live updates
- JSON logs are clean and traceable
- PnL is positive (or at worst, losses are small and explainable via regime)

**Long-term vision:**

Narrative rotations as a market signal layer. Most trading infrastructure is built on price/volume. Adding *narrative momentum* as a data layer unlocks alpha that technical-only approaches miss. CMC, with its editorial lens on narratives, is the ideal data foundation.

---

## Team

**Builder:** Solo developer, writing all code during the hackathon (June 3–21).

**Tools used:**
- Claude (via Claude Desktop + Claude Code) for architecture and implementation
- Python + web3.py for execution
- CMC Agent Hub API for data
- BNB Chain testnet for validation

---

## Disclaimer

This is a proof of concept for the BNB Hack hackathon. It has been tested on testnet and undergone unit testing, but **has not traded real capital yet**. The agent will execute real trades on mainnet June 22–28 if deployed. Losses are possible. Always test on testnet first.

---

## References

- [CMC Agent Hub Documentation](https://pro.coinmarketcap.com/api/documentation/ai-agent-hub/)
- [BNB Chain](https://www.bnbchain.org)
- [PancakeSwap V2 Docs](https://docs.pancakeswap.finance)
- [web3.py Docs](https://web3py.readthedocs.io)

---

## Closing

NarrativePilot AI demonstrates that trading on *narrative rotations* (a leading signal) rather than price moves (lagging) can unlock alpha in volatile crypto markets. By combining CMC's structured narrative data with on-chain risk metrics, we build a bot that's both opportunistic and prudent — it trades strong signals *when it's safe to trade*, and holds otherwise. For the BNB Chain hackathon, this approach fits naturally: detect where capital is flowing (narrative momentum), and execute on BSC where liquidity is deep.

We're ready to go live on June 22.
