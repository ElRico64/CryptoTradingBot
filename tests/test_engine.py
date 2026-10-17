"""End-to-end: discovery -> analysis -> paper entry -> take-profit -> trailing exit -> journal."""

import asyncio

import pytest

from memebot.config import Config
from memebot.data.dexscreener import DexScreener
from memebot.data.geckoterminal import GeckoTerminal
from memebot.engine import Engine
from memebot.execution.base import ExecutorRouter
from memebot.execution.paper import PaperExecutor
from memebot.journal import Journal
from memebot.models import Candidate
from memebot.notify import Notifier
from memebot.safety import SafetyChecker

from .fakes import FakeHttp, clean_rugcheck, make_pair, pair_json
from .test_analysis import _pullback_candles

MINT = "TokenMint1111111111111111111111111111111111"


def ohlcv_json(candles):
    return {"data": {"attributes": {"ohlcv_list": [[c.ts, c.o, c.h, c.l, c.c, c.v] for c in reversed(candles)]}}}


def build(tmp_path, rug=None):
    cfg = Config()
    cfg.general.db_path = str(tmp_path / "j.db")
    state = {"pair": make_pair(address=MINT, price=0.001)}
    http = FakeHttp([
        ("GET", "api.dexscreener.com/tokens/v1/solana/", lambda u, p, b: [pair_json(state["pair"])] if state["pair"] else []),
        ("GET", "api.rugcheck.xyz", rug or clean_rugcheck()),
        ("GET", "api.geckoterminal.com", ohlcv_json(_pullback_candles())),
    ])
    dex = DexScreener(http)
    paper = PaperExecutor(1000, dex.best_pair)
    journal = Journal(cfg.general.db_path)
    eng = Engine(cfg, journal, ExecutorRouter({"solana": paper}), dex, GeckoTerminal(http),
                 SafetyChecker(cfg.safety, http), Notifier(http))
    return eng, state, paper, journal


async def settle(eng):
    await eng.check_positions()
    if eng._tasks:
        await asyncio.gather(*list(eng._tasks))


def test_full_trade_lifecycle(tmp_path):
    eng, state, paper, journal = build(tmp_path)

    async def scenario():
        await eng.process(Candidate("solana", MINT, "kol", context={"kol_weight": 2.0, "kol_wallets": ["w1", "w2"]}))
        await eng.process(Candidate("solana", MINT, "gt_trending"))  # already holding: no double entry
        assert len(eng.open_positions()) == 1
        pos = eng.open_positions()[0]
        assert 30 < pos.cost_usd < 80 and paper.cash == pytest.approx(1000 - pos.cost_usd)

        state["pair"] = make_pair(address=MINT, price=pos.entry_price * 2.05)
        await settle(eng)
        assert pos.tp_hits == [2.0] and pos.remaining_fraction == pytest.approx(0.5)
        assert pos.realized_usd > pos.cost_usd * 0.9  # stake mostly recovered at the first target

        state["pair"] = make_pair(address=MINT, price=pos.entry_price * 3.2)
        await settle(eng)
        state["pair"] = make_pair(address=MINT, price=pos.entry_price * 2.0)  # >35% off a 3.2x peak
        await settle(eng)
        return pos

    pos = asyncio.run(scenario())
    assert pos.status == "closed" and "trailing" in pos.exit_reason
    assert pos.realized_usd > pos.cost_usd * 1.8
    assert paper.cash > 1000
    stats = journal.stats()
    assert stats["closed_trades"] == 1 and stats["win_rate"] == 1.0 and stats["hit_2x"] == 1
    rows = journal.training_rows()
    assert len(rows) == 1 and rows[0][2] > 1.8


def test_unsafe_token_is_never_bought(tmp_path):
    rug = clean_rugcheck()
    rug["freezeAuthority"] = "Dev"
    eng, _, paper, _ = build(tmp_path, rug)
    asyncio.run(eng.process(Candidate("solana", MINT, "kol", context={"kol_weight": 3.0})))
    assert eng.open_positions() == [] and paper.cash == 1000


def test_stop_loss_and_restart_recovery(tmp_path):
    eng, state, paper, journal = build(tmp_path)

    async def scenario():
        await eng.process(Candidate("solana", MINT, "kol", context={"kol_weight": 2.0}))
        pos = eng.open_positions()[0]
        # Restart: a fresh engine picks the position up from the journal.
        eng2 = Engine(eng.cfg, Journal(eng.cfg.general.db_path), eng.executor, eng.dex, eng.gecko, eng.safety, eng.notifier)
        assert [p.id for p in eng2.open_positions()] == [pos.id]
        state["pair"] = make_pair(address=MINT, price=pos.entry_price * 0.6)
        await settle(eng2)
        return eng2.positions[pos.id]

    pos = asyncio.run(scenario())
    assert pos.status == "closed" and "stop loss" in pos.exit_reason
    assert eng.risk is not None


def test_emit_dedupes_and_tracks_confluence(tmp_path):
    eng, _, _, _ = build(tmp_path)

    async def scenario():
        await eng.emit(Candidate("solana", MINT, "gt_trending"))
        await eng.emit(Candidate("solana", MINT, "gt_trending"))  # in flight: ignored
        await eng.emit(Candidate("base", "0xabc", "gt_trending"))  # chain not enabled
        return eng.queue.qsize(), eng.recent_mentions(f"solana:{MINT}")

    size, mentions = asyncio.run(scenario())
    assert size == 1 and set(mentions) == {"gt_trending"}
