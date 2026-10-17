"""Live EVM execution (Base, BSC, Ethereum) through the 0x Swap API v2, signing locally.

Buys spend the chain's native coin (ETH/BNB). Sells approve the 0x AllowanceHolder for the
token once, then swap back to native. Fills are measured from real balance changes.
"""

from __future__ import annotations

import asyncio
import itertools
import logging
import time
from typing import Awaitable, Callable

from eth_account import Account
from eth_utils import to_checksum_address

from ..config import ExecutionConfig
from ..data.http import Http, HttpError
from ..models import Fill
from ..util import safe_float
from .base import ExecutionError, Executor

log = logging.getLogger(__name__)

ZEROX_QUOTE = "https://api.0x.org/swap/allowance-holder/quote"
NATIVE = "0xEeeeeEeeeEeEeeEeEeEeeEEEeeeeEeeeeeeeEEeE"
CHAIN_IDS = {"ethereum": 1, "bsc": 56, "base": 8453}
WRAPPED_NATIVE = {
    "ethereum": "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2",
    "bsc": "0xbb4CdB9CBd36B01bD1cBaEBF2De08d9173bc095c",
    "base": "0x4200000000000000000000000000000000000006",
}
GAS_RESERVE_WEI = {"ethereum": 10**16, "bsc": 3 * 10**15, "base": 10**15}
MAX_UINT = 2**256 - 1


def _pad(hex_or_int: str | int) -> str:
    if isinstance(hex_or_int, int):
        return format(hex_or_int, "064x")
    return hex_or_int.lower().replace("0x", "").rjust(64, "0")


class EvmRpc:
    def __init__(self, http: Http, url: str):
        self.http = http
        self.url = url
        self._ids = itertools.count(1)

    async def call(self, method: str, params: list) -> object:
        resp = await self.http.post_json(self.url, {"jsonrpc": "2.0", "id": next(self._ids), "method": method, "params": params})
        if not isinstance(resp, dict) or resp.get("error"):
            raise ExecutionError(f"{method}: {resp.get('error') if isinstance(resp, dict) else resp}")
        return resp.get("result")

    async def native_balance(self, addr: str) -> int:
        return int(await self.call("eth_getBalance", [addr, "latest"]), 16)

    async def erc20_balance(self, token: str, owner: str) -> int:
        res = await self.call("eth_call", [{"to": token, "data": "0x70a08231" + _pad(owner)}, "latest"])
        return int(res or "0x0", 16)

    async def decimals(self, token: str) -> int:
        res = await self.call("eth_call", [{"to": token, "data": "0x313ce567"}, "latest"])
        return int(res or "0x12", 16)

    async def nonce(self, addr: str) -> int:
        return int(await self.call("eth_getTransactionCount", [addr, "pending"]), 16)

    async def gas_price(self) -> int:
        return int(await self.call("eth_gasPrice", []), 16)

    async def send_raw(self, raw_hex: str) -> str:
        return str(await self.call("eth_sendRawTransaction", [raw_hex]))

    async def receipt(self, tx_hash: str) -> dict | None:
        return await self.call("eth_getTransactionReceipt", [tx_hash])  # type: ignore[return-value]


