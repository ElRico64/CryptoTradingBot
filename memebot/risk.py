"""Portfolio-level risk: sizing, exposure caps, daily kill switch and loss-streak cool-off."""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone

from .analysis.scorer import conviction_multiplier
from .config import RiskConfig, ScoringConfig
from .models import Position

log = logging.getLogger(__name__)


def utc_day(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")


class RiskManager:
    def __init__(self, cfg: RiskConfig, scoring: ScoringConfig):
        self.cfg = cfg
        self.scoring = scoring
        self.day = ""
        self.day_start_equity = 0.0
        self.day_realized = 0.0
        self.trades_today = 0
        self.loss_streak = 0
        self.paused_until = 0.0

    def _roll_day(self, now: float, equity: float) -> None:
        day = utc_day(now)
        if day != self.day:
            self.day = day
            self.day_start_equity = equity
            self.day_realized = 0.0
            self.trades_today = 0

    def can_open(self, open_positions: list[Position], equity: float, cash: float, narrative: str = "", now: float | None = None) -> tuple[bool, str]:
        now = now or time.time()
        self._roll_day(now, equity)
        if now < self.paused_until:
            return False, f"cooling off after {self.cfg.loss_streak_pause} losses until {datetime.fromtimestamp(self.paused_until):%H:%M}"
        if self.day_start_equity > 0 and -self.day_realized >= self.day_start_equity * self.cfg.max_daily_loss_pct / 100:
            return False, "daily loss limit hit; no new entries until 00:00 UTC"
        if len(open_positions) >= self.cfg.max_open_positions:
            return False, "max open positions"
        if self.trades_today >= self.cfg.max_trades_per_day:
            return False, "max trades per day"
        if narrative:
            same = sum(1 for p in open_positions if p.narrative == narrative)
            if same >= self.cfg.max_positions_per_narrative:
                return False, f"already exposed to narrative '{narrative}'"
        if cash - self.cfg.min_cash_reserve_usd < self.cfg.min_position_usd:
            return False, "insufficient cash"
        return True, ""

    def position_size(self, score: float, equity: float, cash: float, liquidity_usd: float) -> float:
        size = equity * self.cfg.position_pct / 100 * conviction_multiplier(score, self.scoring)
        size = min(size, self.cfg.max_position_usd, liquidity_usd * self.cfg.max_pool_liquidity_pct / 100)
        size = min(size, cash - self.cfg.min_cash_reserve_usd)
        return size if size >= self.cfg.min_position_usd else 0.0

    def on_open(self, now: float | None = None) -> None:
        self.trades_today += 1

    def on_close(self, pnl_usd: float, now: float | None = None) -> None:
        now = now or time.time()
        if utc_day(now) == self.day:
            self.day_realized += pnl_usd
        if pnl_usd < 0:
            self.loss_streak += 1
            if self.loss_streak >= self.cfg.loss_streak_pause:
                self.paused_until = now + self.cfg.loss_streak_pause_min * 60
                log.warning("%d losses in a row: pausing entries for %.0f min", self.loss_streak, self.cfg.loss_streak_pause_min)
                self.loss_streak = 0
        else:
            self.loss_streak = 0
