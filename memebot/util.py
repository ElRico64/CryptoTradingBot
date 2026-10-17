"""Small numeric and logging helpers."""

from __future__ import annotations

import logging
import math


def clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


def ramp(x: float, lo: float, hi: float) -> float:
    """0 at or below lo, 1 at or above hi, linear in between."""
    if hi == lo:
        return 1.0 if x >= hi else 0.0
    return clamp((x - lo) / (hi - lo))


def band(x: float, zero_lo: float, full_lo: float, full_hi: float, zero_hi: float) -> float:
    """Trapezoid: 1 inside [full_lo, full_hi], fading to 0 at zero_lo and zero_hi."""
    if x <= zero_lo or x >= zero_hi:
        return 0.0
    if x < full_lo:
        return ramp(x, zero_lo, full_lo)
    if x > full_hi:
        return 1.0 - ramp(x, full_hi, zero_hi)
    return 1.0


def safe_float(value, default: float = 0.0) -> float:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return default
    return f if math.isfinite(f) else default


def setup_logging(level: str = "INFO") -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    logging.getLogger("aiohttp").setLevel(logging.WARNING)
    logging.getLogger("anthropic").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