class ZeroExExecutor(Executor):
    def __init__(
        self,
        http: Http,
        chain: str,
        rpc: EvmRpc,
        private_key: str,
        api_key: str,
        cfg: ExecutionConfig,
        native_price: Callable[[str], Awaitable[float]],
    ):
        if not api_key:
            raise ValueError("ZEROX_API_KEY is required for live EVM trading")
        self.http = http
        self.chain = chain
        self.chain_id = CHAIN_IDS[chain]
        self.rpc = rpc
        self.acct = Account.from_key(private_key)
        self.owner = self.acct.address
        self.cfg = cfg
        self.headers = {"0x-api-key": api_key, "0x-version": "v2"}
        self.native_price = native_price
        self._lock = asyncio.Lock()
        self._decimals: dict[str, int] = {}

    async def _quote(self, sell_token: str, buy_token: str, amount: int, slippage_bps: int) -> dict:
        try:
            q = await self.http.get_json(
                ZEROX_QUOTE,
                params={
                    "chainId": self.chain_id,
                    "sellToken": sell_token,
                    "buyToken": buy_token,
                    "sellAmount": str(amount),
                    "taker": self.owner,
                    "slippageBps": slippage_bps,
                },
                headers=self.headers,
            )
        except HttpError as exc:
            raise ExecutionError(f"0x quote failed: {exc}") from exc
        if not q or not q.get("liquidityAvailable", False) or not q.get("transaction"):
            raise ExecutionError(f"0x: no liquidity/route: {q}")
        return q

    async def _send(self, to: str, data: str, value: int, gas: int | None = None) -> str:
        tx = {
            "to": to_checksum_address(to),
            "data": data,
            "value": value,
            "gas": gas or 300_000,
            "gasPrice": int(await self.rpc.gas_price() * 1.15),
            "nonce": await self.rpc.nonce(self.owner),
            "chainId": self.chain_id,
        }
        signed = self.acct.sign_transaction(tx)
        tx_hash = await self.rpc.send_raw("0x" + signed.raw_transaction.hex().removeprefix("0x"))
        deadline = time.time() + self.cfg.confirm_timeout_sec * 2
        while time.time() < deadline:
            rcpt = await self.rpc.receipt(tx_hash)
            if rcpt:
                if int(rcpt.get("status", "0x0"), 16) != 1:
                    raise ExecutionError(f"transaction {tx_hash} reverted")
                return tx_hash
            await asyncio.sleep(1.5)
        raise ExecutionError(f"transaction {tx_hash} not mined in time")

    async def _execute_quote(self, q: dict) -> str:
        t = q["transaction"]
        gas = int(int(t.get("gas") or 0) * 1.25) or None
        return await self._send(t["to"], t["data"], int(t.get("value") or 0), gas)

    async def _ensure_allowance(self, token: str, amount: int, q: dict) -> dict:
        allowance = (q.get("issues") or {}).get("allowance")
        if allowance and int(allowance.get("actual") or 0) < amount:
            spender = allowance["spender"]
            log.info("Approving %s for 0x AllowanceHolder %s", token, spender)
            await self._send(token, "0x095ea7b3" + _pad(spender) + _pad(MAX_UINT), 0, 100_000)
            return await self._quote(token, NATIVE, amount, int(q.get("_slippage", self.cfg.slippage_bps)))
        return q

    async def _token_decimals(self, token: str) -> int:
        if token not in self._decimals:
            self._decimals[token] = await self.rpc.decimals(token)
        return self._decimals[token]

    async def buy(self, chain: str, token: str, usd: float) -> Fill:
        async with self._lock:
            px = await self.native_price(chain)
            wei = int(usd / px * 1e18)
            before_native = await self.rpc.native_balance(self.owner)
            if wei > before_native - GAS_RESERVE_WEI[chain]:
                raise ExecutionError("insufficient native balance")
            q = await self._quote(NATIVE, token, wei, self.cfg.slippage_bps)
            before_tok = await self.rpc.erc20_balance(token, self.owner)
            tx = await self._execute_quote(q)
            after_tok = await self.rpc.erc20_balance(token, self.owner)
            after_native = await self.rpc.native_balance(self.owner)
            dec = await self._token_decimals(token)
            qty = (after_tok - before_tok) / 10**dec
            spent = (before_native - after_native) / 1e18 * px
            if qty <= 0:
                raise ExecutionError(f"buy {tx} mined but no tokens received")
            return Fill(qty=qty, usd=spent, price=spent / qty, tx=tx)

    async def sell(self, chain: str, token: str, fraction: float) -> Fill:
        async with self._lock:
            raw = await self.rpc.erc20_balance(token, self.owner)
            if raw <= 0:
                raise ExecutionError("no token balance to sell")
            amount = raw if fraction >= 0.999 else int(raw * fraction)
            px = await self.native_price(chain)
            before_native = await self.rpc.native_balance(self.owner)
            last_exc: Exception | None = None
            tx = ""
            for bps in (self.cfg.slippage_bps, int(self.cfg.slippage_bps * 1.8), self.cfg.sell_slippage_bps_max):
                bps = min(bps, self.cfg.sell_slippage_bps_max)
                try:
                    q = await self._quote(token, NATIVE, amount, bps)
                    q["_slippage"] = bps
                    q = await self._ensure_allowance(token, amount, q)
                    tx = await self._execute_quote(q)
                    break
                except ExecutionError as exc:
                    log.warning("EVM sell at %d bps failed: %s", bps, exc)
                    last_exc = exc
                    if await self.rpc.erc20_balance(token, self.owner) < raw:
                        tx = "unconfirmed-but-landed"
                        break
            else:
                raise ExecutionError(f"sell failed after retries: {last_exc}")
            after_raw = await self.rpc.erc20_balance(token, self.owner)
            after_native = await self.rpc.native_balance(self.owner)
            dec = await self._token_decimals(token)
            sold = (raw - after_raw) / 10**dec
            usd = (after_native - before_native) / 1e18 * px
            return Fill(qty=sold, usd=usd, price=usd / sold if sold > 0 else 0.0, tx=tx)

    async def roundtrip_loss_pct(self, chain: str, token: str, usd: float) -> float:
        px = await self.native_price(chain)
        wei = int(usd / px * 1e18)
        buy_q = await self._quote(NATIVE, token, wei, self.cfg.slippage_bps)
        sell_q = await self._quote(token, NATIVE, int(buy_q["buyAmount"]), self.cfg.slippage_bps)
        return 100 * (1 - int(sell_q["buyAmount"]) / wei) if wei else 100.0

    async def cash_usd(self, chain: str) -> float:
        bal = await self.rpc.native_balance(self.owner)
        return max(0, bal - GAS_RESERVE_WEI[chain]) / 1e18 * await self.native_price(chain)


def native_price_from_dexscreener(dex) -> Callable[[str], Awaitable[float]]:
    cache: dict[str, tuple[float, float]] = {}

    async def price(chain: str) -> float:
        hit = cache.get(chain)
        if hit and time.time() - hit[1] < 30:
            return hit[0]
        pair = await dex.best_pair(chain, WRAPPED_NATIVE[chain])
        px = safe_float(pair.price_usd if pair else 0)
        if px <= 0:
            raise ExecutionError(f"no native price for {chain}")
        cache[chain] = (px, time.time())
        return px

    return price
