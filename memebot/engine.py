"""The trading engine: discovery -> filters -> safety -> scoring -> risk -> execution -> exits."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from .analysis.features import compute_features
from .analysis.learner import LogisticModel
from .analysis.patterns import PatternResult, analyze as analyze_pattern
from .analysis.scorer import ScoreResult, hard_filters, score_token
from .config import Config
from .data.dexscreener import DexScreener
from .data.geckoterminal import GeckoTerminal
from .execution.base import ExecutionError, ExecutorRouter
from .journal import Journal
from .models import Candidate, PairSnapshot, Position, SafetyReport, norm_addr, token_key
from .notify import Notifier
from .risk import RiskManager
from .safety import SafetyChecker
from .sources.kol import KOLTracker
from .sources.narrative import NarrativeEngine
from .strategy.exits import ExitAction, ExitManager, MarketTick

log = logging.getLogger(__name__)


@dataclass
class Analysis:
    chain: str
    address: str
    decision: str
    reason: str
    pair: PairSnapshot | None = None
    safety: SafetyReport | None = None
    pattern: PatternResult | None = None
    features: dict[str, float] = field(default_factory=dict)
    result: ScoreResult | None = None
    sources: list[str] = field(default_factory=list)
    narrative: str = ""
    narrative_match: float = 0.0
    kol_weight: float = 0.0
    kol_wallets: list[str] = field(default_factory=list)

    @property
    def symbol(self) -> str:
        return self.pair.symbol if self.pair else ""


class Engine:
    def __init__(
        self,
        cfg: Config,
        journal: Journal,
        executor: ExecutorRouter,
        dex: DexScreener,
        gecko: GeckoTerminal,
        safety: SafetyChecker,
        notifier: Notifier,
        kol: KOLTracker | None = None,
        narrative: NarrativeEngine | None = None,
        model: LogisticModel | None = None,
    ):
        self.cfg = cfg
        self.journal = journal
        self.executor = executor
        self.dex = dex
        self.gecko = gecko
        self.safety = safety
        self.notifier = notifier
        self.kol = kol
        self.narrative = narrative
        self.model = model
        self.exits = ExitManager(cfg.exits)
        self.risk = RiskManager(cfg.risk, cfg.scoring)
        self.queue: asyncio.Queue[Candidate] = asyncio.Queue(maxsize=5000)
        self.positions: dict[str, Position] = {p.id: p for p in journal.open_positions()}
        self.mentions: dict[str, dict[str, tuple[float, dict[str, Any]]]] = {}
        self.last_eval: dict[str, float] = {}
        self.inflight: set[str] = set()
        self.busy: set[str] = set()
        self.blacklist: set[str] = set()
        self._entry_lock = asyncio.Lock()
        self._tasks: set[asyncio.Task] = set()

    # ------------------------------------------------------------------ discovery
    def open_positions(self) -> list[Position]:
        return [p for p in self.positions.values() if p.status == "open"]

    def holding(self, key: str) -> bool:
        return any(p.key == key for p in self.open_positions())

    async def emit(self, cand: Candidate) -> None:
        if cand.chain not in self.cfg.general.chains:
            return
        key = cand.key
        now = time.time()
        sources = self.mentions.setdefault(key, {})
        new_source = cand.source not in sources
        sources[cand.source] = (cand.ts, cand.context)
        if key in self.blacklist or key in self.inflight or self.holding(key):
            return
        if not new_source and now - self.last_eval.get(key, 0) < self.cfg.general.reevaluate_after_sec:
            return
        try:
            self.queue.put_nowait(cand)
            self.inflight.add(key)
        except asyncio.QueueFull:
            log.debug("queue full, dropping %s", key)

    def recent_mentions(self, key: str, now: float | None = None) -> dict[str, dict[str, Any]]:
        now = now or time.time()
        horizon = now - self.cfg.general.confluence_window_sec
        return {s: ctx for s, (ts, ctx) in self.mentions.get(key, {}).items() if ts >= horizon}

    # ------------------------------------------------------------------ analysis
    async def analyze(self, chain: str, address: str) -> Analysis:
        key = token_key(chain, address)
        mentions = self.recent_mentions(key)
        a = Analysis(chain, address, "skip", "", sources=sorted(mentions))

        pair = await self.dex.best_pair(chain, address)
        if pair is None:
            a.reason = "no DEX pair found"
            return a
        a.pair = pair
        problems = hard_filters(pair, self.cfg.filters)
        if problems:
            a.reason = "filters: " + "; ".join(problems)
            return a

        a.safety = await self.safety.check(chain, address, pair)
        if not a.safety.ok:
            a.reason = "safety: " + "; ".join(a.safety.reasons)
            return a

        if self.cfg.scoring.use_ohlcv_patterns and pair.pair_address:
            try:
                a.pattern = analyze_pattern(await self.gecko.ohlcv(chain, pair.pair_address))
            except Exception as exc:
                log.debug("OHLCV unavailable for %s: %s", pair.symbol, exc)

        a.features = compute_features(pair)

        kol_weight = 0.0
        if self.kol is not None and chain == "solana":
            kol_weight, a.kol_wallets = self.kol.consensus(address)
        if "kol" in mentions:
            kol_weight = max(kol_weight, float(mentions["kol"].get("kol_weight", 0)))
            a.kol_wallets = a.kol_wallets or list(mentions["kol"].get("kol_wallets", []))

        narrative_match = 0.0
        if "narrative" in mentions:
            narrative_match = float(mentions["narrative"].get("narrative_match", 0))
            a.narrative = mentions["narrative"].get("narrative", "")
        if self.narrative is not None:
            # Tokens found by other scanners can still ride a live narrative.
            m, n = self.narrative.store.match(pair.symbol, pair.name)
            if n is not None and m > narrative_match and self.narrative.strong_enough(m, n):
                narrative_match, a.narrative = m, n.key

        a.kol_weight, a.narrative_match = kol_weight, narrative_match
        a.result = score_token(
            a.features, a.safety, a.pattern, kol_weight, narrative_match, self.cfg.scoring, self.cfg.filters, self.model
        )
        a.decision = a.result.decision
        a.reason = a.result.reason
        return a

    async def process(self, cand: Candidate) -> None:
        key = cand.key
        self.last_eval[key] = time.time()
        a = await self.analyze(cand.chain, cand.address)
        if a.result is None:
            log.debug("skip %s %s: %s", a.symbol or cand.address[:8], a.sources, a.reason)
            return
        signal_id = self.journal.log_signal(
            cand.chain, cand.address, a.symbol, a.result.score, a.decision, a.reason, a.sources,
            {**a.features, "pattern": a.result.components.get("pattern", 50.0),
             "kol_weight": a.kol_weight, "narrative_match": a.narrative_match},
            {**a.result.components, "tags": a.result.extras.get("pattern_tags", [])},
        )
        log.info(
            "%-5s %-10s score=%5.1f conf=%s src=%s | %s",
            a.decision.upper(), a.symbol[:10], a.result.score, a.result.confluence, a.sources, a.reason,
        )
        if a.decision == "enter":
            await self.enter(a, signal_id)

    # ------------------------------------------------------------------ entries
    async def equity(self) -> float:
        cash = await self.executor.total_cash_usd()
        return cash + sum(p.value_usd() for p in self.open_positions())

    async def enter(self, a: Analysis, signal_id: int) -> Position | None:
        assert a.pair is not None and a.result is not None
        key = token_key(a.chain, a.address)
        async with self._entry_lock:
            if self.holding(key) or key in self.blacklist:
                return None
            equity = await self.equity()
            cash = await self.executor.cash_usd(a.chain)
            ok, why = self.risk.can_open(self.open_positions(), equity, cash, a.narrative)
            if not ok:
                log.info("entry blocked for %s: %s", a.symbol, why)
                return None
            size = self.risk.position_size(a.result.score, equity, cash, a.pair.liquidity_usd)
            if size <= 0:
                log.info("entry for %s sized to zero", a.symbol)
                return None
            try:
                loss = await self.executor.roundtrip_loss_pct(a.chain, a.address, size)
            except ExecutionError as exc:
                log.info("round-trip quote failed for %s: %s", a.symbol, exc)
                return None
            if loss > self.cfg.safety.max_roundtrip_loss_pct:
                log.warning("%s round-trip loses %.1f%%: likely honeypot/tax, blacklisting", a.symbol, loss)
                self.blacklist.add(key)
                return None
            fresh = await self.dex.best_pair(a.chain, a.address)
            if fresh and a.pair.price_usd > 0 and fresh.price_usd > a.pair.price_usd * 1.15:
                log.info("%s ran %.0f%% during analysis; not chasing", a.symbol, (fresh.price_usd / a.pair.price_usd - 1) * 100)
                return None
            try:
                fill = await self.executor.buy(a.chain, a.address, size)
            except ExecutionError as exc:
                log.error("BUY failed for %s: %s", a.symbol, exc)
                return None

            pos = Position(
                chain=a.chain,
                token_address=a.address,
                symbol=a.symbol,
                pair_address=a.pair.pair_address,
                entry_price=fill.price,
                entry_ts=time.time(),
                cost_usd=fill.usd,
                qty=fill.qty,
                entry_liquidity_usd=(fresh or a.pair).liquidity_usd,
                score=a.result.score,
                sources=a.sources,
                narrative=a.narrative,
                kol_wallets=a.kol_wallets,
                signal_id=signal_id,
            )
            self.positions[pos.id] = pos
            self.risk.on_open()
            self.journal.save_position(pos)
            self.journal.attach_position(signal_id, pos.id)
            self.journal.log_trade(pos, "buy", fill.qty, fill.usd, fill.price, fill.tx, a.reason)
        await self.notifier.send(
            f"BUY {pos.symbol} ({pos.chain}) ${fill.usd:,.2f} @ {fill.price:.8g}\n"
            f"score {a.result.score:.1f} | {', '.join(a.result.confluence)} | sources {', '.join(a.sources)}"
            + (f"\nnarrative: {a.narrative}" if a.narrative else "")
            + (f"\n{a.pair.url}" if a.pair.url else "")
        )
        return pos

    # ------------------------------------------------------------------ exits
    async def check_positions(self) -> None:
        open_pos = self.open_positions()
        if not open_pos:
            return
        by_chain: dict[str, list[Position]] = {}
        for p in open_pos:
            by_chain.setdefault(p.chain, []).append(p)
        now = time.time()
        for chain, plist in by_chain.items():
            try:
                pairs = await self.dex.pairs_for_tokens(chain, [p.token_address for p in plist])
            except Exception as exc:
                log.warning("price refresh failed for %s: %s", chain, exc)
                continue
            for pos in plist:
                if pos.id in self.busy:
                    continue
                pair = pairs.get(norm_addr(chain, pos.token_address))
                action: ExitAction | None
                if pair is None or pair.price_usd <= 0:
                    pos.missing_checks += 1
                    if pos.missing_checks < self.cfg.exits.missing_price_exit_checks:
                        continue
                    action = ExitAction(1.0, "price feed lost")
                else:
                    pos.missing_checks = 0
                    pos.last_price = pair.price_usd
                    t5 = pair.txn_count("m5")
                    tick = MarketTick(
                        price=pair.price_usd,
                        liquidity_usd=pair.liquidity_usd,
                        buy_ratio_m5=pair.buys("m5") / t5 if t5 else None,
                        txns_m5=t5,
                        kol_sellers=self.kol.sellers(pos.token_address, pos.kol_wallets, pos.entry_ts)
                        if self.kol and pos.kol_wallets
                        else 0,
                    )
                    action = self.exits.evaluate(pos, tick, now)
                if action:
                    self.busy.add(pos.id)
                    task = asyncio.create_task(self.execute_exit(pos, action))
                    self._tasks.add(task)
                    task.add_done_callback(self._tasks.discard)

    async def execute_exit(self, pos: Position, action: ExitAction) -> None:
        try:
            fill = await self.executor.sell(pos.chain, pos.token_address, action.fraction)
        except ExecutionError as exc:
            log.error("SELL failed for %s (%s): %s", pos.symbol, action.reason, exc)
            await self.notifier.send(f"SELL FAILED {pos.symbol}: {action.reason}: {exc} (will retry)")
            return
        finally:
            self.busy.discard(pos.id)
        full = action.fraction >= 0.999
        pos.qty = 0.0 if full else max(0.0, pos.qty - fill.qty)
        pos.realized_usd += fill.usd
        pos.tp_hits.extend(action.take_profit_levels)
        self.journal.log_trade(pos, "sell", fill.qty, fill.usd, fill.price, fill.tx, action.reason)
        if full or pos.remaining_fraction < 0.001:
            pos.status = "closed"
            pos.closed_ts = time.time()
            pos.exit_reason = action.reason
            pnl = pos.realized_usd - pos.cost_usd
            self.risk.on_close(pnl)
            if "liquidity" in action.reason or "price feed lost" in action.reason:
                self.blacklist.add(pos.key)
            await self.notifier.send(
                f"CLOSED {pos.symbol}: {action.reason}\nP&L ${pnl:+,.2f} ({pos.realized_multiple:.2f}x on ${pos.cost_usd:,.2f})"
            )
        else:
            await self.notifier.send(
                f"SOLD {action.fraction:.0%} of {pos.symbol}: {action.reason} (+${fill.usd:,.2f}); "
                f"holding {pos.remaining_fraction:.0%}"
            )
        self.journal.save_position(pos)

    # ------------------------------------------------------------------ loops
    async def _worker(self) -> None:
        while True:
            cand = await self.queue.get()
            try:
                await self.process(cand)
            except Exception:
                log.exception("analysis failed for %s", cand.key)
            finally:
                self.inflight.discard(cand.key)
                self.queue.task_done()

    async def _monitor(self) -> None:
        while True:
            try:
                await self.check_positions()
                for p in self.open_positions():
                    self.journal.save_position(p)
            except Exception:
                log.exception("position monitor error")
            await asyncio.sleep(self.cfg.general.monitor_interval_sec)

    async def _status(self) -> None:
        while True:
            await asyncio.sleep(self.cfg.general.status_interval_sec)
            try:
                eq = await self.equity()
                lines = [
                    f"{p.symbol} {p.multiple(p.last_price):.2f}x (peak {p.peak_multiple:.2f}x, {p.remaining_fraction:.0%} held)"
                    for p in self.open_positions()
                ]
                log.info("equity $%.2f | queue %d | open: %s", eq, self.queue.qsize(), "; ".join(lines) or "none")
                horizon = time.time() - self.cfg.general.confluence_window_sec * 2
                for k in [k for k, v in self.mentions.items() if max(ts for ts, _ in v.values()) < horizon]:
                    self.mentions.pop(k, None)
                    self.last_eval.pop(k, None)
            except Exception:
                log.exception("status error")

    async def run(self, sources: list) -> None:
        if self.positions:
            log.info("Resuming %d open position(s) from the journal", len(self.positions))
        tasks = [asyncio.create_task(self._worker()) for _ in range(self.cfg.general.analysis_workers)]
        tasks += [asyncio.create_task(self._monitor()), asyncio.create_task(self._status())]
        tasks += [asyncio.create_task(src.run(self.emit)) for src in sources]
        await asyncio.gather(*tasks)
