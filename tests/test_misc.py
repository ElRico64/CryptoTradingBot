from pathlib import Path

import pytest

from memebot.app import LiveTradingNotAcknowledged, build_app
from memebot.backtest import load_csv, simulate
from memebot.config import Config, ExitConfig, Secrets, load_config
from memebot.data.dexscreener import parse_pair
from memebot.data.geckoterminal import parse_ohlcv, parse_pool_tokens

from .fakes import candles_from_closes, make_pair, pair_json

ROOT = Path(__file__).resolve().parent.parent


def test_example_config_loads_and_matches_defaults():
    cfg = load_config(ROOT / "config.example.yaml")
    assert cfg == Config()


def test_unknown_config_key_rejected(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text("exits:\n  stop_los_pct: 20\n")
    with pytest.raises(ValueError, match="stop_los_pct"):
        load_config(p)


def test_bad_take_profit_order_rejected(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text("exits:\n  take_profits:\n    - {multiple: 3, sell_fraction: 0.5}\n    - {multiple: 2, sell_fraction: 0.5}\n")
    with pytest.raises(ValueError, match="increasing"):
        load_config(p)


def test_live_mode_requires_explicit_ack(tmp_path):
    cfg = Config()
    cfg.general.mode = "live"
    cfg.general.db_path = str(tmp_path / "x.db")
    with pytest.raises(LiveTradingNotAcknowledged):
        build_app(cfg, Secrets())


def test_paper_app_builds(tmp_path):
    cfg = Config()
    cfg.general.db_path = str(tmp_path / "x.db")
    app = build_app(cfg, Secrets())
    names = {type(s).__name__ for s in app.sources}
    assert {"DexScreenerScanner", "GeckoTerminalScanner", "PumpPortalStream", "NarrativeEngine"} <= names
    app.journal.close()


def test_parse_dexscreener_pair_roundtrip():
    p = make_pair()
    q = parse_pair(pair_json(p))
    assert q.price_usd == p.price_usd and q.liquidity_usd == p.liquidity_usd and q.txns == p.txns
    assert q.socials == 2 and q.websites == 1
    assert parse_pair({"bad": 1}) is None


def test_parse_geckoterminal():
    data = {"data": {"attributes": {"ohlcv_list": [[120, 2, 3, 1, 2.5, 10], [60, 1, 2, 1, 2, 5]]}}}
    candles = parse_ohlcv(data)
    assert [c.ts for c in candles] == [60, 120]
    pools = {"data": [{"relationships": {"base_token": {"data": {"id": "solana_ABC"}}}}]}
    assert parse_pool_tokens(pools, "solana") == ["ABC"]


def test_backtest_runner_and_moon_bag():
    closes = [1.0, 1.3, 1.8, 2.2, 3.0, 4.0, 5.5, 6.0, 4.2, 4.0]
    r = simulate(candles_from_closes(closes), ExitConfig(), fee_pct=0)
    assert "trailing" in r.exit_reason
    assert r.multiple > 2.5 and r.peak_multiple > 5


def test_backtest_rug():
    r = simulate(candles_from_closes([1.0, 1.1, 0.5, 0.2]), ExitConfig(), fee_pct=0)
    assert "stop loss" in r.exit_reason and r.multiple < 0.7


def test_load_csv(tmp_path):
    f = tmp_path / "c.csv"
    f.write_text("ts,open,high,low,close,volume\n120,2,3,1,2.5,10\n60,1,2,1,2,5\n")
    assert [c.ts for c in load_csv(f)] == [60, 120]
