"""Minimal Solana JSON-RPC client (works with any provider: Helius, Triton, QuickNode, public)."""

from __future__ import annotations

import itertools
from typing import Any

from ..util import safe_float
from .http import Http

WSOL_MINT = "So11111111111111111111111111111111111111112"
USDC_MINT = "EPjFWrQ8GzmNJcPXWYnmn3sWWHmDcXP1dGHqeyZJRfo8"
USDT_MINT = "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"
TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
TOKEN_2022_PROGRAM = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"


class RpcError(Exception):
    pass


class SolanaRPC:
    def __init__(self, http: Http, url: str):
        self.http = http
        self.url = url
        self._ids = itertools.count(1)

    async def call(self, method: str, params: list | None = None) -> Any:
        payload = {"jsonrpc": "2.0", "id": next(self._ids), "method": method, "params": params or []}
        resp = await self.http.post_json(self.url, payload)
        if not isinstance(resp, dict):
            raise RpcError(f"{method}: bad response")
        if resp.get("error"):
            raise RpcError(f"{method}: {resp['error']}")
        return resp.get("result")

    async def get_balance_lamports(self, owner: str) -> int:
        res = await self.call("getBalance", [owner, {"commitment": "confirmed"}])
        return int((res or {}).get("value", 0))

    async def get_mint_info(self, mint: str) -> dict:
        """Parsed mint account: mintAuthority, freezeAuthority, decimals, supply, extensions, program."""
        res = await self.call("getAccountInfo", [mint, {"encoding": "jsonParsed", "commitment": "confirmed"}])
        value = (res or {}).get("value")
        if not value:
            raise RpcError(f"mint {mint} not found")
        parsed = (value.get("data") or {}).get("parsed") or {}
        info = dict(parsed.get("info") or {})
        info["program"] = value.get("owner", "")
        return info

    async def get_token_balance(self, owner: str, mint: str) -> tuple[int, int, float]:
        """(raw amount, decimals, ui amount) summed over all of owner's accounts for this mint."""
        res = await self.call(
            "getTokenAccountsByOwner",
            [owner, {"mint": mint}, {"encoding": "jsonParsed", "commitment": "confirmed"}],
        )
        raw, decimals, ui = 0, 0, 0.0
        for acc in (res or {}).get("value") or []:
            amt = acc["account"]["data"]["parsed"]["info"]["tokenAmount"]
            raw += int(amt["amount"])
            decimals = int(amt["decimals"])
            ui += safe_float(amt.get("uiAmountString") or amt.get("uiAmount"))
        return raw, decimals, ui

    async def send_transaction(self, tx_b64: str, skip_preflight: bool = True) -> str:
        return await self.call(
            "sendTransaction",
            [tx_b64, {"encoding": "base64", "skipPreflight": skip_preflight, "maxRetries": 3, "preflightCommitment": "confirmed"}],
        )

    async def get_signature_status(self, signature: str) -> dict | None:
        res = await self.call("getSignatureStatuses", [[signature], {"searchTransactionHistory": False}])
        values = (res or {}).get("value") or [None]
        return values[0]

    async def get_signatures(self, address: str, limit: int = 15) -> list[dict]:
        return await self.call("getSignaturesForAddress", [address, {"limit": limit, "commitment": "confirmed"}]) or []

    async def get_transaction(self, signature: str) -> dict | None:
        return await self.call(
            "getTransaction",
            [signature, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0, "commitment": "confirmed"}],
        )
