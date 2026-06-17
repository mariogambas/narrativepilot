# NarrativePilot AI — Technical Architecture

## System Overview

```
┌─────────────────────────────────────────────────────────────────┐
│                     CMC Agent Hub (MCP)                         │
│  trending_crypto_narratives() + global_metrics() +              │
│  get_crypto_derivatives_metrics()                               │
└──────────────────────────┬──────────────────────────────────────┘
                           │
                           ▼
        ┌──────────────────────────────────────┐
        │      cmc_client.py                   │
        │  Parse & normalize narratives        │
        │  Fetch prices (symbol→id→price)      │
        └──────────────┬───────────────────────┘
                       │
                       ▼
        ┌──────────────────────────────────────┐
        │    narrative_scorer.py               │
        │  Score each narrative (0–100)        │
        │  Apply market regime filter          │
        └──────────────┬───────────────────────┘
                       │
                       ▼
        ┌──────────────────────────────────────┐
        │      trader.py                       │
        │  BUY/SELL/HOLD decision              │
        │  Risk mgmt: stops, drawdown, sizing  │
        └──────────────┬───────────────────────┘
                       │
                       ▼
        ┌──────────────────────────────────────┐
        │     executor.py                      │
        │  Simulate (testnet) or execute       │
        │  (mainnet) via web3.py + PancakeSwap │
        └──────────────┬───────────────────────┘
                       │
                       ▼
        ┌──────────────────────────────────────┐
        │      main.py                         │
        │  Orchestrate: loop every 15 min      │
        │  Log JSON to trades.log              │
        └──────────────┬───────────────────────┘
                       │
                       ▼
        ┌──────────────────────────────────────┐
        │    dashboard/index.html              │
        │  Read logs, render live (30s refresh)│
        └──────────────────────────────────────┘
```

---

## Module Breakdown

### 1. cmc_client.py

**Purpose:** Interface to CMC Agent Hub. Fetches trending narratives, global market metrics, derivatives data. Resolves token symbols to USD prices.

**Key methods:**

- `_call(tool_name, args)` — Send JSON-RPC to `https://mcp.coinmarketcap.com/mcp` with `X-CMC-MCP-API-KEY` header. Handles double-JSON parsing (Hub wraps response in `.content[0].text`).

- `get_trending_narratives()` — Calls `trending_crypto_narratives()` tool. Parses the response to extract:
  - `trendingRank` — position in trending list
  - `categoryName` — narrative slug (e.g., "AI tokens")
  - `marketCapChangePercentage24h/7d/30d` — momentum signal
  - `volumeChangePercentage24h` — volume signal
  - `volumeWeightedPricePerfVsCryptoMarketCap24h/7d/30d` — rotation signal (price perf vs. overall market)
  - `socialKeywordUniqueAuthorCount` — social engagement
  - `topCoinList` — top 3 coins in the narrative (used by token selector)
  - **Deduplication:** Some narratives appear multiple times; dedup by slug.

- `get_global_metrics()` — Calls `get_global_metrics_latest()` tool. Extracts:
  - `fear_and_greed_index` (0–100) + label (Extreme fear, Fear, Neutral, Greed, Extreme greed)
  - `altcoin_season_index` (0–100, where >50 = altcoins outperforming BTC)
  - `btc_dominance_percentage`
  - Used by the regime filter in the scorer.

- `get_derivatives()` — Calls `get_global_crypto_derivatives_metrics()`. Extracts:
  - `funding_rate_avg` — average perpetual funding rate (high = overleveraged market, risky)
  - `liquidation_volume_24h` — total liquidated in 24h (high = cascades likely)
  - Also used by regime filter.

- `get_prices(symbols)` — For each symbol, resolve to CMC id via `BSC_LIQUID_IDS` map (or fallback to `search_cryptos()` tool), then fetch price from `get_crypto_quotes_latest()`. Returns `{symbol: price_usd}` dict.

- `fetch_all_signals()` — Run all 3 fetches in parallel (`asyncio.gather`). Returns dict with `narratives`, `regime`, and `prices`.

**Rate limiting:** CMC free tier = 333 calls/day. This agent makes ~3 calls/cycle × 96 cycles/day = ~288 calls/day. Safe margin.

**Caching:** Internal 60-second TTL on narrative/metrics to avoid duplicate calls within a cycle.

---

### 2. narrative_scorer.py

**Purpose:** Score each trending narrative (0–100) based on 4 market signals. Apply a regime filter to reduce scores in risky conditions.

**Signal model:**

Each narrative gets a composite score:

```
raw_score = 0.35 × rotation_signal
          + 0.25 × momentum_signal
          + 0.20 × volume_signal
          + 0.20 × social_signal

final_score = raw_score × regime_factor
```

