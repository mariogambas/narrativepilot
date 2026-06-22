"""
NarrativePilot — trade execution layer.

Two modes, selected by TRADING_MODE:

    testnet : fully simulated. NEVER calls twak, NEVER opens a network
              connection, NEVER needs a private key. It logs what it WOULD do
              and returns a deterministic mock tx hash. Safe to run the whole
              pipeline end-to-end with zero risk and zero funds.

    mainnet : real swaps on BSC via Trust Wallet Agent Kit (TWAK) CLI.
              The TWAK wallet is pre-configured and authenticated in this
              environment (credentials in ~/.twak/, password in system
              keychain). No private key management needed in the agent.
              Calls are made via subprocess; each call blocks until the
              on-chain tx is confirmed (wrapped in asyncio.to_thread to
              avoid stalling the event loop).
"""

import asyncio
import hashlib
import json
import os
import re
import subprocess
import time
from dataclasses import dataclass
from typing import Optional

from trader import TradeDecision

# Absolute path to the TWAK CLI binary. Overridable via TWAK_BIN if the
# install location changes; defaults to the confirmed path on this system.
TWAK_BIN = os.environ.get("TWAK_BIN", "/home/mario/.npm-global/bin/twak")

# ----------------------------------------------------------------------
# BSC mainnet token addresses (PancakeSwap V2 verified pairs)
# ----------------------------------------------------------------------

WBNB = "0xbb4CdB9CBd36B01bD1cBaEBF2De08d9173bc095c"
BUSD = "0xe9e7CEA3DedcA5984780Bafc599bD69ADd087D56"

# BEP-20 addresses for tradeable tokens — all verified on BscScan (June 2026).
# Keep in sync with cmc_client.BSC_LIQUID_IDS (selector uses that list).
# Any token NOT listed here returns a safe failure on mainnet instead of guessing.
# BNB is native (not BEP-20) — it's the swap source but never a swap target.
# Note: DOGE has 8 decimals; all others are 18. TWAK returns human-readable
# amounts, so the executor never needs to handle raw decimals directly.
TOKEN_ADDRESSES: dict[str, str] = {
    "ETH":  "0x2170Ed0880ac9A755fd29B2688956BD959F933F8",  # Binance-Peg Ethereum
    "CAKE": "0x0E09FaBB73Bd3Ade0a17ECC321fD13a19e81cE82",  # PancakeSwap Token
    "DOGE": "0xbA2aE424d960c26247Dd6c32edC70B295c744C43",  # Binance-Peg Dogecoin (8 dec)
    "XRP":  "0x1D2F0da169ceB9fC7B3144628dB156f3F6c60dBe",  # Binance-Peg XRP
    "ADA":  "0x3EE2200Efb3400fAbB9AacF31297cBdD1d435D47",  # Binance-Peg Cardano
    "TWT":  "0x4B0F1812e5Df2A09796481Ff14017e6005508003",  # Trust Wallet Token
    "LINK": "0xF8A0BF9cF54Bb92F17374d9e9A321E6a111a51bD",  # Binance-Peg Chainlink
    "AVAX": "0x1CE0c2827e2eF14D5C4f29a091d735A204794041",  # Binance-Peg Avalanche
    "FIL":  "0x0D8Ce2A99Bb6e3B7Db580eD848240e4a0F9aE153",  # Binance-Peg Filecoin
    "LTC":  "0x4338665CBB7B2485A8855A139b75D5e34AB0DB94",  # Binance-Peg Litecoin
    "INJ":  "0xa2B726B1145A4773F68593CF171187d8EBe4d495",  # Injective Protocol
    "UNI":  "0xBf5140A22578168FD562DCcF235E5D43A02ce9B1",  # Binance-Peg Uniswap
}

TWAK_TIMEOUT = 90  # seconds — on-chain confirmations can take a moment


# ----------------------------------------------------------------------
# Result object
# ----------------------------------------------------------------------

