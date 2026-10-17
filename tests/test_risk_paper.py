import asyncio

import pytest

from memebot.config import RiskConfig, ScoringConfig
from memebot.execution.base import ExecutionError
from memebot.execution.paper import PaperExecutor
from memebot.models import Position
from memebot.risk import RiskManager

from .fakes import make_pair

NOW = 1_700_000_000.0


def open_pos(narrative: str = "") -> Position:
    return Position("solana", "M", "M", "P", 1.0, NOW, 40, 40, 1e5, 80, narrative=narrative)


def test_sizing_scales_with_conviction_and_caps():
    rm = RiskManager(RiskConfig(position_pct=4, max_position_usd=500, max_pool_liquidity_pct=1.5), ScoringConfig())
    normal = rm.position_size(78, equity=1000, cash=1000, liquidity_usd=1e6)
    strong = rm.position_size(92, equity=1000, cash=1000, liquidity_usd=1e6)
    assert normal == pytest.approx(40)
    assert strong == pytest.approx(60)
    assert rm.position_size(92, 1000, 1000, liquidity_usd=500) == 0.0  # 1.5% of a $500 pool is dust
    assert rm.position_size(92, 1000, cash=20, liquidity_usd=1e6) == pytest.approx(15)


def test_limits():
    rm = RiskManager(RiskConfig(max_open_positions=2, max_positions_per_narrative=1), ScoringConfig())
    assert rm.can_open([], 1000, 1000, now=NOW)[0]
    assert not rm.can_open([open_pos(), open_pos()], 1000, 1000, now=NOW)[0]
    ok, why = rm.can_open([open_pos("SUPERINTELLIGENCE")], 1000, 1000, narrative="SUPERINTELLIGENCE", now=NOW)
    assert not ok and "narrative" in why
    assert not rm.can_open([], 1000, cash=3, now=NOW)[0]


def test_daily_kill_switch():
    rm = RiskManager(RiskConfig(max_daily_loss_pct=10, loss_streak_pause=99), ScoringConfig())
    assert rm.can_open([], 1000, 1000, now=NOW)[0]
    rm.on_close(-60, NOW)
    assert rm.can_open([], 940, 940, now=NOW)[0]
    rm.on_close(-45, NOW)
    ok, why = rm.can_open([], 895, 895, now=NOW)
    assert not ok and "daily loss" in why
    assert rm.can_open([], 895, 895, now=NOW + 86400)[0]  # resets next UTC day


def test_loss_streak_pause():
    rm = RiskManager(RiskConfig(loss_streak_pause=3, loss_streak_pause_min=60, max_daily_loss_pct=99), ScoringConfig())
    rm.can_open([], 1000, 1000, now=NOW)
    for _ in range(3):
        rm.on_close(-1, NOW)
    assert not rm.can_open([], 1000, 1000, now=NOW + 60)[0]
    assert rm.can_open([], 1000, 1000, now=NOW + 3601)[0]


def test_paper_round_trip_costs_are_realistic():
    pair = make_pair(price=1.0, liquidity=100_000)

    async def src(chain, token):
        return pair

    ex = PaperExecutor(1000, src, fee_pct=1.0, extra_slippage_pct=1.0)

    async def go():
        buy = await ex.buy("solana", pair.token_address, 100)
        assert buy.price > 1.0  # impact + slippage + fee
        sell = await ex.sell("solana", pair.token_address, 0.5)
        rest = await ex.sell("solana", pair.token_address, 1.0)
        return buy, sell, rest

    buy, sell, rest = asyncio.run(go())
    assert 93 < sell.usd + rest.usd < 97  # ~4-5% round trip on a $100k pool
    assert ex.cash == pytest.approx(900 + sell.usd + rest.usd)
    loss = asyncio.run(ex.roundtrip_loss_pct("solana", pair.token_address, 100))
    assert 3 < loss < 6
    with pytest.raises(ExecutionError):
        asyncio.run(ex.sell("solana", pair.token_address, 1.0))


def test_paper_rugged_token_is_worthless():
    state = {"pair": make_pair(price=1.0)}

    async def src(chain, token):
        return state["pair"]

    ex = PaperExecutor(1000, src)
    asyncio.run(ex.buy("solana", "TokenMint1111111111111111111111111111111111", 100))
    state["pair"] = None
    fill = asyncio.run(ex.sell("solana", "TokenMint1111111111111111111111111111111111", 0.5))
    assert fill.usd == 0.0 and fill.qty > 0 and ex.holdings == {}
