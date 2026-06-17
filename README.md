# NarrativePilot AI

An autonomous AI trading agent that detects crypto narrative rotations in real-time using the CMC Agent Hub, scores them across multiple market signals, and executes trades on BNB Chain with active risk management.

**Track:** BNB Hack: AI Trading Agent Edition (Track 1 — Autonomous Trading Agents)  
**Live trading window:** June 22–28, 2026  
**Status:** ✅ Testnet validated · Ready for mainnet

---

## Overview

NarrativePilot AI operates on a thesis: **narrative rotations are a leading signal of market movement**, and they can be detected from trending data + on-chain metrics before price reflects them. Instead of classic technical indicators (RSI, MACD), the agent monitors which crypto narratives (AI tokens, Solana Ecosystem, Layer 1, etc.) are gaining momentum across volume, sentiment, and market dominance — then executes when conditions align.

**The agent:**
- Fetches **trending crypto narratives** from the CMC Agent Hub (official signal provider)
- Scores each narrative across 4 dimensions: rotation (volumeWeightedPrice), momentum (market cap change), volume change, and social author count
- Applies a **market regime filter** (fear & greed, funding rates, liquidation risk) to avoid trading in dangerous conditions
- Selects the strongest token within the winning narrative (with verified PancakeSwap liquidity on BSC)
- Executes via web3.py + PancakeSwap V2 Router with local key signing (no manual confirmations)
- Logs every cycle to JSON for replay and auditing

**Differentiation:** Most trading bots use lagging indicators or predict price moves. NarrativePilot detects narrative *shifts* (a leading signal) before they're priced in, and only acts when the broader market regime permits (risk-managed).

---

## Tech Stack

- **Data:** CMC Agent Hub (MCP) — trending narratives, global metrics, derivatives data
- **Execution:** Trust Wallet Agent Kit (TWAK) — non-custodial agent wallet, local signing, on-chain swaps via CLI
- **Storage:** Local JSON logs (testnet) + on-chain wallet state (mainnet)
- **Testing:** Testnet simulation (no real BNB spent), unit tests for each module

