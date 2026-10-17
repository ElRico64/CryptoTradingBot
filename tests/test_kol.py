from memebot.config import KOLConfig, KolWallet
from memebot.data.solana_rpc import WSOL_MINT
from memebot.sources.kol import KOLTracker, KolTrade, parse_wallet_swaps

WALLET = "KoLWallet1111111111111111111111111111111111"
MINT = "MemeMint11111111111111111111111111111111111"


def tx(token_pre: float, token_post: float, sol_pre: int, sol_post: int, err=None) -> dict:
    return {
        "blockTime": 1_700_000_000,
        "transaction": {"signatures": ["sig1"], "message": {"accountKeys": [{"pubkey": WALLET, "signer": True}, {"pubkey": "Pool"}]}},
        "meta": {
            "err": err,
            "preBalances": [sol_pre, 0],
            "postBalances": [sol_post, 0],
            "preTokenBalances": [{"mint": MINT, "owner": WALLET, "uiTokenAmount": {"uiAmountString": str(token_pre)}}] if token_pre else [],
            "postTokenBalances": [{"mint": MINT, "owner": WALLET, "uiTokenAmount": {"uiAmountString": str(token_post)}}],
        },
    }


def test_parse_buy():
    trades = parse_wallet_swaps(tx(0, 5000, 10_000_000_000, 7_000_000_000), WALLET)
    assert len(trades) == 1
    t = trades[0]
    assert t.side == "buy" and t.mint == MINT and t.token_delta == 5000 and abs(t.sol_delta + 3.0) < 1e-9


def test_parse_sell_and_wsol_counts_as_sol():
    t = tx(5000, 0, 7_000_000_000, 7_000_000_000)
    t["meta"]["postTokenBalances"].append({"mint": WSOL_MINT, "owner": WALLET, "uiTokenAmount": {"uiAmountString": "4.0"}})
    trades = parse_wallet_swaps(t, WALLET)
    assert trades[0].side == "sell" and abs(trades[0].sol_delta - 4.0) < 1e-9


def test_failed_tx_ignored():
    assert parse_wallet_swaps(tx(0, 5000, 10, 5, err={"x": 1}), WALLET) == []


def test_consensus_and_sellers():
    cfg = KOLConfig(enabled=True, wallets=[KolWallet("a", "Alpha", 1.5), KolWallet("b", "Beta", 1.0)], min_buy_sol=0.5)
    k = KOLTracker(cfg, None)
    now = 1_700_000_000
    k.record(KolTrade("a", MINT, "buy", 100, -2.0, now - 60, "s1"))
    k.record(KolTrade("b", MINT, "buy", 100, -0.1, now - 50, "dust"))  # below min_buy_sol: ignored
    assert k.consensus(MINT, now) == (1.5, ["a"])
    k.record(KolTrade("b", MINT, "buy", 100, -1.0, now - 40, "s2"))
    assert k.consensus(MINT, now)[0] == 2.5
    k.record(KolTrade("a", MINT, "sell", -100, 3.0, now - 10, "s3"))
    assert k.consensus(MINT, now) == (1.0, ["b"])
    assert k.sellers(MINT, ["a", "b"], since=now - 100) == 1
    assert k.consensus(MINT, now + 3600)[0] == 0  # outside window
