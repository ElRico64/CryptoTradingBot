"""Exit engine: where memecoin profits are actually made or lost.

Rules, checked in priority order on every price tick:

1. Liquidity pull     - pool liquidity collapses vs entry -> sell everything immediately.
2. Hard stop          - before the first take-profit, price <= entry * (1 - stop_loss) -> sell all.
3. Breakeven floor    - after the first take-profit the rest may never close below
                        entry * breakeven_floor_multiple (the trade is locked in profit).
4. Trailing stop      - once the peak passes a tier, a pullback of drawdown_pct from the peak
                        sells the rest. Tiers tighten as the multiple grows.
5. Take-profit ladder - default: 50% at 2x (stake recovered), half the rest at 5x, half again
                        at 10x; the remaining "moon bag" rides the trailing stop.
6. Copy-trade exit    - the KOLs whose buying triggered the entry are selling.
7. Flow reversal      - before TP1, a losing position where sellers dominate the tape.
8. Time stop          - no meaningful move within N minutes: free the capital.
9. Max hold           - nothing is held forever.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..config import ExitConfig
from ..models import Position


@dataclass
class ExitAction:
    fraction: float  # fraction of the *currently held* quantity to sell
    reason: str
    take_profit_levels: tuple[float, ...] = ()


@dataclass
class MarketTick:
    price: float
    liquidity_usd: float | None = None
    buy_ratio_m5: float | None = None
    txns_m5: int | None = None
    kol_sellers: int = 0


class ExitManager:
    def __init__(self, cfg: ExitConfig):
        self.cfg = cfg

    def trail_drawdown_pct(self, peak_multiple: float) -> float | None:
        pct = None
        for tier in sorted(self.cfg.trail_tiers, key=lambda t: t.from_multiple):
            if peak_multiple >= tier.from_multiple:
                pct = tier.drawdown_pct
        return pct

    def evaluate(self, pos: Position, tick: MarketTick, now: float) -> ExitAction | None:
        cfg = self.cfg
        price = tick.price
        if price <= 0 or pos.qty <= 0:
            return None
        pos.peak_price = max(pos.peak_price, price)
        mult = pos.multiple(price)
        peak_mult = pos.peak_multiple
        tp1_done = bool(pos.tp_hits)
        age_min = (now - pos.entry_ts) / 60

        # 1. Liquidity pull
        if tick.liquidity_usd is not None and pos.entry_liquidity_usd > 0:
            drop = 1 - tick.liquidity_usd / pos.entry_liquidity_usd
            if drop * 100 >= cfg.liquidity_drop_exit_pct:
                return ExitAction(1.0, f"liquidity dropped {drop * 100:.0f}%")

        # 2. Hard stop
        if not tp1_done and mult <= 1 - cfg.stop_loss_pct / 100:
            return ExitAction(1.0, f"stop loss at {mult:.2f}x")

        # 3. Breakeven floor after the first take-profit
        if tp1_done and mult <= cfg.breakeven_floor_multiple:
            return ExitAction(1.0, f"breakeven floor at {mult:.2f}x")

        # 4. Trailing stop
        trail = self.trail_drawdown_pct(peak_mult)
        if trail is not None and price <= pos.peak_price * (1 - trail / 100):
            return ExitAction(1.0, f"trailing stop: {mult:.2f}x, {trail:.0f}% off {peak_mult:.2f}x peak")

        # 5. Take-profit ladder (several levels can trigger on one gap up)
        hit = [tp for tp in cfg.take_profits if mult >= tp.multiple and tp.multiple not in pos.tp_hits]
        if hit:
            keep = 1.0
            for tp in hit:
                keep *= 1 - tp.sell_fraction
            return ExitAction(1 - keep, f"take profit at {mult:.2f}x", tuple(tp.multiple for tp in hit))

        # 6. The smart money that got us in is getting out
        if pos.kol_wallets and tick.kol_sellers >= cfg.kol_exit_min_sellers:
            frac = 1.0 if not tp1_done else 0.5
            return ExitAction(frac, f"{tick.kol_sellers} source KOL(s) selling")

        # 7. Order-flow reversal on a loser
        if (
            cfg.flow_reversal_exit
            and not tp1_done
            and mult < 1.0
            and age_min >= 3
            and tick.buy_ratio_m5 is not None
            and (tick.txns_m5 or 0) >= 30
            and tick.buy_ratio_m5 < cfg.flow_reversal_buy_ratio
        ):
            return ExitAction(1.0, f"sellers in control ({tick.buy_ratio_m5:.0%} buys) at {mult:.2f}x")

        # 8. Time stop
        if not tp1_done and age_min >= cfg.time_stop_min and peak_mult < cfg.time_stop_min_peak_multiple:
            return ExitAction(1.0, f"time stop after {age_min:.0f}m (peak {peak_mult:.2f}x)")

        # 9. Max hold
        if age_min >= cfg.max_hold_hours * 60:
            return ExitAction(1.0, "max hold time")
        return None


def apply_simulated_sell(pos: Position, action: ExitAction, price: float, fee_pct: float = 0.0) -> float:
    """Book a sell against a position (used by backtests and paper fills). Returns USD received."""
    qty = pos.qty * min(1.0, action.fraction)
    if action.fraction >= 0.999:
        qty = pos.qty
    usd = qty * price * (1 - fee_pct / 100)
    pos.qty -= qty
    pos.realized_usd += usd
    pos.tp_hits.extend(action.take_profit_levels)
    return usd