@dataclass
class ExecutionResult:
    success: bool
    action: str
    token: Optional[str]
    amount_usd: float
    fill_price: float
    qty: float
    tx_hash: str
    simulated: bool
    error: Optional[str] = None


# ----------------------------------------------------------------------
# Executor
# ----------------------------------------------------------------------

class TradeExecutor:
    def __init__(
        self,
        mode: str,
        slippage: float = 1.0,  # percent, passed to twak --slippage
    ):
        self.mode = mode.lower()
        self.slippage = slippage
        self._wallet_address: str = ""   # lazily resolved via twak wallet address

    # ------------------------------------------------------------------
    # Public entry point (async so callers can await without blocking)
    # ------------------------------------------------------------------

    async def execute(self, decision: TradeDecision) -> ExecutionResult:
        if decision.action not in ("BUY", "SELL"):
            return ExecutionResult(
                success=True, action=decision.action, token=decision.token,
                amount_usd=0.0, fill_price=decision.price, qty=0.0,
                tx_hash="", simulated=(self.mode != "mainnet"),
            )

        if self.mode == "mainnet":
            return await self._execute_onchain(decision)
        return self._simulate(decision)

    # ------------------------------------------------------------------
    # Testnet — pure simulation, no network, no key, no twak
    # ------------------------------------------------------------------

    def _simulate(self, decision: TradeDecision) -> ExecutionResult:
        price = decision.price if decision.price > 0 else 0.0
        qty = (decision.amount_usd / price) if price > 0 else 0.0

        seed = f"{decision.action}|{decision.token}|{decision.amount_usd}|{time.time_ns()}"
        tx_hash = "0x" + hashlib.sha256(seed.encode()).hexdigest()

        print(
            f"  [SIMULATED] {decision.action} {decision.token} "
            f"${decision.amount_usd:.2f} @ ${price:.6f}  "
            f"(qty {qty:.6f})  tx {tx_hash[:12]}…",
            flush=True,
        )

        return ExecutionResult(
            success=True, action=decision.action, token=decision.token,
            amount_usd=decision.amount_usd, fill_price=price, qty=qty,
            tx_hash=tx_hash, simulated=True,
        )

    # ------------------------------------------------------------------
    # Mainnet — TWAK CLI via subprocess
    # ------------------------------------------------------------------

    def _run_twak(self, args: list[str]) -> tuple[int, str, str]:
        """Synchronous TWAK call — run inside asyncio.to_thread."""
        try:
            result = subprocess.run(
                [TWAK_BIN] + args,
                capture_output=True, text=True, timeout=TWAK_TIMEOUT,
            )
            return result.returncode, result.stdout, result.stderr
        except subprocess.TimeoutExpired:
            return 1, "", f"twak timed out after {TWAK_TIMEOUT}s"
        except FileNotFoundError:
            return 1, "", f"twak binary not found at {TWAK_BIN} — is TWAK installed? (override with TWAK_BIN env var)"
        except Exception as e:
            return 1, "", str(e)

    async def _execute_onchain(self, decision: TradeDecision) -> ExecutionResult:
        token_sym = decision.token or ""
        try:
            # BNB guard first: BNB is the native swap-source currency, never a target.
            if token_sym == "BNB" and decision.action == "BUY":
                return ExecutionResult(
                    success=False, action="BUY", token="BNB",
                    amount_usd=decision.amount_usd, fill_price=decision.price,
                    qty=0.0, tx_hash="", simulated=False,
                    error="Cannot buy BNB (native) with BNB — path would be [WBNB, WBNB].",
                )

            token_addr = TOKEN_ADDRESSES.get(token_sym)
            if not token_addr:
                return ExecutionResult(
                    success=False, action=decision.action, token=token_sym,
                    amount_usd=decision.amount_usd, fill_price=decision.price,
                    qty=0.0, tx_hash="", simulated=False,
                    error=f"No verified BSC address for {token_sym}; refusing to guess. "
                          f"Add it to TOKEN_ADDRESSES.",
                )

            if decision.action == "BUY":
                return await self._buy_twak(decision)
            return await self._sell_twak(decision, token_addr)

        except Exception as e:
            return ExecutionResult(
                success=False, action=decision.action, token=token_sym,
                amount_usd=decision.amount_usd, fill_price=decision.price,
                qty=0.0, tx_hash="", simulated=False, error=str(e),
            )

    async def _buy_twak(self, decision: TradeDecision) -> ExecutionResult:
        # Can't swap BNB for BNB — path would be [WBNB, WBNB], invalid.
        if decision.token == "BNB":
            return ExecutionResult(
                success=False, action="BUY", token="BNB",
                amount_usd=decision.amount_usd, fill_price=decision.price,
                qty=0.0, tx_hash="", simulated=False,
                error="Cannot buy BNB (native) with BNB — path would be [WBNB, WBNB].",
            )

        token_addr = TOKEN_ADDRESSES[decision.token]
        args = [
            "swap", "--usd", str(decision.amount_usd),
            "BNB", token_addr,          # BNB = native; destination = contract address
            "--chain", "bsc",
            "--slippage", str(self.slippage),
            "--json",
        ]
        rc, stdout, stderr = await asyncio.to_thread(self._run_twak, args)
        return self._parse_swap_result(rc, stdout, stderr, decision, "BUY")

    async def _get_wallet_address(self) -> str:
        """Lazily resolve the TWAK agent wallet address on BSC (cached after first call)."""
        if self._wallet_address:
            return self._wallet_address
        rc, stdout, stderr = await asyncio.to_thread(
            self._run_twak, ["wallet", "address", "--chain", "bsc", "--json"]
        )
        if rc != 0:
            raise RuntimeError(f"Could not resolve wallet address: {stderr or stdout}")
        data = json.loads(stdout)
        self._wallet_address = data["address"]
        return self._wallet_address

    async def _sell_twak(self, decision: TradeDecision, token_addr: str) -> ExecutionResult:
        # TradeDecision has no qty field — read the actual on-chain balance via TWAK.
        # Confirmed live (June 2026): field is "available" for both native and ERC-20.
        wallet_addr = await self._get_wallet_address()
        balance_args = [
            "balance",
            "--address", wallet_addr,
            "--chain", "bsc",
            "--token", token_addr,
            "--json",
        ]
        rc, stdout, stderr = await asyncio.to_thread(self._run_twak, balance_args)
        if rc != 0:
            return ExecutionResult(
                success=False, action="SELL", token=decision.token,
                amount_usd=decision.amount_usd, fill_price=decision.price,
                qty=0.0, tx_hash="", simulated=False,
                error=f"balance query failed: {stderr or stdout}",
            )

        try:
            bal_data = json.loads(stdout)
        except json.JSONDecodeError:
            return ExecutionResult(
                success=False, action="SELL", token=decision.token,
                amount_usd=decision.amount_usd, fill_price=decision.price,
                qty=0.0, tx_hash="", simulated=False,
                error=f"unparseable balance output: {stdout[:200]}",
            )

        # Defensive: "available" confirmed live; fallbacks for any future API change.
        qty = float(
            bal_data.get("available")
            or bal_data.get("balance")
            or bal_data.get("amount")
            or bal_data.get("total")
            or 0
        )
        if qty <= 0:
            return ExecutionResult(
                success=False, action="SELL", token=decision.token,
                amount_usd=decision.amount_usd, fill_price=decision.price,
                qty=0.0, tx_hash="", simulated=False,
                error=f"No {decision.token} balance to sell (on-chain balance = 0).",
            )

        swap_args = [
            "swap", str(qty), token_addr, "BNB",  # source = contract address; BNB = native
            "--chain", "bsc",
            "--slippage", str(self.slippage),
            "--json",
        ]
        rc, stdout, stderr = await asyncio.to_thread(self._run_twak, swap_args)
        return self._parse_swap_result(rc, stdout, stderr, decision, "SELL", qty=qty)

    def _parse_swap_result(
        self,
        rc: int,
        stdout: str,
        stderr: str,
        decision: TradeDecision,
        action: str,
        qty: float = 0.0,
    ) -> ExecutionResult:
        if rc != 0:
            parts = [p for p in (stdout.strip(), stderr.strip()) if p]
            full_error = " | ".join(parts) if parts else f"twak exited with code {rc}"
            return ExecutionResult(
                success=False, action=action, token=decision.token,
                amount_usd=decision.amount_usd, fill_price=decision.price,
                qty=0.0, tx_hash="", simulated=False,
                error=f"twak rc={rc}: {full_error}",
            )

        try:
            data = json.loads(stdout)
        except json.JSONDecodeError:
            parts = [p for p in (stdout.strip(), stderr.strip()) if p]
            return ExecutionResult(
                success=False, action=action, token=decision.token,
                amount_usd=decision.amount_usd, fill_price=decision.price,
                qty=0.0, tx_hash="", simulated=False,
                error=f"unparseable twak output: {' | '.join(parts)}",
            )

        # --- tx hash: named field or fallback scan of all string values ---
        tx_hash = (
            data.get("hash")               # confirmed field name (live swap, June 2026)
            or data.get("txHash")
            or data.get("transactionHash")
            or data.get("transactionId")
            or data.get("tx")
            or ""
        )
        if not tx_hash:
            # Scan every string value for a 0x-prefixed 66-char BSC tx hash.
            for v in data.values():
                if isinstance(v, str) and re.fullmatch(r"0x[0-9a-fA-F]{64}", v):
                    tx_hash = v
                    break

        # --- output quantity: "0.705499 CAKE" -> 0.705499 ---
        amount_out = qty  # fallback to balance-query qty (SELL path)
        raw_output = data.get("output") or data.get("amountOut") or data.get("toAmount") or ""
        if raw_output:
            m = re.match(r"([0-9]+(?:\.[0-9]+)?)", str(raw_output))
            if m:
                amount_out = float(m.group(1))

        fill_price = (
            decision.amount_usd / amount_out
            if amount_out > 0 else decision.price
        )

        return ExecutionResult(
            success=bool(tx_hash),
            action=action, token=decision.token,
            amount_usd=decision.amount_usd, fill_price=fill_price,
            qty=amount_out, tx_hash=tx_hash, simulated=False,
            error=None if tx_hash else f"no tx_hash in twak output (rc=0): stdout={stdout.strip()!r} stderr={stderr.strip()!r}",
        )