**Signal definitions:**

1. **Rotation (35%):** `volumeWeightedPricePerfVsCryptoMarketCap`
   - Blend: `0.5 × pct_24h + 0.3 × pct_7d + 0.2 × pct_30d`
   - Normalized to 0–100: change of ±5% = 0/100 endpoints
   - Key insight: if a narrative is up 5% while BTC is flat/down, it's rotating in

2. **Momentum (25%):** Market cap change
   - Blend: `0.5 × mcap_24h + 0.3 × mcap_7d + 0.2 × mcap_30d`
   - Normalized: ±10% = 0/100 endpoints
   - Measures if the narrative itself (not just token price) is gaining TVL/adoption

3. **Volume (20%):** Volume change 24h
   - Normalized: ±25% = 0/100 endpoints
   - Liquidity confirmation; if volume surges, it's a real move, not noise

4. **Social (20%):** Unique authors mentioning the narrative on CMC
   - Min-max normalized *relative to all narratives in this batch*
   - If AI tokens are mentioned by 100 unique authors and memes by 50, AI gets 100, memes get 50

**Regime filter (`regime_factor`):**

A multiplier ∈ [0.6, 1.0] applied to reduce scores in dangerous market conditions:

```
regime_factor = fear_greed_factor × funding_factor × liquidation_factor

fear_greed_factor:
  - if fg <= 20 or fg >= 80: 0.75 (Extreme fear/greed → caution)
  - if fg <= 30 or fg >= 70: 0.90 (Light fear/greed)
  - else: 1.0 (Neutral)

funding_factor:
  - if |funding| > 0.10: 0.80 (Very leveraged)
  - if |funding| > 0.03: 0.90 (Moderately leveraged)
  - else: 1.0

liquidation_factor:
  - if liquidations_24h > $1B: 0.70 (Cascade risk)
  - if liquidations_24h > $500M: 0.85 (Moderate liquidation pressure)
  - else: 1.0
```

Example: A narrative scores 95 raw, but market is in Extreme Fear (fg=16), funding is high (0.12), and $800M liquidated. Factor = 0.75 × 0.80 × 0.85 = 0.51. Final score = 95 × 0.51 = 48.5. Even though the signal is strong, the regime is too risky, so the score drops from entry threshold.

**Token selector:**

Given the winning narrative (highest score), pick the best token from its `topCoinList`:

1. Filter to tokens that exist in `TOKEN_ADDRESSES` (BSC tradeable universe)
2. Among those, pick the one with highest `priceChangePercent7d` (strongest momentum within the narrative)
3. If none are tradeable, return `None` (the trader will HOLD)

Example: Binance Ecosystem scores 67 (HOLD zone), but if it scored 75 (entry), the selector would pick BNB from its top coins.

---

### 3. trader.py

**Purpose:** Decision engine. Given scores, decide BUY/SELL/HOLD. Apply risk management: stops, drawdown cap, position sizing.

**Decision tree** (priority order):

1. **Valuate & compute drawdown.**
   - `portfolio.total_value()` = cash + (positions × current_price)
   - If `total_value > portfolio.peak_value`, update peak
   - `drawdown_pct = (peak - current) / peak`
   - If `drawdown >= 20%`: set `risk_off = True` (no new entries) — this overrides every other rule, including the forced daily trade below

2. **Sweep for stop-loss (max priority).**
   - For each open position: if `pnl_pct <= -8%`, mark for `SELL`
   - Always executed, regardless of signal or gas reserve (sells are never blocked by the gas reserve, since selling is what replenishes liquid BNB)

3. **Score-based decision by narrative:**
   - If `score < 40`: `SELL` all positions in this narrative
   - If `40 <= score < 55`: `HOLD` (no action)
   - If `55 <= score < 70`: reduced-conviction `BUY` at 5% sizing
   - If `score >= 70`: full-conviction `BUY` at 10% sizing, subject to guards:
     - Already own the token? → `HOLD` (no pyramiding)
     - Token was just sold this cycle (stop-loss)? → `HOLD` (no churn)
     - `risk_off = True` (drawdown cap)? → `HOLD`
     - No valid price? → `HOLD`
     - Insufficient cash after gas reserve? → `HOLD`

4. **Forced daily trade (competition requirement).** The BNB Hack rules require at least 1 trade per day to qualify for Track 1 ranking. If `force_trade=True` (passed in from `main.py` when 23+ hours have elapsed since the last trade) and no normal BUY fired this cycle, the agent picks the best-scoring narrative with an available, unowned, not-just-sold token and forces a small entry at 2.5% sizing — half of the reduced-conviction size. This is blocked by `risk_off` and by the gas reserve exactly like a normal BUY. If no valid token exists anywhere, the agent logs an explicit warning and stays in HOLD rather than crash or violate risk rules.