**Tradeable tokens (12, BNB Hack official eligible list):** ETH, CAKE, DOGE, XRP, ADA, TWT, LINK, AVAX, FIL, LTC, INJ, UNI — all Binance-Peg or native BEP-20, verified live liquidity on PancakeSwap V2/V3. BNB itself is excluded (native asset, not BEP-20, and not on the competition's eligible list).

**Competition registration:** Agent wallet registered on-chain via `twak compete register` against the official BNB Hack competition contract on BSC. [View registration tx on BscScan](https://bscscan.com/tx/0x9a0d88d951b8a91a06b1f6f8f646a3220bcf883f186f245e4bfc6954d00c667a)

---

## Getting Started

### Prerequisites

- Python 3.12+
- Git
- MetaMask (or other BSC wallet for mainnet)
- CMC API key (free tier, from https://pro.coinmarketcap.com)

### Install

```bash
git clone <your-repo> narrativepilot
cd narrativepilot
python -m venv venv
source venv/bin/activate  # or `venv\Scripts\activate` on Windows
pip install -r requirements.txt
```

### Configure

Copy `.env.example` to `.env` and fill in:

```bash
cp .env.example .env
notepad .env  # or your editor
```

Required variables:

```
CMC_API_KEY=<your_key_from_pro.coinmarketcap.com>
TRADING_MODE=testnet  # or "mainnet" for live trading
WALLET_PRIVATE_KEY=<your_bsc_private_key>  # only for mainnet
SCAN_INTERVAL_MINUTES=15
INITIAL_CAPITAL_USD=100
MAX_POSITION_PCT=0.10
STOP_LOSS_PCT=0.08
MAX_DRAWDOWN_PCT=0.20
```

### Run

**Testnet (simulation, no real BNB):**

```bash
python agent/main.py --mode testnet
```

Output: one JSON line per cycle in `logs/trades.log`, one line per decision to stdout.

**View the dashboard** (in another terminal):

```bash
python -m http.server 8080
# Then open http://localhost:8080/dashboard/index.html
```

The dashboard updates every 30 seconds, showing live scores, portfolio value, decision feed, and equity curve.

---

## Architecture

See [ARCHITECTURE.md](./ARCHITECTURE.md) for detailed design: module breakdown, signal flow, risk management logic, and mainnet considerations.

Quick summary:

1. **cmc_client.py** — MCP client; fetches trending narratives, global metrics, derivatives; resolves symbol→price via CMC
2. **narrative_scorer.py** — Scores each narrative (0–100) using 4 signals (rotation 35%, momentum 25%, volume 20%, social 20%); applies regime filter for risk-off markets
3. **trader.py** — Decision engine; applies umbrals (70=BUY, 40=HOLD, <40=SELL); checks stops-loss; sizes positions (max 10% per narrative, 20% portfolio drawdown cap)
4. **executor.py** — Simulates (testnet) or executes (mainnet) BUY/SELL via PancakeSwap; handles slippage, approvals, gas
5. **main.py** — Orchestrates: fetch signals → score → decide → execute → log; runs indefinitely on `SCAN_INTERVAL_MINUTES` cadence
6. **dashboard/** — HTML5 + Chart.js; reads `logs/trades.log` every 30s and renders portfolio, scores, decisions, equity curve

---

## Testing

Unit tests are in each module (run with `python agent/<module>.py`):

```bash
python agent/narrative_scorer.py    # 27/27 tests
python agent/trader.py              # 39/39 tests
python agent/executor.py            # 21/21 tests
```

All pass. Smoke tests (live CMC data, real decisions, and a live $0.50 TWAK swap on mainnet) confirm the full pipeline end-to-end.

---

## Risk Management

The agent enforces:

- **Max position size:** 10% of portfolio per narrative (full conviction), 5% (reduced conviction, score 55-70), 2.5% (forced daily trade)
- **Stop-loss:** Automatically exit if any position drops > 8%
- **Portfolio drawdown cap:** If portfolio drops > 20% from peak, shift to HOLD mode (no new entries) — this takes absolute priority over every other rule, including the forced daily trade
- **Gas reserve:** $6 USD (~0.01 BNB) is always kept out of buy sizing so the agent never strands itself without funds to pay gas for a future sell
- **Minimum daily activity:** if no trade has fired in 23 hours, the agent forces a small (2.5%) entry into the best-scoring narrative with an available, untouched token — required to qualify for Track 1 ranking. Survives PC restarts by reading the last trade timestamp back from the JSON log on startup
- **Regime filter:** In fear/high-liquidation environments, reduce confidence scores by 25–40% (amortigua overconfidence)
- **Token selector:** Only chooses tokens with verified BSC liquidity from the 12-token eligible universe; rejects others safely

**What it doesn't do:** No leverage, no borrowing, no manual overrides. Everything is deterministic and auditable.

---

## Mainnet Deployment (June 22–28, 2026)

To go live on BSC mainnet:

1. Install Trust Wallet Agent Kit (TWAK) in WSL/Linux: `curl -fsSL https://agent-kit.trustwallet.com/install.sh | bash`
2. Run `twak setup` to create the agent wallet (non-custodial, encrypted in your OS keychain)
3. Fund the agent wallet address with your trading capital in BNB
4. Register for the competition: `twak compete register` (one-time, before the trading window opens)
5. In `.env`, change `TRADING_MODE=mainnet` and set `INITIAL_CAPITAL_USD` to your actual deployed capital
6. Run: `python agent/main.py --mode mainnet`
7. Monitor the dashboard and logs in real-time

**Important:** Start the agent a few hours before the trading window opens so the agent has time to read the current market regime and narrative state before the live PnL window starts.

---

## Logs

Each cycle writes one JSON line to `logs/trades.log`:

```json
{
  "timestamp": "2026-06-09T09:05:33",
  "cycle": 1,
  "action": "HOLD",
  "narrative": "Layer 1",
  "narrative_scores": {
    "Layer 1": 35.8,
    "Binance Ecosystem": 34.7,
    "...": "..."
  },
  "market_regime": {
    "fear_greed": 16,
    "label": "Extreme fear",
    "factor": 0.75,
    "btc_dominance": 58.19,
    "funding_avg": 0.035,
    "liquidations_24h": 62950000
  },
  "portfolio_value": 100.0,
  "open_positions": [],
  "reason": "No actionable signal. Top narrative Layer 1 @ 35.8."
}
```

This log is the **PnL replay** that hackathon judges use to audit the agent's behavior. Every decision is transparent and traceable.

---

## FAQ

**Q: Why narratives instead of technical analysis?**  
A: Narrative rotations are a *leading* signal — they're detected on-chain and in sentiment before price moves. Classic TA is lagging. We trade the signal before consensus catches it.

**Q: What if BNB is selected as the best token?**  
A: The executor safely rejects it (can't swap BNB for BNB). The trader treats it as HOLD. In practice, BNB is the base currency for the whole chain, so "buying BNB" is already done via your wallet balance.

**Q: Is the dashboard real-time?**  
A: It refreshes every 30 seconds by reading `logs/trades.log` from disk. Not WebSocket-driven, so there's a slight lag, but it's sufficient for monitoring.

**Q: What's the difference between testnet and mainnet modes?**  
A: Testnet simulates all swaps (returns mock tx hashes, doesn't touch the blockchain). Mainnet sends real transactions to BSC and requires your private key. The logic is identical.

**Q: Can I customize the narrative list or thresholds?**  
A: Yes. Edit the thresholds in `trader.py` (currently 70/40), the signal weights in `narrative_scorer.py` (currently 35/25/20/20), or the regime filter params. The CMC Agent Hub provides whatever narratives are trending; you can't filter them client-side (that's the point — detect what's hot now).

---

## Disclaimer

This is a **proof of concept** for the BNB Hack hackathon. While it has been tested on testnet and undergone unit testing, **it has not been battle-tested in live trading**. Use at your own risk. The agent will trade real capital on mainnet if deployed; losses are possible. Always test on testnet first, never deploy with capital you can't afford to lose.

---

## License

MIT

---

**Contact / Questions**

Built during BNB Hack: AI Trading Agent Edition (June 3–21, 2026).

For technical details, see [ARCHITECTURE.md](./ARCHITECTURE.md).
