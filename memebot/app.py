"""Wires configuration and secrets into a ready-to-run engine."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from .analysis.learner import LogisticModel
from .config import EVM_CHAINS, Config, Secrets
from .data.dexscreener import DexScreener
from .data.geckoterminal import GeckoTerminal
from .data.http import Http
from .data.solana_rpc import SolanaRPC
from .engine import Engine
from .execution.base import Executor, ExecutorRouter
from .execution.paper import PaperExecutor
from .journal import Journal
from .notify import Notifier
from .safety import SafetyChecker
from .sources.kol import KOLTracker
from .sources.narrative import NarrativeEngine
from .sources.scanners import DexScreenerScanner, GeckoTerminalScanner, PumpPortalStream

log = logging.getLogger(__name__)


class LiveTradingNotAcknowledged(RuntimeError):
    pass


@dataclass
class App:
    http: Http
    engine: Engine
    sources: list
    journal: Journal

    async def close(self) -> None:
        await self.engine.executor.close()
        await self.http.close()
        self.journal.close()


def build_executors(cfg: Config, secrets: Secrets, http: Http, dex: DexScreener, rpc: SolanaRPC | None) -> ExecutorRouter:
    if cfg.general.mode == "paper":
        paper = PaperExecutor(
            cfg.risk.paper_starting_balance_usd,
            dex.best_pair,
            fee_pct=cfg.execution.paper_fee_pct,
            extra_slippage_pct=cfg.execution.paper_extra_slippage_pct,
        )
        return ExecutorRouter({c: paper for c in cfg.general.chains})

    if not secrets.live_ack:
        raise LiveTradingNotAcknowledged(
            "Live mode trades real money. Set MEMEBOT_I_ACCEPT_THE_RISK=yes in your environment to enable it."
        )
    executors: dict[str, Executor] = {}
    if "solana" in cfg.general.chains:
        from .execution.jupiter import JupiterExecutor, load_keypair

        if not secrets.solana_private_key:
            raise ValueError("SOLANA_PRIVATE_KEY is required for live Solana trading")
        assert rpc is not None
        executors["solana"] = JupiterExecutor(
            http, rpc, load_keypair(secrets.solana_private_key), cfg.execution, secrets.jupiter_api_key
        )
    evm = [c for c in cfg.general.chains if c in EVM_CHAINS]
    if evm:
        from .execution.evm import EvmRpc, ZeroExExecutor, native_price_from_dexscreener

        if not secrets.evm_private_key:
            raise ValueError("EVM_PRIVATE_KEY is required for live EVM trading")
        price_fn = native_price_from_dexscreener(dex)
        for chain in evm:
            url = secrets.evm_rpc_urls.get(chain)
            if not url:
                raise ValueError(f"EVM_RPC_URL_{chain.upper()} is required for live trading on {chain}")
            executors[chain] = ZeroExExecutor(
                http, chain, EvmRpc(http, url), secrets.evm_private_key, secrets.zeroex_api_key, cfg.execution, price_fn
            )
    return ExecutorRouter(executors)


def build_app(cfg: Config, secrets: Secrets) -> App:
    http = Http()
    dex = DexScreener(http)
    gecko = GeckoTerminal(http)
    rpc = SolanaRPC(http, secrets.solana_rpc_url) if "solana" in cfg.general.chains else None
    journal = Journal(cfg.general.db_path)
    executor = build_executors(cfg, secrets, http, dex, rpc)
    notifier = Notifier(http, secrets.telegram_bot_token if cfg.notify.telegram else "", secrets.telegram_chat_id)
    safety = SafetyChecker(cfg.safety, http, rpc)

    kol = KOLTracker(cfg.kol, rpc) if cfg.kol.enabled and "solana" in cfg.general.chains else None
    narrative = (
        NarrativeEngine(cfg.narrative, http, dex, cfg.general.chains, secrets.x_bearer_token, secrets.anthropic_api_key)
        if cfg.narrative.enabled
        else None
    )
    if cfg.narrative.use_llm and not secrets.anthropic_api_key:
        log.warning("narrative.use_llm is on but ANTHROPIC_API_KEY is missing; using heuristics only")
    model = LogisticModel.load(cfg.scoring.learned_model_path)
    if model:
        log.info("Loaded learned model trained on %d trades", model.n_samples)

    engine = Engine(cfg, journal, executor, dex, gecko, safety, notifier, kol, narrative, model)

    sources: list = []
    if cfg.sources.dexscreener:
        sources.append(DexScreenerScanner(dex, cfg.general.chains, cfg.sources.dexscreener_interval_sec))
    if cfg.sources.geckoterminal:
        sources.append(GeckoTerminalScanner(gecko, cfg.general.chains, cfg.sources.geckoterminal_interval_sec))
    if cfg.sources.pumpportal and "solana" in cfg.general.chains:
        on_new = None
        if narrative is not None:
            async def on_new(msg: dict) -> None:
                await narrative.on_new_launch(msg, engine.emit)
        sources.append(PumpPortalStream(on_new))
    if kol is not None:
        sources.append(kol)
    if narrative is not None:
        sources.append(narrative)
    return App(http, engine, sources, journal)
