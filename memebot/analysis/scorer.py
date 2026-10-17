"""Turns market data, chart structure, smart-money and narrative signals into one decision.

Score = weighted average of always-available components (0..100)
        + bonuses for independent edges (KOL accumulation, live narrative match)
        blended with the learned model once it has enough history.

Entry also requires *confluence*: at least `min_confluence` independent reasons to buy,
unless the score is exceptionally high. This is what "quality over quantity" means in code.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..config import FilterConfig, ScoringConfig
from ..models import PairSnapshot, SafetyReport
from ..util import band, clamp, ramp
from .learner import LogisticModel
from .patterns import PatternResult


@dataclass
class ScoreResult:
    score: float
    components: dict[str, float]
    confluence: list[str]
    decision: str  # "enter" | "skip"
    reason: str
    model_proba: float | None = None
    extras: dict[str, Any] = field(default_factory=dict)


def hard_filters(pair: PairSnapshot, cfg: FilterConfig) -> list[str]:
    reasons = []
    if pair.price_usd <= 0:
        reasons.append("no price")
    if pair.liquidity_usd < cfg.min_liquidity_usd:
        reasons.append(f"liquidity ${pair.liquidity_usd:,.0f} < ${cfg.min_liquidity_usd:,.0f}")
    if pair.liquidity_usd > cfg.max_liquidity_usd:
        reasons.append("liquidity above range (not a memecoin play)")
    if pair.mcap and pair.mcap < cfg.min_market_cap_usd:
        reasons.append(f"mcap ${pair.mcap:,.0f} too small")
    if pair.mcap > cfg.max_market_cap_usd:
        reasons.append(f"mcap ${pair.mcap:,.0f} too large")
    if pair.age_minutes is not None and pair.age_minutes > cfg.max_age_hours * 60:
        reasons.append(f"pair older than {cfg.max_age_hours:.0f}h")
    if pair.txn_count("h1") < cfg.min_txns_h1:
        reasons.append(f"only {pair.txn_count('h1')} txns in 1h")
    if pair.volume.get("h1", 0) < cfg.min_volume_h1_usd:
        reasons.append("1h volume too low")
    if pair.mcap and pair.liquidity_usd / pair.mcap < cfg.min_liquidity_to_mcap:
        reasons.append("liquidity too thin for its market cap")
    if pair.price_change.get("h1", 0) > cfg.max_price_change_h1_pct:
        reasons.append("already vertical this hour")
    if cfg.quote_symbols and pair.quote_symbol and pair.quote_symbol.upper() not in {q.upper() for q in cfg.quote_symbols}:
        reasons.append(f"unsupported quote token {pair.quote_symbol}")
    return reasons


def momentum_component(f: dict[str, float]) -> float:
    # Rising, but not already parabolic: the best risk/reward is early in the move.
    return 100 * (0.6 * band(f["pc_h1"], -15, 15, 180, 600) + 0.4 * band(f["pc_m5"], -10, 2, 35, 120))


def order_flow_component(f: dict[str, float]) -> float:
    s = (
        0.40 * ramp(f["buy_ratio_m5"], 0.45, 0.68)
        + 0.20 * ramp(f["buy_ratio_h1"], 0.45, 0.62)
        + 0.25 * ramp(f["txn_accel"], 0.8, 2.5)
        + 0.15 * ramp(f["txns_m5"], 15, 150)
    )
    # Lots of tiny trades with no size = bots painting volume.
    if f["txns_m5"] >= 20 and f["avg_trade_usd_m5"] < 12:
        s *= 0.6
    return 100 * s


def liquidity_component(f: dict[str, float], filters: FilterConfig) -> float:
    return 100 * (
        0.5 * ramp(f["liquidity_usd"], filters.min_liquidity_usd, 4 * filters.min_liquidity_usd)
        + 0.3 * band(f["liq_to_mcap"], 0.02, 0.08, 0.6, 1.2)
        + 0.2 * band(f["turnover_h1"], 0.05, 0.3, 6, 25)
    )


def earliness_component(f: dict[str, float]) -> float:
    # The edge in memecoins is being early *with confirmation*: young pair, small cap.
    return 100 * (0.5 * band(f["age_min"], 0, 8, 360, 4320) + 0.5 * band(f["mcap_usd"], 15_000, 60_000, 2_000_000, 30_000_000))


def safety_component(safety: SafetyReport) -> float:
    d = safety.data
    s = 1.0
    if d.get("top10_pct") is not None:
        s -= 0.4 * ramp(d["top10_pct"], 12, 30)
    if d.get("risk_norm") is not None:
        s -= 0.3 * ramp(d["risk_norm"], 5, 60)
    if d.get("holders"):
        s -= 0.2 * (1 - ramp(d["holders"], 150, 2000))
    s -= 0.05 * len(safety.warnings)
    return 100 * clamp(s)


def score_token(
    features: dict[str, float],
    safety: SafetyReport,
    pattern: PatternResult | None,
    kol_weight: float,
    narrative_match: float,
    cfg: ScoringConfig,
    filters: FilterConfig,
    model: LogisticModel | None = None,
) -> ScoreResult:
    comps = {
        "momentum": momentum_component(features),
        "order_flow": order_flow_component(features),
        "liquidity": liquidity_component(features, filters),
        "earliness": earliness_component(features),
        "safety": safety_component(safety),
    }
    if pattern is not None:
        comps["pattern"] = pattern.score
    weights = {k: cfg.weights.get(k, 1.0) for k in comps}
    total_w = sum(weights.values()) or 1.0
    base = sum(comps[k] * weights[k] for k in comps) / total_w

    kol_bonus = cfg.kol_bonus_max * clamp(kol_weight / 3.0)
    narrative_bonus = cfg.narrative_bonus_max * clamp(narrative_match)
    rule_score = clamp(base + kol_bonus + narrative_bonus, 0, 100)

    model_proba = None
    score = rule_score
    if model is not None and model.n_samples >= cfg.learned_model_min_samples:
        model_proba = model.predict_proba(
            {**features, "pattern": comps.get("pattern", 50.0), "kol_weight": kol_weight, "narrative_match": narrative_match}
        )
        score = (1 - cfg.learned_model_weight) * rule_score + cfg.learned_model_weight * 100 * model_proba

    confluence = []
    if kol_weight > 0:
        confluence.append("smart_money")
    if narrative_match > 0:
        confluence.append("narrative")
    if comps.get("pattern", 0) >= 65:
        confluence.append("chart_structure")
    if comps["order_flow"] >= 70:
        confluence.append("order_flow")
    if comps["momentum"] >= 70:
        confluence.append("momentum")

    if score >= cfg.strong_threshold and len(confluence) >= 1:
        decision, reason = "enter", f"strong score {score:.1f}"
    elif score >= cfg.entry_threshold and len(confluence) >= cfg.min_confluence:
        decision, reason = "enter", f"score {score:.1f} with {len(confluence)} confirmations"
    elif score < cfg.entry_threshold:
        decision, reason = "skip", f"score {score:.1f} < {cfg.entry_threshold:.0f}"
    else:
        decision, reason = "skip", f"only {len(confluence)} confirmation(s): {confluence}"

    comps["kol_bonus"] = kol_bonus
    comps["narrative_bonus"] = narrative_bonus
    return ScoreResult(
        score=round(score, 2),
        components={k: round(v, 2) for k, v in comps.items()},
        confluence=confluence,
        decision=decision,
        reason=reason,
        model_proba=model_proba,
        extras={"rule_score": round(rule_score, 2), "pattern_tags": pattern.tags if pattern else []},
    )


def conviction_multiplier(score: float, cfg: ScoringConfig) -> float:
    """Bigger size only when the evidence is genuinely stronger."""
    if score >= cfg.strong_threshold + 5:
        return 1.5
    if score >= cfg.strong_threshold:
        return 1.2
    return 1.0 if score >= cfg.entry_threshold + 5 else 0.7
