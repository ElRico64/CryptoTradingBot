"""Live executors against mocked APIs: request shapes, local signing, balance-based fills."""

import asyncio
import base64

import pytest
from eth_account import Account
from solders.hash import Hash
from solders.instruction import AccountMeta, Instruction
from solders.keypair import Keypair
from solders.message import MessageV0, to_bytes_versioned
from solders.pubkey import Pubkey
from solders.signature import Signature
from solders.transaction import VersionedTransaction

from memebot.config import ExecutionConfig
from memebot.data.solana_rpc import WSOL_MINT
from memebot.execution.base import ExecutionError
from memebot.execution.evm import NATIVE, EvmRpc, ZeroExExecutor
from memebot.execution.jupiter import JupiterExecutor, load_keypair

from .fakes import FakeHttp

MINT = "MemeMint11111111111111111111111111111111111"


class FakeSolRPC:
    def __init__(self, kp: Keypair, msg: MessageV0):
        self.kp, self.msg = kp, msg
        self.sol = 10 * 10**9
        self.tokens = 0
        self.sent: list[str] = []
        self.on_send = None

    async def get_balance_lamports(self, owner):
        return self.sol

    async def get_token_balance(self, owner, mint):
        return self.tokens, 6, self.tokens / 1e6

    async def send_transaction(self, tx_b64, skip_preflight=True):
        tx = VersionedTransaction.from_bytes(base64.b64decode(tx_b64))
        assert tx.signatures[0] == self.kp.sign_message(to_bytes_versioned(self.msg))  # signed locally, correctly
        self.sent.append(tx_b64)
        self.on_send()
        return "sig%d" % len(self.sent)

    async def get_signature_status(self, sig):
        return {"confirmationStatus": "confirmed", "err": None}


def unsigned_tx(kp: Keypair) -> tuple[str, MessageV0]:
    ix = Instruction(Pubkey.default(), b"", [AccountMeta(kp.pubkey(), True, True)])
    msg = MessageV0.try_compile(kp.pubkey(), [ix], [], Hash.default())
    tx = VersionedTransaction.populate(msg, [Signature.default()])
    return base64.b64encode(bytes(tx)).decode(), msg


def test_load_keypair_formats():
    kp = Keypair()
    assert load_keypair(str(kp)).pubkey() == kp.pubkey()
    assert load_keypair(str(list(bytes(kp)))).pubkey() == kp.pubkey()


def test_jupiter_buy_and_sell():
    kp = Keypair()
    tx_b64, msg = unsigned_tx(kp)
    rpc = FakeSolRPC(kp, msg)
    quotes = []

    def quote(url, params, body):
        quotes.append(params)
        if params["inputMint"] == WSOL_MINT:
            return {"inAmount": params["amount"], "outAmount": "5000000000", "priceImpactPct": "0.01"}
        return {"inAmount": params["amount"], "outAmount": "900000000", "priceImpactPct": "0.01"}

    http = FakeHttp([
        ("GET", "/price/v3", {WSOL_MINT: {"usdPrice": 200.0}}),
        ("GET", "/swap/v1/quote", quote),
        ("POST", "/swap/v1/swap", lambda u, p, body: {"swapTransaction": tx_b64, "lastValidBlockHeight": 1}),
    ])
    ex = JupiterExecutor(http, rpc, kp, ExecutionConfig())

    def after_buy():
        rpc.sol -= 501_000_000  # 0.5 SOL + fees
        rpc.tokens += 5_000_000_000

    rpc.on_send = after_buy
    fill = asyncio.run(ex.buy("solana", MINT, 100.0))
    assert quotes[0]["amount"] == str(500_000_000) and quotes[0]["outputMint"] == MINT
    assert fill.qty == pytest.approx(5000) and fill.usd == pytest.approx(100.2)

    def after_sell():
        rpc.sol += 450_000_000
        rpc.tokens -= 2_500_000_000

    rpc.on_send = after_sell
    sell = asyncio.run(ex.sell("solana", MINT, 0.5))
    assert quotes[-1]["amount"] == str(2_500_000_000) and quotes[-1]["outputMint"] == WSOL_MINT
    assert sell.qty == pytest.approx(2500) and sell.usd == pytest.approx(90.0)
    loss = asyncio.run(ex.roundtrip_loss_pct("solana", MINT, 100.0))
    assert loss == pytest.approx(100 * (1 - 900_000_000 / 500_000_000))  # mocked sell quote returns more than spent


