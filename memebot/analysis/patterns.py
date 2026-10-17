"""Chart-structure recognition on 1-minute candles.

The goal is not to predict the future from a line; it is to avoid the two classic memecoin
mistakes (chasing a vertical candle, buying a chart that is already distributing) and to favour
the entries good discretionary traders use: the first healthy pullback after an impulse that
holds VWAP, and volume-confirmed breakouts out of a tight range.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Sequence

import numpy as np

from ..data.geckoterminal import Candle


@dataclass
class PatternResult:
    score: float  # 0..100, 50 = neutral
    tags: list[str] = field(default_factory=list)
    metrics: dict[str, float] = field(default_factory=dict)


def ema(values: np.ndarray, n: int) -> np.ndarray:
    alpha = 2.0 / (n + 1)
    out = np.empty_like(values, dtype=float)
    out[0] = values[0]
    for i in range(1, len(values)):
        out[i] = alpha * values[i] + (1 - alpha) * out[i - 1]
    return out


def rsi(closes: np.ndarray, n: int = 14) -> float:
    if len(closes) <= n:
        return 50.0
    deltas = np.diff(closes)
    gains = np.clip(deltas, 0, None)
    losses = np.clip(-deltas, 0, None)
    avg_gain = gains[:n].mean()
    avg_loss = losses[:n].mean()
    for g, lo in zip(gains[n:], losses[n:]):  # Wilder smoothing
        avg_gain = (avg_gain * (n - 1) + g) / n
        avg_loss = (avg_loss * (n - 1) + lo) / n
    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0
    return 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)


def swing_lows(lows: np.ndarray, k: int = 2) -> list[float]:
    return [
        float(lows[i])
        for i in range(k, len(lows) - k)
        if lows[i] == lows[i - k : i + k + 1].min()
    ]


def analyze(candles: Sequence[Candle]) -> PatternResult:
    if len(candles) < 20:
        return PatternResult(50.0, ["insufficient_history"], {"candles": float(len(candles))})

    o = np.array([c.o for c in candles], dtype=float)
    h = np.array([c.h for c in candles], dtype=float)
    lo = np.array([c.l for c in candles], dtype=float)
    c = np.array([c.c for c in candles], dtype=float)
    v = np.array([c.v for c in candles], dtype=float)
    last = c[-1]

    typical = (h + lo + c) / 3
    cum_v = np.cumsum(v)
    vwap = np.cumsum(typical * v)[-1] / cum_v[-1] if cum_v[-1] > 0 else typical.mean()
    extension = last / vwap - 1 if vwap > 0 else 0.0
    r = rsi(c)
    e9, e21 = ema(c, 9), ema(c, 21)

    base_v = v[-33:-3] if len(v) >= 36 else v[:-3]
    vol_z = float((v[-3:].mean() - base_v.mean()) / (base_v.std() + 1e-9)) if len(base_v) > 2 else 0.0

    window = min(len(c), 60)
    start = len(c) - window
    hh_idx = start + int(np.argmax(h[start:]))
    low_before = float(lo[start : hh_idx + 1].min())
    hh = float(h[hh_idx])
    impulse = hh / low_before - 1 if low_before > 0 else 0.0
    retrace = (hh - last) / (hh - low_before) if hh > low_before else 0.0

    prior_high = float(h[-21:-1].max())
    prior_range = (h[-21:-1].max() - lo[-21:-1].min()) / max(lo[-21:-1].min(), 1e-12)
    breakout = last > prior_high and vol_z >= 1.5

    lows = swing_lows(lo[-40:])
    higher_lows = len(lows) >= 3 and lows[-1] > lows[-2] > lows[-3]

    recent = slice(-6, None)
    red_heavy = int(((c[recent] < o[recent]) & (v[recent] > np.median(v))).sum())
    lower_highs = bool(np.all(np.diff(h[-5:]) < 0))
    distribution = red_heavy >= 3 and lower_highs

    score = 50.0
    tags: list[str] = []
    if last > vwap:
        score += 10
        tags.append("above_vwap")
    else:
        score -= 12
        tags.append("below_vwap")
    if e9[-1] > e21[-1]:
        score += 8
        tags.append("ema_uptrend")
    else:
        score -= 8

    # A blow-off top looks like a breakout with volume; don't reward it as one.
    overextended = r > 85 or extension > 0.8
    first_pullback = impulse >= 0.5 and 0.25 <= retrace <= 0.62 and last > vwap and c[-1] > o[-1] and hh_idx < len(c) - 2
    if first_pullback:
        score += 18
        tags.append("first_pullback")
    if breakout and not overextended:
        score += 14 if prior_range < 0.35 else 8  # tight base breakouts are higher quality
        tags.append("range_breakout")
    if higher_lows:
        score += 8
        tags.append("higher_lows")
    if 1.0 <= vol_z <= 5.0 and not overextended:
        score += 6
        tags.append("volume_expansion")
    if overextended:
        score -= 22
        tags.append("overextended")
    if retrace > 0.786 and impulse >= 0.3:
        score -= 15
        tags.append("failed_impulse")
    if distribution:
        score -= 15
        tags.append("distribution")
    if r < 35 and last < vwap:
        score -= 8
        tags.append("breaking_down")

    metrics = {
        "rsi": round(r, 1),
        "vwap_extension": round(extension, 3),
        "impulse": round(impulse, 3),
        "retrace": round(retrace, 3),
        "vol_z": round(vol_z, 2),
        "candles": float(len(c)),
    }
    return PatternResult(float(np.clip(score, 0, 100)), tags, metrics)
