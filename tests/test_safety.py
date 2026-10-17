import asyncio

from memebot.config import SafetyConfig
from memebot.data.solana_rpc import TOKEN_2022_PROGRAM
from memebot.safety import SafetyChecker, evaluate_goplus, evaluate_mint, evaluate_rugcheck

from .fakes import FakeHttp, clean_rugcheck

CFG = SafetyConfig()


def test_clean_rugcheck_passes_and_excludes_pool_from_concentration():
    reasons, warnings, data = evaluate_rugcheck(clean_rugcheck(), CFG)
    assert reasons == []
    assert data["top10_pct"] == 15.0  # the 22% pool vault is not counted


def test_rugcheck_flags():
    r = clean_rugcheck()
    r["mintAuthority"] = "Dev"
    r["risks"] = [{"name": "Single holder ownership", "level": "danger"}, {"name": "Low LP providers", "level": "warn"}]
    r["topHolders"].insert(1, {"address": "Whale", "owner": "WhaleOwner", "pct": 25.0, "insider": True})
    reasons, warnings, _ = evaluate_rugcheck(r, CFG)
    joined = " ".join(reasons)
    assert "mint authority" in joined and "danger" in joined and "top-10" in joined and "insider" in joined
    assert warnings == ["rugcheck warn: Low LP providers"]


def test_mint_checks_token2022_extensions():
    info = {
        "mintAuthority": None,
        "freezeAuthority": None,
        "program": TOKEN_2022_PROGRAM,
        "extensions": [{"extension": "permanentDelegate"}, {"extension": "transferFeeConfig", "state": {"newerTransferFee": {"transferFeeBasisPoints": 1000}}}],
    }
    reasons, _ = evaluate_mint(info, CFG)
    assert any("permanentDelegate" in r for r in reasons)
    assert any("transfer fee 10.0%" in r for r in reasons)
    assert evaluate_mint({"mintAuthority": None, "freezeAuthority": None, "program": "x"}, CFG) == ([], [])


def test_goplus_honeypot_and_tax():
    sec = {
        "is_honeypot": "1", "buy_tax": "0.01", "sell_tax": "0.25", "is_open_source": "1", "is_mintable": "0",
        "holder_count": "900",
        "holders": [{"address": "0xpool", "percent": "0.4"}, {"address": "0xa", "percent": "0.05"}],
        "lp_holders": [{"address": "0xpool", "percent": "1.0", "is_locked": 1}],
    }
    reasons, _, data = evaluate_goplus(sec, CFG)
    assert "honeypot" in reasons and any("sell tax 25.0%" in r for r in reasons)
    assert data["top10_pct"] == 5.0


def test_checker_fails_closed_on_api_error():
    http = FakeHttp([("GET", "rugcheck", lambda *a: (_ for _ in ()).throw(RuntimeError("down")))])
    rep = asyncio.run(SafetyChecker(CFG, http).check("solana", "Mint"))
    assert not rep.ok and "unavailable" in rep.reasons[0]


def test_checker_solana_end_to_end():
    http = FakeHttp([("GET", "api.rugcheck.xyz/v1/tokens/Mint/report", clean_rugcheck())])
    rep = asyncio.run(SafetyChecker(CFG, http).check("solana", "Mint"))
    assert rep.ok, rep.reasons
