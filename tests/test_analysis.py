import numpy as np

from memebot.analysis.features import compute_features
from memebot.analysis.learner import LogisticModel, model_inputs
from memebot.analysis.patterns import analyze, rsi
from memebot.analysis.scorer import hard_filters, score_token
from memebot.config import FilterConfig, ScoringConfig
from memebot.models import SafetyReport

from .fakes import candles_from_closes, make_pair

SAFE = SafetyReport(ok=True, data={"top10_pct": 14.0, "risk_norm": 1, "holders": 2500})


def test_hard_filters_pass_healthy_pair():
    assert hard_filters(make_pair(), FilterConfig()) == []


def test_hard_filters_reject_thin_liquidity_and_old_pairs():
    reasons = hard_filters(make_pair(liquidity=5_000, age_min=60 * 24 * 10), FilterConfig())
    assert any("liquidity" in r for r in reasons)
    assert any("older" in r for r in reasons)


def test_hard_filters_reject_vertical_chart():
    assert any("vertical" in r for r in hard_filters(make_pair(pc_h1=1200), FilterConfig()))


def test_features():
    f = compute_features(make_pair(buys_m5=70, sells_m5=30, buys_h1=500, sells_h1=300))
    assert abs(f["buy_ratio_m5"] - 0.7) < 1e-9
    assert abs(f["txn_accel"] - 100 * 12 / 800) < 1e-9


def _pullback_candles():
    closes = [1.0] * 20 + list(np.linspace(1.0, 2.2, 15)) + list(np.linspace(2.15, 1.75, 8)) + [1.85]
    vols = [800] * 20 + [3000] * 15 + [1200] * 8 + [2500]
    return candles_from_closes(closes, vols)


def test_pattern_first_pullback_scores_well():
    r = analyze(_pullback_candles())
    assert "first_pullback" in r.tags
    assert r.score >= 65


def test_pattern_parabolic_is_penalised():
    closes = list(np.geomspace(1.0, 6.0, 40))
    r = analyze(candles_from_closes(closes, [1000] * 30 + [9000] * 10))
    assert "overextended" in r.tags
    assert r.score < 60


def test_pattern_dump_scores_low():
    closes = list(np.linspace(1.0, 2.0, 15)) + list(np.linspace(2.0, 0.8, 25))
    r = analyze(candles_from_closes(closes))
    assert r.score < 40


def test_pattern_insufficient_history_is_neutral():
    assert analyze(candles_from_closes([1, 1.1, 1.2])).score == 50.0


def test_rsi_bounds():
    assert rsi(np.linspace(1, 2, 30)) > 90
    assert rsi(np.linspace(2, 1, 30)) < 10


def test_strong_setup_enters_with_confluence():
    f = compute_features(make_pair())
    pattern = analyze(_pullback_candles())
    r = score_token(f, SAFE, pattern, kol_weight=2.0, narrative_match=0.0, cfg=ScoringConfig(), filters=FilterConfig())
    assert r.decision == "enter", r
    assert "smart_money" in r.confluence


def test_weak_flow_is_skipped():
    f = compute_features(make_pair(buys_m5=20, sells_m5=80, buys_h1=200, sells_h1=600, pc_h1=-20, pc_m5=-6))
    r = score_token(f, SAFE, None, 0.0, 0.0, ScoringConfig(), FilterConfig())
    assert r.decision == "skip"


def test_confluence_required_when_not_strong():
    cfg = ScoringConfig(entry_threshold=10, strong_threshold=101, min_confluence=5)
    f = compute_features(make_pair())
    r = score_token(f, SAFE, None, 0.0, 0.0, cfg, FilterConfig())
    assert r.decision == "skip" and "confirmation" in r.reason


def test_narrative_bonus_raises_score():
    f = compute_features(make_pair())
    a = score_token(f, SAFE, None, 0.0, 0.0, ScoringConfig(), FilterConfig())
    b = score_token(f, SAFE, None, 0.0, 1.0, ScoringConfig(), FilterConfig())
    assert b.score > a.score and "narrative" in b.confluence


def test_learner_fits_separable_data(tmp_path):
    rng = np.random.default_rng(0)
    rows, labels = [], []
    for _ in range(200):
        good = rng.random() < 0.5
        f = compute_features(make_pair(buys_m5=80 if good else 30, sells_m5=20 if good else 70))
        f.update(pattern=70 if good else 40, kol_weight=0.0, narrative_match=0.0)
        rows.append(model_inputs(f))
        labels.append(1.0 if good else 0.0)
    m = LogisticModel.fit(np.array(rows), np.array(labels))
    good_f = {**compute_features(make_pair(buys_m5=80, sells_m5=20)), "pattern": 70}
    bad_f = {**compute_features(make_pair(buys_m5=30, sells_m5=70)), "pattern": 40}
    assert m.predict_proba(good_f) > 0.8 > 0.2 > m.predict_proba(bad_f)
    m.save(tmp_path / "m.json")
    assert LogisticModel.load(tmp_path / "m.json").n_samples == 200