**Position sizing (with gas reserve):**

```
GAS_RESERVE_USD = 6.0  # always kept out of buy sizing
spendable = max(0, cash - GAS_RESERVE_USD)
max_per_position = size_pct × portfolio_value   # 10%, 5%, or 2.5%
actual_amount = min(max_per_position, spendable)
```

The $6 reserve (~0.01 BNB) is never touched by BUY sizing, so the agent can always afford gas for a future SELL — including an urgent stop-loss — even if every other dollar is deployed. If `spendable` after the reserve is below the minimum trade size, the agent HOLDs with an explicit "gas reserve" reason instead of forcing an undersized trade.

If portfolio is $100 and you have $50 cash, spendable is $44 (after the $6 reserve). At 10% sizing, you can deploy up to $10 per trade, limited by spendable cash. The agent buys once and holds until SELL signal.

**Output:** `TradeDecision` dataclass with action, token, price, amount USD, reason.

---

### 4. executor.py

**Purpose:** Execute or simulate trades using Trust Wallet Agent Kit (TWAK), a non-custodial CLI that handles wallet signing and on-chain swaps without the agent ever touching a private key directly.

**Modes:**

- **Testnet:** `_simulate()` returns a mock tx hash. No blockchain call, no capital spent, no TWAK invocation at all.
- **Mainnet:** `_buy_twak()` or `_sell_twak()` shell out to the `twak` CLI via `subprocess`, wrapped in `asyncio.to_thread()` so the agent's event loop isn't blocked while waiting for on-chain confirmation.

**Mainnet logic (BUY):**

1. Guard: if `token == "BNB"`, reject immediately (BNB is native, not a swappable BEP-20 target against itself)
2. Resolve token contract address from `TOKEN_ADDRESSES[symbol]` (12-token eligible universe)
3. Run `twak swap --usd <amount_usd> BNB <token_address> --chain bsc --slippage 1 --json`
4. Parse the JSON response — confirmed live fields: `input`, `output`, `minReceived`, `provider`, `priceImpact`, `hash`, `explorer`. The transaction hash lives in the `hash` field (verified empirically against a real $0.50 mainnet swap, not assumed from documentation)
5. Defensive parsing: if `hash` is missing, fall back to scanning all JSON values for a 66-character `0x`-prefixed string before giving up

**Mainnet logic (SELL):**

1. Query the real on-chain balance first: `twak balance --chain bsc --token <address> --json` (the wallet's actual `available` balance — TradeDecision doesn't carry a `qty` field, so unlike a naive implementation, the executor never assumes a stale or estimated quantity)
2. Run `twak swap <exact_qty> <token_address> BNB --chain bsc --slippage 1 --json`
3. Same defensive JSON parsing as BUY

**Error handling:**

- `subprocess.TimeoutExpired` (60s timeout) → `success=False`
- `twak` not found in PATH → `success=False`
- Non-zero exit code (insufficient funds, failed swap) → `success=False, error=stderr`
- Unparseable JSON → `success=False`, raw output preserved in `error` for debugging
- All exceptions caught; the agent's main loop never crashes on a failed trade

**Testnet output:**

```json
{
  "success": true,
  "action": "BUY",
  "token": "CAKE",
  "amount_usd": 10.0,
  "fill_price": 2.84,
  "qty": 3.52,
  "tx_hash": "0x7c6a3f...",  // mock
  "simulated": true,
  "error": ""
}
```

---

### 5. main.py

**Purpose:** Orchestrate the loop. Fetch → score → decide → execute → log.

**Main loop:**

```python
while True:
    cycle += 1
    
    # Fetch
    signals = await cmc_client.fetch_all_signals()
    
    # Score
    scores = scorer.score_narratives(signals['narratives'], signals['regime'])
    best_token = scorer.select_token(scores)
    
    # Fetch prices for decision
    prices = await cmc_client.get_prices(portfolio.open_symbols() + [best_token])
    
    # Decide
    decision = trader.decide(scores, prices, portfolio)
    
    # Execute
    result = executor.execute(decision)
    
    # Update portfolio
    if result.success:
        if decision.action == "BUY":
            portfolio.apply_buy(decision.token, result.qty, result.fill_price)
        elif decision.action == "SELL":
            portfolio.apply_sell(decision.token, result.fill_price)
    
    # Log
    log_line = {
        "timestamp": now,
        "cycle": cycle,
        "action": decision.action,
        "narrative_scores": {name: score for name, score in scores.items()},
        "market_regime": signals['regime'],
        "portfolio_value": portfolio.total_value(),
        "open_positions": [pos.token for pos in portfolio.positions],
        "reason": decision.reason,
    }
    logger.write_json(log_line)
    
    # Sleep
    await asyncio.sleep(SCAN_INTERVAL_MINUTES * 60)
```

