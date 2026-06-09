"""
NarrativePilot — trade execution layer.

Two modes, selected by TRADING_MODE:

    testnet : fully simulated. NEVER imports web3, NEVER opens a network
              connection, NEVER needs a private key. It logs what it WOULD do
              and returns a deterministic mock tx hash. Safe to run the whole
              pipeline end-to-end with zero risk and zero funds.

    mainnet : real swaps on BSC via PancakeSwap V2 Router, signed locally with
              the private key from .env (Trust-Wallet-style local signing, no
              manual confirmation). Slippage is configurable and enforced
              on-chain through amountOutMin.

The web3 stack is imported lazily inside the mainnet path so that testnet runs
have no dependency on a live node or a key being present.
"""

import hashlib
import time
from dataclasses import dataclass
from typing import Optional

from trader import TradeDecision

# ----------------------------------------------------------------------
# BSC mainnet infrastructure addresses (PancakeSwap V2)
# ----------------------------------------------------------------------

PANCAKE_ROUTER = "0x10ED43C718714eb63d5aA57B78B54704E256024E"
WBNB = "0xbb4CdB9CBd36B01bD1cBaEBF2De08d9173bc095c"
BUSD = "0xe9e7CEA3DedcA5984780Bafc599bD69ADd087D56"
BSC_MAINNET_CHAIN_ID = 56

# BEP-20 addresses for tradeable tokens — the BSC tradeable universe.
# Verified live (Mario, June 2026) to have active PancakeSwap V2 pairs.
# Any token NOT listed here returns a safe failure on mainnet instead of
# guessing. Keep this in sync with cmc_client.BSC_LIQUID_IDS (the selector).
TOKEN_ADDRESSES: dict[str, str] = {
    "BNB":  "0xbb4CdB9CBd36B01bD1cBaEBF2De08d9173bc095c",  # WBNB — $8.76M WBNB/BUSD
    "CAKE": "0x0e09fabb73bd3ade0a17ecc321fd13a19e81ce82",  # PancakeSwap native
    "ETH":  "0x2170ed0880ac9a755fd29b2688956bd959f933f8",  # Binance-Peg ETH
    "SOL":  "0x570a5d26f7765ecb712c0924e4de545b89fd43df",  # Binance-Peg SOL
}

# Minimal ABIs — only the fragments we actually call.
ROUTER_ABI = [
    {"name": "swapExactETHForTokens", "type": "function", "stateMutability": "payable",
     "inputs": [{"name": "amountOutMin", "type": "uint256"},
                {"name": "path", "type": "address[]"},
                {"name": "to", "type": "address"},
                {"name": "deadline", "type": "uint256"}],
     "outputs": [{"name": "amounts", "type": "uint256[]"}]},
    {"name": "swapExactTokensForETH", "type": "function", "stateMutability": "nonpayable",
     "inputs": [{"name": "amountIn", "type": "uint256"},
                {"name": "amountOutMin", "type": "uint256"},
                {"name": "path", "type": "address[]"},
                {"name": "to", "type": "address"},
                {"name": "deadline", "type": "uint256"}],
     "outputs": [{"name": "amounts", "type": "uint256[]"}]},
    {"name": "getAmountsOut", "type": "function", "stateMutability": "view",
     "inputs": [{"name": "amountIn", "type": "uint256"},
                {"name": "path", "type": "address[]"}],
     "outputs": [{"name": "amounts", "type": "uint256[]"}]},
]

ERC20_ABI = [
    {"name": "balanceOf", "type": "function", "stateMutability": "view",
     "inputs": [{"name": "owner", "type": "address"}],
     "outputs": [{"name": "", "type": "uint256"}]},
    {"name": "decimals", "type": "function", "stateMutability": "view",
     "inputs": [], "outputs": [{"name": "", "type": "uint8"}]},
    {"name": "allowance", "type": "function", "stateMutability": "view",
     "inputs": [{"name": "owner", "type": "address"}, {"name": "spender", "type": "address"}],
     "outputs": [{"name": "", "type": "uint256"}]},
    {"name": "approve", "type": "function", "stateMutability": "nonpayable",
     "inputs": [{"name": "spender", "type": "address"}, {"name": "amount", "type": "uint256"}],
     "outputs": [{"name": "", "type": "bool"}]},
]