# ----------------------------------------------------------------------
# Tests: python agent/executor.py
# ----------------------------------------------------------------------

if __name__ == "__main__":
    import sys
    from unittest.mock import patch, MagicMock

    passed = 0
    failed = 0

    def check(name: str, cond: bool, extra: str = "") -> None:
        global passed, failed
        if cond:
            passed += 1
            print(f"  PASS  {name}")
        else:
            failed += 1
            print(f"  FAIL  {name}  {extra}")

    # ------------------------------------------------------------------
    # [1] Testnet simulation (unchanged — no twak calls)
    # ------------------------------------------------------------------

    print("\n[1] Testnet simulation — no network, no twak")
    ex = TradeExecutor(mode="testnet")

    buy = TradeDecision(
        action="BUY", narrative="ai_tokens", token="CAKE",
        price=2.5, amount_usd=10.0, reason="test", score=85.0,
    )
    res = asyncio.run(ex.execute(buy))
    print(f"    -> {res}")
    check("BUY simulated successfully", res.success and res.simulated)
    check("mock tx hash 0x+64hex", res.tx_hash.startswith("0x") and len(res.tx_hash) == 66)
    check("qty = 10 / 2.5 = 4.0", abs(res.qty - 4.0) < 1e-9, extra=f"got {res.qty}")

    sell = TradeDecision(
        action="SELL", narrative="ai_tokens", token="CAKE",
        price=3.0, amount_usd=12.0, reason="exit", score=30.0,
    )
    res2 = asyncio.run(ex.execute(sell))
    check("SELL simulated successfully", res2.success and res2.simulated)

    hold = TradeDecision(
        action="HOLD", narrative="defi", token="ETH",
        price=0.0, amount_usd=0.0, reason="hold band", score=50.0,
    )
    res3 = asyncio.run(ex.execute(hold))
    check("HOLD is no-op (no tx)", res3.success and res3.tx_hash == "")

    # ------------------------------------------------------------------
    # [2] Mainnet BUY — subprocess mocked
    # ------------------------------------------------------------------

    print("\n[2] Mainnet BUY via TWAK (subprocess mocked)")

    # Real TWAK JSON shape (verified live): "output" = "<qty> <symbol>", tx hash
    # added by execution (not present in --quote-only).
    buy_twak_output = json.dumps({
        "input": "0.00165 BNB",
        "output": "4.120000 CAKE",
        "minReceived": "4.078800 CAKE",
        "provider": "0x",
        "priceImpact": "0",
        "txHash": "0xdeadbeef" + "a" * 56,
    })

    ex_main = TradeExecutor(mode="mainnet")
    with patch.object(ex_main, "_run_twak", return_value=(0, buy_twak_output, "")):
        res4 = asyncio.run(ex_main.execute(TradeDecision(
            action="BUY", narrative="binance", token="CAKE",
            price=2.5, amount_usd=10.0, reason="test", score=80.0,
        )))
    print(f"    -> {res4}")
    check("mainnet BUY success", res4.success and not res4.simulated)
    check("tx_hash from twak output", res4.tx_hash == "0xdeadbeef" + "a" * 56)
    check("qty from amountOut", abs(res4.qty - 4.12) < 1e-9, extra=f"got {res4.qty}")

    # ------------------------------------------------------------------
    # [3] BNB guard — even in mainnet, BUY BNB is rejected
    # ------------------------------------------------------------------

    print("\n[3] BNB guard — mainnet BUY BNB rejected without calling twak")

    call_count = 0
    original_run = ex_main._run_twak
    def counting_run(args):
        global call_count
        call_count += 1
        return original_run(args)

    with patch.object(ex_main, "_run_twak", side_effect=counting_run):
        res5 = asyncio.run(ex_main.execute(TradeDecision(
            action="BUY", narrative="bnb", token="BNB",
            price=600.0, amount_usd=10.0, reason="test", score=75.0,
        )))
    check("BNB BUY returns failure", not res5.success)
    check("BNB guard never calls twak", call_count == 0,
          extra=f"called {call_count} times")
    check("error mentions WBNB path", "WBNB" in (res5.error or ""),
          extra=f"error: {res5.error}")

    # ------------------------------------------------------------------
    # [4] Unconfigured token — safe failure
    # ------------------------------------------------------------------

    print("\n[4] Unconfigured token rejected without twak call")

    with patch.object(ex_main, "_run_twak", return_value=(0, "{}", "")) as mock_twak:
        res6 = asyncio.run(ex_main.execute(TradeDecision(
            action="BUY", narrative="ai", token="RENDER",
            price=5.0, amount_usd=10.0, reason="test", score=80.0,
        )))
        check("RENDER (no address) returns failure", not res6.success)
        check("RENDER safe failure doesn't call twak", not mock_twak.called)

    # ------------------------------------------------------------------
    # [5] Mainnet SELL — balance query + swap (two twak calls)
    # ------------------------------------------------------------------

    print("\n[5] Mainnet SELL via TWAK (balance + swap, both mocked)")

    # Real TWAK balance JSON shape: field is "available" (confirmed live June 2026)
    balance_output = json.dumps({
        "address": "0xBBE4006b1dc454aB4CF5361C1Bf27318864855b5",
        "chain": "smartchain",
        "symbol": "CAKE",
        "token": "0x0E09FaBB73Bd3Ade0a17ECC321fD13a19e81cE82",
        "available": "4.12",
        "total": "4.12",
        "totalUsd": 5.77,
    })
    sell_output = json.dumps({
        "input": "4.12 CAKE",
        "output": "0.016800 BNB",
        "minReceived": "0.016632 BNB",
        "provider": "LiquidMesh",
        "priceImpact": "0",
        "hash": "0xf00d" + "b" * 60,
    })

    # SELL path: wallet address lookup + balance query + swap (3 twak calls)
    wallet_output = json.dumps({"chain": "bsc", "address": "0xBBE4006b1dc454aB4CF5361C1Bf27318864855b5"})
    twak_responses = [(0, wallet_output, ""), (0, balance_output, ""), (0, sell_output, "")]
    response_iter = iter(twak_responses)

    with patch.object(ex_main, "_run_twak", side_effect=lambda args: next(response_iter)):
        res7 = asyncio.run(ex_main.execute(TradeDecision(
            action="SELL", narrative="binance", token="CAKE",
            price=2.5, amount_usd=10.0, reason="exit", score=30.0,
        )))
    print(f"    -> {res7}")
    check("mainnet SELL success", res7.success)
    check("SELL tx_hash from twak", res7.tx_hash == "0xf00d" + "b" * 60)

    # ------------------------------------------------------------------
    # [6] tx hash found by fallback scan (unknown field name)
    # ------------------------------------------------------------------

    print("\n[6] tx hash found by 0x-scan when field name is unknown")

    mystery_hash = "0x" + "c" * 64
    mystery_output = json.dumps({
        "output": "3.9 CAKE",
        "someUnknownField": mystery_hash,   # TWAK might use any key
    })
    with patch.object(ex_main, "_run_twak", return_value=(0, mystery_output, "")):
        res_scan = asyncio.run(ex_main.execute(TradeDecision(
            action="BUY", narrative="binance", token="CAKE",
            price=2.5, amount_usd=10.0, reason="test", score=80.0,
        )))
    check("scan fallback finds hash", res_scan.success and res_scan.tx_hash == mystery_hash,
          extra=f"tx_hash={res_scan.tx_hash}")
    check("output qty parsed from 'output' field", abs(res_scan.qty - 3.9) < 1e-9,
          extra=f"qty={res_scan.qty}")

    # ------------------------------------------------------------------
    # [7] twak timeout -> success=False, no crash
    # ------------------------------------------------------------------

    print("\n[7] twak timeout -> graceful failure")

    with patch.object(ex_main, "_run_twak", return_value=(1, "", "twak timed out after 90s")):
        res8 = asyncio.run(ex_main.execute(TradeDecision(
            action="BUY", narrative="binance", token="CAKE",
            price=2.5, amount_usd=10.0, reason="test", score=80.0,
        )))
    check("timeout returns success=False", not res8.success)
    check("timeout error message present", "timed out" in (res8.error or ""),
          extra=f"error: {res8.error}")

    # ------------------------------------------------------------------
    # [7] twak rc!=0 (e.g. insufficient funds) -> success=False
    # ------------------------------------------------------------------

    print("\n[8] twak rc!=0 (insufficient funds) -> graceful failure")

    with patch.object(ex_main, "_run_twak",
                      return_value=(1, "", "Error: Insufficient BNB balance")):
        res9 = asyncio.run(ex_main.execute(TradeDecision(
            action="BUY", narrative="binance", token="CAKE",
            price=2.5, amount_usd=10.0, reason="test", score=80.0,
        )))
    check("rc!=0 returns success=False", not res9.success)
    check("stderr captured in error field", "Insufficient" in (res9.error or ""),
          extra=f"error: {res9.error}")

    # ------------------------------------------------------------------

    print(f"\n{'='*50}")
    print(f"  {passed} passed, {failed} failed")
    print(f"{'='*50}")
    if failed:
        sys.exit(1)
