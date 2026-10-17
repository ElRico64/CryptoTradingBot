from memebot.config import ExitConfig
from memebot.models import Position
from memebot.strategy.exits import ExitManager, MarketTick, apply_simulated_sell

T0 = 1_000_000.0


def pos(**kw) -> Position:
    base = dict(
        chain="solana", token_address="M", symbol="M", pair_address="P", entry_price=1.0, entry_ts=T0,
        cost_usd=100.0, qty=100.0, entry_liquidity_usd=100_000, score=80.0,
    )
    base.update(kw)
    return Position(**base)


def at(minutes: float) -> float:
    return T0 + minutes * 60


mgr = ExitManager(ExitConfig())


def test_hard_stop_loss():
    a = mgr.evaluate(pos(), MarketTick(0.69), at(2))
    assert a and a.fraction == 1.0 and "stop loss" in a.reason


def test_no_action_in_normal_range():
    assert mgr.evaluate(pos(), MarketTick(1.15), at(2)) is None


def test_first_take_profit_at_2x_sells_half():
    p = pos()
    a = mgr.evaluate(p, MarketTick(2.05), at(5))
    assert a and abs(a.fraction - 0.5) < 1e-9 and a.take_profit_levels == (2.0,)
    apply_simulated_sell(p, a, 2.05)
    assert p.tp_hits == [2.0]
    assert abs(p.realized_usd - 102.5) < 1e-9  # initial stake back
    assert mgr.evaluate(p, MarketTick(2.1), at(6)) is None  # level not re-triggered


def test_gap_through_multiple_levels_combines_sells():
    a = mgr.evaluate(pos(), MarketTick(5.5), at(5))
    assert a and abs(a.fraction - 0.75) < 1e-9 and a.take_profit_levels == (2.0, 5.0)


def test_breakeven_floor_after_tp1():
    p = pos(tp_hits=[2.0], qty=50.0, peak_price=2.0)
    a = mgr.evaluate(p, MarketTick(1.08), at(20))
    assert a and a.fraction == 1.0 and "floor" in a.reason


def test_trailing_stop_after_run():
    p = pos(tp_hits=[2.0], qty=50.0)
    assert mgr.evaluate(p, MarketTick(4.0), at(10)) is None  # sets peak 4x (30% tier)
    assert mgr.evaluate(p, MarketTick(2.9), at(11)) is None  # 27.5% off peak
    a = mgr.evaluate(p, MarketTick(2.75), at(12))  # 31% off peak
    assert a and "trailing" in a.reason


def test_trailing_protects_unrealised_gain_before_tp1():
    p = pos()
    assert mgr.evaluate(p, MarketTick(1.9), at(5)) is None
    a = mgr.evaluate(p, MarketTick(1.2), at(6))  # 37% off a 1.9x peak
    assert a and "trailing" in a.reason


def test_tighter_trail_on_big_winners():
    assert mgr.trail_drawdown_pct(1.2) is None
    assert mgr.trail_drawdown_pct(2.0) == 35.0
    assert mgr.trail_drawdown_pct(7.0) == 25.0
    assert mgr.trail_drawdown_pct(20.0) == 20.0


def test_time_stop_when_nothing_happens():
    p = pos()
    assert mgr.evaluate(p, MarketTick(1.05), at(29)) is None
    a = mgr.evaluate(p, MarketTick(1.05), at(31))
    assert a and "time stop" in a.reason


def test_time_stop_skipped_if_it_moved():
    p = pos(peak_price=1.4)
    assert mgr.evaluate(p, MarketTick(1.1), at(45)) is None


def test_liquidity_pull_overrides_everything():
    a = mgr.evaluate(pos(), MarketTick(3.0, liquidity_usd=40_000), at(5))
    assert a and a.fraction == 1.0 and "liquidity" in a.reason


def test_kol_exit_follows_smart_money():
    p = pos(kol_wallets=["w1", "w2"])
    a = mgr.evaluate(p, MarketTick(1.2, kol_sellers=1), at(5))
    assert a and a.fraction == 1.0
    p2 = pos(kol_wallets=["w1"], tp_hits=[2.0], qty=50.0, peak_price=2.2)
    a2 = mgr.evaluate(p2, MarketTick(2.1, kol_sellers=1), at(5))
    assert a2 and a2.fraction == 0.5


def test_flow_reversal_exit_on_loser():
    a = mgr.evaluate(pos(), MarketTick(0.9, buy_ratio_m5=0.2, txns_m5=60), at(5))
    assert a and "sellers" in a.reason
    assert mgr.evaluate(pos(), MarketTick(1.1, buy_ratio_m5=0.2, txns_m5=60), at(5)) is None


def test_max_hold():
    p = pos(tp_hits=[2.0], qty=50.0, peak_price=2.5)
    a = mgr.evaluate(p, MarketTick(2.4), at(24 * 60 + 1))
    assert a and "max hold" in a.reason


def test_full_exit_option():
    cfg = ExitConfig()
    cfg.take_profits[0].sell_fraction = 1.0
    a = ExitManager(cfg).evaluate(pos(), MarketTick(2.0), at(5))
    assert a and a.fraction == 1.0