MAX_UINT256 = 2**256 - 1


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
        rpc_url: str = "",
        private_key: str = "",
        slippage: float = 0.01,
    ):
        self.mode = mode.lower()
        self.rpc_url = rpc_url
        self._private_key = private_key
        self.slippage = slippage

        # Lazily-initialised mainnet state — stays None in testnet.
        self._w3 = None
        self._account = None
        self._router = None

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    def execute(self, decision: TradeDecision) -> ExecutionResult:
        if decision.action not in ("BUY", "SELL"):
            # HOLD and friends are not executable — nothing to do.
            return ExecutionResult(
                success=True, action=decision.action, token=decision.token,
                amount_usd=0.0, fill_price=decision.price, qty=0.0,
                tx_hash="", simulated=(self.mode != "mainnet"),
            )

        if self.mode == "mainnet":
            return self._execute_onchain(decision)
        return self._simulate(decision)

    # ------------------------------------------------------------------
    # Testnet — pure simulation, no network, no key
    # ------------------------------------------------------------------

    def _simulate(self, decision: TradeDecision) -> ExecutionResult:
        price = decision.price if decision.price > 0 else 0.0
        qty = (decision.amount_usd / price) if price > 0 else 0.0

        # Deterministic, realistic-looking 32-byte mock tx hash.
        seed = f"{decision.action}|{decision.token}|{decision.amount_usd}|{time.time_ns()}"
        tx_hash = "0x" + hashlib.sha256(seed.encode()).hexdigest()

        print(
            f"  [SIMULATED] {decision.action} {decision.token} "
            f"${decision.amount_usd:.2f} @ ${price:.6f}  "
            f"(qty {qty:.6f})  tx {tx_hash[:12]}…"
        )

        return ExecutionResult(
            success=True, action=decision.action, token=decision.token,
            amount_usd=decision.amount_usd, fill_price=price, qty=qty,
            tx_hash=tx_hash, simulated=True,
        )

    # ------------------------------------------------------------------
    # Mainnet — real PancakeSwap V2 swap
    # ------------------------------------------------------------------

    def _ensure_connected(self):
        if self._w3 is not None:
            return
        # Lazy import: testnet never pulls these in.
        from web3 import Web3
        from eth_account import Account

        if not self._private_key:
            raise RuntimeError("WALLET_PRIVATE_KEY is required for mainnet mode.")

        self._w3 = Web3(Web3.HTTPProvider(self.rpc_url))
        if not self._w3.is_connected():
            raise RuntimeError(f"Could not connect to BSC RPC at {self.rpc_url}")

        self._account = Account.from_key(self._private_key)
        self._router = self._w3.eth.contract(
            address=Web3.to_checksum_address(PANCAKE_ROUTER), abi=ROUTER_ABI
        )

    def _bnb_price_usd(self) -> float:
        from web3 import Web3
        one_bnb = 10**18
        amounts = self._router.functions.getAmountsOut(
            one_bnb,
            [Web3.to_checksum_address(WBNB), Web3.to_checksum_address(BUSD)],
        ).call()
        return amounts[-1] / 10**18  # BUSD ≈ USD, 18 decimals

    def _execute_onchain(self, decision: TradeDecision) -> ExecutionResult:
        from web3 import Web3

        token_sym = decision.token or ""
        try:
            self._ensure_connected()

            token_addr = TOKEN_ADDRESSES.get(token_sym)
            if not token_addr:
                return ExecutionResult(
                    success=False, action=decision.action, token=token_sym,
                    amount_usd=decision.amount_usd, fill_price=decision.price,
                    qty=0.0, tx_hash="", simulated=False,
                    error=f"No verified BSC address configured for {token_sym}; "
                          f"refusing to guess. Add it to TOKEN_ADDRESSES.",
                )
            token_addr = Web3.to_checksum_address(token_addr)
            wbnb = Web3.to_checksum_address(WBNB)
            addr = self._account.address
            deadline = int(time.time()) + 300

            if decision.action == "BUY":
                return self._buy(decision, token_addr, wbnb, addr, deadline)
            return self._sell(decision, token_addr, wbnb, addr, deadline)

        except Exception as e:
            return ExecutionResult(
                success=False, action=decision.action, token=token_sym,
                amount_usd=decision.amount_usd, fill_price=decision.price,
                qty=0.0, tx_hash="", simulated=False, error=str(e),
            )

    def _buy(self, decision, token_addr, wbnb, addr, deadline) -> ExecutionResult:
        # Can't buy BNB with BNB — the swap path would be [WBNB, WBNB], which is
        # invalid. BNB is the base/gas currency, so a "BUY BNB" is a no-op here.
        # Safe failure -> main.py won't open a position (effectively a HOLD).
        if decision.token == "BNB":
            return ExecutionResult(
                success=False, action="BUY", token="BNB",
                amount_usd=decision.amount_usd, fill_price=decision.price,
                qty=0.0, tx_hash="", simulated=False,
                error="Cannot buy BNB (native) with BNB — path would be [WBNB, WBNB].",
            )

        bnb_price = self._bnb_price_usd()
        amount_in_wei = int((decision.amount_usd / bnb_price) * 10**18)

        balance = self._w3.eth.get_balance(addr)
        if balance < amount_in_wei:
            raise RuntimeError(
                f"Insufficient BNB: have {balance/1e18:.5f}, need {amount_in_wei/1e18:.5f}"
            )

        path = [wbnb, token_addr]
        amounts = self._router.functions.getAmountsOut(amount_in_wei, path).call()
        expected_out = amounts[-1]
        amount_out_min = int(expected_out * (1 - self.slippage))

        tx = self._router.functions.swapExactETHForTokens(
            amount_out_min, path, addr, deadline
        ).build_transaction({
            "from": addr,
            "value": amount_in_wei,
            "nonce": self._w3.eth.get_transaction_count(addr),
            "gas": 250000,
            "gasPrice": self._w3.eth.gas_price,
            "chainId": BSC_MAINNET_CHAIN_ID,
        })
        receipt = self._sign_and_send(tx)

        decimals = self._token_decimals(token_addr)
        qty = expected_out / 10**decimals
        fill_price = decision.amount_usd / qty if qty > 0 else 0.0
        return ExecutionResult(
            success=(receipt.status == 1), action="BUY", token=decision.token,
            amount_usd=decision.amount_usd, fill_price=fill_price, qty=qty,
            tx_hash=receipt.transactionHash.hex(), simulated=False,
        )

    def _sell(self, decision, token_addr, wbnb, addr, deadline) -> ExecutionResult:
        token = self._w3.eth.contract(address=token_addr, abi=ERC20_ABI)
        amount_in = token.functions.balanceOf(addr).call()
        if amount_in == 0:
            raise RuntimeError(f"No {decision.token} balance to sell.")

        # Approve the router once if needed.
        allowance = token.functions.allowance(addr, self._router.address).call()
        if allowance < amount_in:
            approve_tx = token.functions.approve(
                self._router.address, MAX_UINT256
            ).build_transaction({
                "from": addr,
                "nonce": self._w3.eth.get_transaction_count(addr),
                "gas": 60000,
                "gasPrice": self._w3.eth.gas_price,
                "chainId": BSC_MAINNET_CHAIN_ID,
            })
            self._sign_and_send(approve_tx)

        path = [token_addr, wbnb]
        amounts = self._router.functions.getAmountsOut(amount_in, path).call()
        amount_out_min = int(amounts[-1] * (1 - self.slippage))

        tx = self._router.functions.swapExactTokensForETH(
            amount_in, amount_out_min, path, addr, deadline
        ).build_transaction({
            "from": addr,
            "nonce": self._w3.eth.get_transaction_count(addr),
            "gas": 300000,
            "gasPrice": self._w3.eth.gas_price,
            "chainId": BSC_MAINNET_CHAIN_ID,
        })
        receipt = self._sign_and_send(tx)

        decimals = self._token_decimals(token_addr)
        qty = amount_in / 10**decimals
        bnb_out = amounts[-1] / 10**18
        proceeds_usd = bnb_out * self._bnb_price_usd()
        fill_price = proceeds_usd / qty if qty > 0 else 0.0
        return ExecutionResult(
            success=(receipt.status == 1), action="SELL", token=decision.token,
            amount_usd=proceeds_usd, fill_price=fill_price, qty=qty,
            tx_hash=receipt.transactionHash.hex(), simulated=False,
        )

    # -- low-level helpers ---------------------------------------------

    def _token_decimals(self, token_addr) -> int:
        token = self._w3.eth.contract(address=token_addr, abi=ERC20_ABI)
        return token.functions.decimals().call()

    def _sign_and_send(self, tx):
        signed = self._account.sign_transaction(tx)
        tx_hash = self._w3.eth.send_raw_transaction(signed.raw_transaction)
        return self._w3.eth.wait_for_transaction_receipt(tx_hash, timeout=180)