**Signal handling:**

- SIGINT / SIGTERM: Print final PnL, close files, exit gracefully
- API errors: Log, continue next cycle (resilient)

---

## Data Flow: Example Trade

**Cycle 1 (Extreme Fear):**

```
CMC Hub says "Layer 1" is trending with:
  - mcap change: -5% (24h)
  - vwPerf: +1.2% (beating market in relative terms)
  - volume change: +15%
  - social authors: 45

Scorer computes:
  raw = 0.35×45 + 0.25×30 + 0.20×60 + 0.20×55 = 43.5
  regime = fear(0.75) × funding(0.90) × liq(0.85) = 0.57
  final = 43.5 × 0.57 = 24.8 → SELL existing, no new entry (< 40)

Trader: SELL all Layer 1 positions
```

**Cycle 5 (Market normalizes, fear → 45):**

```
CMC Hub says "Binance Ecosystem" is trending with:
  - mcap change: +8%
  - vwPerf: +3.5%
  - volume change: +25%
  - social authors: 120

Scorer computes:
  raw = 0.35×100 + 0.25×75 + 0.20×100 + 0.20×100 = 91.5
  regime = fear(1.0) × funding(1.0) × liq(0.95) = 0.95
  final = 91.5 × 0.95 = 87.0 → BUY (>= 70)
  
Token selector picks: BNB (highest 7d change in Binance Ecosystem)

Trader: BUY $10 of BNB
  But executor rejects: "Cannot buy BNB with BNB"
  → Trader receives success=False, treats as HOLD
```

**Cycle 6 (Token selector picks SOL instead):**

```
Same narrative "Binance Ecosystem", but selector picks SOL (second in topCoinList)

Executor BUYs $10 SOL:
  - Path: WBNB → SOL
  - Gets 0.065 SOL @ $154/SOL
  - tx_hash: 0x3f2a...

Portfolio now: $90 cash + 0.065 SOL
```

**Cycle 10 (SOL drops 8%):**

```
SOL @ $141.68 (-8%)
Trader detects stop-loss: -8% hit exactly
Action: SELL SOL
Executor: Path SOL → WBNB, get ~$9.20 back
Portfolio: $99.20 cash, lesson learned
```

---

## Key Design Decisions

1. **Narratives from CMC (not custom list):** The Hub gives us what's trending *now*, not a static list. This is more adaptive.

2. **4 signals, not 1:** No single metric is reliable. Rotation + momentum + volume + social together filter noise.

3. **Regime filter:** A market in Extreme Fear can still have strong rotations, but trading them is dangerous. The filter *dampens* signal confidence, not zeros it.

4. **Local key signing:** No multisig, no external confirmations. The agent is autonomous. For a hackathon, this is intentional; for production, you'd add approvals/time-locks.

5. **Testnet ≠ mainnet:** Full simulation on testnet; real tx on mainnet. The logic is identical, so testnet results are predictive.

6. **Token liquidity gates:** `TOKEN_ADDRESSES` is the single source of truth. Tokens not in it are rejected at runtime, not rejected speculatively. This prevents surprises.

---

## Known Limitations & Future Work

1. **No on-chain execution on Ethereum/Solana.** The agent trades *on BSC only*. To trade AI tokens on Ethereum (where they live), you'd need an Ethereum executor and liquidity bridges.

2. **No dynamic fee adjustment.** Slippage is fixed at 1%. In volatile markets, you'd want dynamic slippage % based on pool depth.

3. **No cascade detection.** The agent sees global liquidation volume but doesn't predict *which* tokens will cascade. Could add on-chain whale monitoring.

4. **No backtesting framework.** Testnet is live simulation. For risk modeling, you'd want historical backtests (would need a CMC data dump).

5. **No ML/adaptive scoring.** Weights are fixed (35/25/20/20). A future version could learn optimal weights from realized PnL.

---

## Audit Trail

Every decision is logged to `logs/trades.log` in JSON format. A judge can replay any cycle:

```bash
jq '.[] | select(.cycle == 5)' logs/trades.log
```

This shows exactly what the agent saw, scored, and decided at cycle 5. Full transparency.

---

## Test Coverage

| Module | Tests |
|--------|-------|
| `narrative_scorer.py` | 27/27 |
| `trader.py` | 39/39 |
| `executor.py` | 21/21 |

All passing, plus a live mainnet smoke test: a real $0.50 BNB→CAKE swap executed via TWAK confirmed the full execution path end-to-end, including the actual JSON response schema (`hash`, `output`, `minReceived`) used to harden the parsing logic against assumptions.
