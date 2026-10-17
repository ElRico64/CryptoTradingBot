"""Replay the exit strategy over historical candles to tune take-profit / stop settings.

Within each candle we assume the worst plausible order: the low is visited before the high,
so stops are checked before take-profits. Results are therefore slightly pessimistic.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence

from .config import ExitConfig
from .data.geckoterminal import Candle
from .models import Position
from .strategy.exits import ExitAction, ExitManager, MarketTick, apply_simulated_sell


@dataclass
class BacktestResult:
    multiple: float  # USD returned / USD invested
    exit_reason: str
    peak_multiple: float
    minutes_held: float
    events: list[str] = field(default_factory=list)


def simulate(candles: Sequence[Candle], cfg: ExitConfig, entry_index: int = 0, fee_pct: float = 1.0, stake: float = 100.0) -> BacktestResult:
    entry = candles[entry_index]
    entry_price = entry.c * (1 + fee_pct / 100)
    pos = Position("sim", "sim", "SIM", "", entry_price, float(entry.ts), stake, stake / entry_price, 0.0, 0.0)
    mgr = ExitManager(cfg)
    events: list[str] = []
    reason = "end of data"
    last_ts = float(entry.ts)
    for c in candles[entry_index + 1 :]:
        last_ts = float(c.ts)
        for price in (c.l, c.h, c.c):
            action = mgr.evaluate(pos, MarketTick(price=price), now=float(c.ts))
            if action:
                usd = apply_simulated_sell(pos, action, price, fee_pct)
                events.append(f"{(c.ts - entry.ts) / 60:.0f}m {action.reason}: sold {action.fraction:.0%} for ${usd:.2f}")
                if pos.qty <= pos.initial_qty * 1e-3:
                    reason = action.reason
                    break
        if pos.qty <= pos.initial_qty * 1e-3:
            break
    else:
        if pos.qty > 0:
            apply_simulated_sell(pos, ExitAction(1.0, reason), candles[-1].c, fee_pct)
    return BacktestResult(pos.realized_usd / stake, reason, pos.peak_multiple, (last_ts - entry.ts) / 60, events)


def load_csv(path: str | Path) -> list[Candle]:
    """CSV with columns ts,open,high,low,close,volume (ts in seconds; header optional)."""
    rows = []
    with open(path, newline="") as f:
        for r in csv.reader(f):
            if not r or not r[0].strip().lstrip("-").replace(".", "").isdigit():
                continue
            rows.append(Candle(int(float(r[0])), *(float(x) for x in r[1:6])))
    rows.sort(key=lambda c: c.ts)
    return rows