def test_jupiter_rejects_high_impact():
    kp = Keypair()
    tx_b64, msg = unsigned_tx(kp)
    http = FakeHttp([
        ("GET", "/price/v3", {WSOL_MINT: {"usdPrice": 200.0}}),
        ("GET", "/swap/v1/quote", {"outAmount": "1", "priceImpactPct": "0.2"}),
    ])
    with pytest.raises(ExecutionError, match="impact"):
        asyncio.run(JupiterExecutor(http, FakeSolRPC(kp, msg), kp, ExecutionConfig()).buy("solana", MINT, 100))


class FakeChain:
    """Enough of an EVM node + 0x API to exercise buy / approve / sell."""

    def __init__(self, owner: str):
        self.owner = owner
        self.native = 10**18
        self.token = 0
        self.allowance = 0
        self.sent = []

    def rpc(self, url, params, body):
        m, p = body["method"], body["params"]
        result = {
            "eth_getBalance": lambda: hex(self.native),
            "eth_call": lambda: hex(self.token) if p[0]["data"].startswith("0x70a08231") else hex(18),
            "eth_getTransactionCount": lambda: hex(len(self.sent)),
            "eth_gasPrice": lambda: hex(10**9),
            "eth_sendRawTransaction": lambda: self._send(p[0]),
            "eth_getTransactionReceipt": lambda: {"status": "0x1"},
        }[m]()
        return {"jsonrpc": "2.0", "id": body["id"], "result": result}

    def _send(self, raw):
        tx = Account.recover_transaction(raw)
        assert tx == self.owner
        kind = self.pending.pop(0)
        self.sent.append(kind)
        if kind == "buy":
            self.native -= 5 * 10**16
            self.token += 1000 * 10**18
        elif kind == "approve":
            self.allowance = 2**256 - 1
        elif kind == "sell":
            self.native += 4 * 10**16
            self.token -= 1000 * 10**18
        return "0x" + "ab" * 32

    def quote(self, url, params, body):
        assert params["taker"] == self.owner and params["chainId"] == 8453
        selling_native = params["sellToken"] == NATIVE
        q = {
            "liquidityAvailable": True,
            "buyAmount": str(1000 * 10**18) if selling_native else str(4 * 10**16),
            "transaction": {"to": "0x" + "11" * 20, "data": "0xdeadbeef", "value": params["sellAmount"] if selling_native else "0", "gas": "200000"},
            "issues": {},
        }
        if not selling_native and self.allowance < int(params["sellAmount"]):
            q["issues"]["allowance"] = {"actual": "0", "spender": "0x" + "22" * 20}
        return q


def test_zeroex_buy_then_sell_with_approval():
    acct = Account.create()
    chain = FakeChain(acct.address)
    http = FakeHttp([("GET", "api.0x.org", chain.quote), ("POST", "rpc.example", chain.rpc)])

    async def native_price(c):
        return 2000.0

    ex = ZeroExExecutor(http, "base", EvmRpc(http, "https://rpc.example"), acct.key.hex(), "k", ExecutionConfig(), native_price)
    chain.pending = ["buy"]
    fill = asyncio.run(ex.buy("base", "0x" + "33" * 20, 100.0))
    assert fill.qty == pytest.approx(1000) and fill.usd == pytest.approx(100.0)
    chain.pending = ["approve", "sell"]
    sell = asyncio.run(ex.sell("base", "0x" + "33" * 20, 1.0))
    assert chain.sent == ["buy", "approve", "sell"]
    assert sell.qty == pytest.approx(1000) and sell.usd == pytest.approx(80.0)
