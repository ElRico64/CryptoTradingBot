"""Live Solana execution through the Jupiter swap aggregator, signing locally with your key.

The private key never leaves this process: Jupiter returns an unsigned transaction, we sign it
with solders and submit it to your own RPC. Fills are measured from actual wallet balance
changes (SOL and token), so recorded P&L includes fees, priority fees and real slippage.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import time

from solders.keypair import Keypair
from solders.transaction import VersionedTransaction

from ..config import ExecutionConfig
from ..data.http import Http, HttpError
from ..data.solana_rpc import WSOL_MINT, SolanaRPC
from ..models import Fill
from ..util import safe_float
from .base import ExecutionError, Executor

log = logging.getLogger(__name__)

LAMPORTS = 1_000_000_000
FEE_RESERVE_LAMPORTS = 20_000_000  # keep 0.02 SOL for fees and token-account rent


def load_keypair(secret: str) -> Keypair:
    secret = secret.strip()
    if secret.startswith("["):
        return Keypair.from_bytes(bytes(json.loads(secret)))  # solana-keygen JSON array
    return Keypair.from_base58_string(secret)  # Phantom / Axiom style export


class JupiterExecutor(Executor):
    def __init__(self, http: Http, rpc: SolanaRPC, keypair: Keypair, cfg: ExecutionConfig, api_key: str = ""):
        self.http = http
        self.rpc = rpc
        self.kp = keypair
        self.owner = str(keypair.pubkey())
        self.cfg = cfg
        self.base = "https://api.jup.ag" if api_key else cfg.jupiter_base_url.rstrip("/")
        self.headers = {"x-api-key": api_key} if api_key else None
        self._lock = asyncio.Lock()  # balance-delta accounting needs one swap at a time
        self._sol_price = (0.0, 0.0)

    async def sol_price(self) -> float:
        price, ts = self._sol_price
        if time.time() - ts < 30 and price > 0:
            return price
        data = await self.http.get_json(f"{self.base}/price/v3", params={"ids": WSOL_MINT}, headers=self.headers)
        price = safe_float(((data or {}).get(WSOL_MINT) or {}).get("usdPrice"))
        if price <= 0:
            raise ExecutionError("could not fetch SOL price")
        self._sol_price = (price, time.time())
        return price

    async def quote(self, input_mint: str, output_mint: str, amount: int, slippage_bps: int) -> dict:
        try:
            q = await self.http.get_json(
                f"{self.base}/swap/v1/quote",
                params={
                    "inputMint": input_mint,
                    "outputMint": output_mint,
                    "amount": str(amount),
                    "slippageBps": slippage_bps,
                    "restrictIntermediateTokens": "true",
                },
                headers=self.headers,
            )
        except HttpError as exc:
            raise ExecutionError(f"quote failed: {exc}") from exc
        if not q or "outAmount" not in q:
            raise ExecutionError(f"no route: {q}")
        return q

    async def _swap(self, quote: dict) -> str:
        body = {
            "quoteResponse": quote,
            "userPublicKey": self.owner,
            "wrapAndUnwrapSol": True,
            "dynamicComputeUnitLimit": True,
            "prioritizationFeeLamports": {
                "priorityLevelWithMaxLamports": {
                    "maxLamports": self.cfg.priority_fee_max_lamports,
                    "priorityLevel": self.cfg.priority_level,
                }
            },
        }
        try:
            resp = await self.http.post_json(f"{self.base}/swap/v1/swap", body, headers=self.headers)
        except HttpError as exc:
            raise ExecutionError(f"swap build failed: {exc}") from exc
        unsigned = VersionedTransaction.from_bytes(base64.b64decode(resp["swapTransaction"]))
        signed = VersionedTransaction(unsigned.message, [self.kp])
        sig = await self.rpc.send_transaction(base64.b64encode(bytes(signed)).decode(), self.cfg.skip_preflight)
        await self._confirm(sig)
        return sig

    async def _confirm(self, sig: str) -> None:
        deadline = time.time() + self.cfg.confirm_timeout_sec
        while time.time() < deadline:
            status = await self.rpc.get_signature_status(sig)
            if status:
                if status.get("err"):
                    raise ExecutionError(f"transaction {sig} failed: {status['err']}")
                if status.get("confirmationStatus") in ("confirmed", "finalized"):
                    return
            await asyncio.sleep(1.0)
        raise ExecutionError(f"transaction {sig} not confirmed in {self.cfg.confirm_timeout_sec:.0f}s")

    async def _wait_token_change(self, mint: str, before: int, tries: int = 8) -> tuple[int, int, float]:
        bal = await self.rpc.get_token_balance(self.owner, mint)
        for _ in range(tries):
            if bal[0] != before:
                break
            await asyncio.sleep(0.75)
            bal = await self.rpc.get_token_balance(self.owner, mint)
        return bal

    async def buy(self, chain: str, token: str, usd: float) -> Fill:
        async with self._lock:
            sol_px = await self.sol_price()
            lamports = int(usd / sol_px * LAMPORTS)
            sol_before = await self.rpc.get_balance_lamports(self.owner)
            if lamports > sol_before - FEE_RESERVE_LAMPORTS:
                raise ExecutionError("insufficient SOL")
            q = await self.quote(WSOL_MINT, token, lamports, self.cfg.slippage_bps)
            impact = safe_float(q.get("priceImpactPct")) * 100
            if impact > self.cfg.max_price_impact_pct:
                raise ExecutionError(f"price impact {impact:.1f}% too high")
            tok_before = await self.rpc.get_token_balance(self.owner, token)
            sig = await self._swap(q)
            tok_after = await self._wait_token_change(token, tok_before[0])
            sol_after = await self.rpc.get_balance_lamports(self.owner)
            qty = tok_after[2] - tok_before[2]
            spent_usd = (sol_before - sol_after) / LAMPORTS * sol_px
            if qty <= 0:
                raise ExecutionError(f"buy {sig} confirmed but no tokens received")
            log.info("BUY %s: %.4f SOL -> %.2f tokens (%s)", token, (sol_before - sol_after) / LAMPORTS, qty, sig)
            return Fill(qty=qty, usd=spent_usd, price=spent_usd / qty, tx=sig)

    async def sell(self, chain: str, token: str, fraction: float) -> Fill:
        async with self._lock:
            raw, _decimals, ui = await self.rpc.get_token_balance(self.owner, token)
            if raw <= 0:
                raise ExecutionError("no token balance to sell")
            amount = raw if fraction >= 0.999 else int(raw * fraction)
            sol_px = await self.sol_price()
            sol_before = await self.rpc.get_balance_lamports(self.owner)
            # Exits must go through: retry with progressively wider slippage.
            slippages = [self.cfg.slippage_bps, int(self.cfg.slippage_bps * 1.8), self.cfg.sell_slippage_bps_max]
            last_exc: Exception | None = None
            sig = ""
            for bps in slippages:
                bps = min(bps, self.cfg.sell_slippage_bps_max)
                try:
                    q = await self.quote(token, WSOL_MINT, amount, bps)
                    sig = await self._swap(q)
                    break
                except ExecutionError as exc:
                    log.warning("sell attempt at %d bps failed: %s", bps, exc)
                    last_exc = exc
                    # A "timed out" transaction may still have landed: never sell twice.
                    if (await self.rpc.get_token_balance(self.owner, token))[0] < raw:
                        sig = "unconfirmed-but-landed"
                        break
            else:
                raise ExecutionError(f"sell failed after retries: {last_exc}")
            tok_after = await self._wait_token_change(token, raw)
            sol_after = await self.rpc.get_balance_lamports(self.owner)
            sold = ui - tok_after[2]
            usd = (sol_after - sol_before) / LAMPORTS * sol_px
            log.info("SELL %s: %.2f tokens -> %.4f SOL (%s)", token, sold, (sol_after - sol_before) / LAMPORTS, sig)
            return Fill(qty=sold, usd=usd, price=usd / sold if sold > 0 else 0.0, tx=sig)

    async def roundtrip_loss_pct(self, chain: str, token: str, usd: float) -> float:
        sol_px = await self.sol_price()
        lamports = int(usd / sol_px * LAMPORTS)
        buy_q = await self.quote(WSOL_MINT, token, lamports, self.cfg.slippage_bps)
        sell_q = await self.quote(token, WSOL_MINT, int(buy_q["outAmount"]), self.cfg.slippage_bps)
        back = int(sell_q["outAmount"])
        return 100 * (1 - back / lamports) if lamports else 100.0

    async def cash_usd(self, chain: str) -> float:
        lamports = await self.rpc.get_balance_lamports(self.owner)
        return max(0, lamports - FEE_RESERVE_LAMPORTS) / LAMPORTS * await self.sol_price()
