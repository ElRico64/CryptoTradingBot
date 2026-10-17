"""Numeric features describing a token's market at one instant."""

from __future__ import annotations

from ..models import PairSnapshot


def compute_features(pair: PairSnapshot) -> dict[str, float]:
    t5, t1 = pair.txn_count("m5"), pair.txn_count("h1")
    v5, v1 = pair.volume.get("m5", 0.0), pair.volume.get("h1", 0.0)
    liq = pair.liquidity_usd
    mcap = pair.mcap
    return {
        "liquidity_usd": liq,
        "mcap_usd": mcap,
        "age_min": pair.age_minutes if pair.age_minutes is not None else 10_000.0,
        "txns_m5": float(t5),
        "txns_h1": float(t1),
        "buy_ratio_m5": pair.buys("m5") / t5 if t5 else 0.5,
        "buy_ratio_h1": pair.buys("h1") / t1 if t1 else 0.5,
        # >1 means the last 5 minutes run hotter than the hourly average.
        "txn_accel": (t5 * 12) / t1 if t1 else 0.0,
        "vol_accel": (v5 * 12) / v1 if v1 else 0.0,
        "vol_m5": v5,
        "vol_h1": v1,
        "avg_trade_usd_m5": v5 / t5 if t5 else 0.0,
        "liq_to_mcap": liq / mcap if mcap else 0.0,
        "turnover_h1": v1 / liq if liq else 0.0,
        "pc_m5": pair.price_change.get("m5", 0.0),
        "pc_h1": pair.price_change.get("h1", 0.0),
        "pc_h6": pair.price_change.get("h6", 0.0),
        "socials": float(pair.socials + pair.websites),
        "boosts": float(pair.boosts),
    }


# Features fed to the learned model (stable order matters for saved weights).
MODEL_FEATURES = [
    "buy_ratio_m5",
    "buy_ratio_h1",
    "txn_accel",
    "vol_accel",
    "liq_to_mcap",
    "turnover_h1",
    "pc_m5",
    "pc_h1",
    "socials",
    "log_liquidity",
    "log_mcap",
    "log_age",
    "pattern",
    "kol_weight",
    "narrative_match",
]