# ----------------------------------------------------------------------
# Smoke test (testnet only): python agent/executor.py
# ----------------------------------------------------------------------

if __name__ == "__main__":
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

    print("\n[testnet] simulation needs NO rpc and NO private key")
    ex = TradeExecutor(mode="testnet")  # note: no rpc_url, no private_key
    check("no web3 connection created on init", ex._w3 is None)

    buy = TradeDecision(
        action="BUY", narrative="ai_tokens", token="RENDER",
        price=8.0, amount_usd=10.0, reason="test", score=92.6,
    )
    res = ex.execute(buy)
    print(f"    -> {res}")
    check("BUY simulated successfully", res.success and res.simulated)
    check("mock tx hash looks real (0x + 64 hex)",
          res.tx_hash.startswith("0x") and len(res.tx_hash) == 66)
    check("qty = 10 / 8 = 1.25", abs(res.qty - 1.25) < 1e-9, extra=f"got {res.qty}")
    check("still no network connection after execute", ex._w3 is None)

    sell = TradeDecision(
        action="SELL", narrative="ai_tokens", token="RENDER",
        price=9.0, amount_usd=11.25, reason="exit", score=30.0,
    )
    res2 = ex.execute(sell)
    check("SELL simulated successfully", res2.success and res2.simulated)

    hold = TradeDecision(
        action="HOLD", narrative="meme_coins", token="DOGE",
        price=0.0, amount_usd=0.0, reason="hold band", score=50.0,
    )
    res3 = ex.execute(hold)
    check("HOLD is a no-op (no tx)", res3.success and res3.tx_hash == "")

    print(f"\n{'='*50}")
    print(f"  {passed} passed, {failed} failed")
    print(f"{'='*50}")
    if failed:
        raise SystemExit(1)
