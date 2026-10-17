"""Command line: run the bot, inspect a token, test a narrative, backtest exits, train, stats."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path

from dotenv import load_dotenv

from .analysis.learner import LogisticModel, model_inputs
from .config import Config, Secrets, load_config
from .util import setup_logging

log = logging.getLogger("memebot")


def _load(args) -> tuple[Config, Secrets]:
    load_dotenv(args.env)
    if args.config == "config.yaml" and not Path(args.config).exists():
        print("config.yaml not found; using built-in defaults (copy config.example.yaml to customise)", file=sys.stderr)
        args.config = None
    cfg = load_config(args.config)
    setup_logging(cfg.general.log_level if not args.verbose else "DEBUG")
    return cfg, Secrets.from_env()


async def cmd_run(cfg: Config, secrets: Secrets) -> None:
    from .app import build_app

    app = build_app(cfg, secrets)
    mode = cfg.general.mode.upper()
    log.info("memebot starting in %s mode on %s", mode, ", ".join(cfg.general.chains))
    if cfg.general.mode == "live":
        log.warning("LIVE MODE: real funds will be traded.")
    await app.engine.notifier.send(f"memebot started ({mode}, chains: {', '.join(cfg.general.chains)})")
    try:
        await app.engine.run(app.sources)
    finally:
        await app.close()


async def cmd_check(cfg: Config, secrets: Secrets, chain: str, address: str) -> None:
    from .app import build_app

    cfg.general.mode = "paper"  # analysis only, never trades
    app = build_app(cfg, secrets)
    try:
        a = await app.engine.analyze(chain, address)
        p = a.pair
        print(f"\n{chain}:{address}")
        if p:
            print(f"  {p.symbol} / {p.name} on {p.dex_id}  {p.url}")
            print(f"  price ${p.price_usd:.10g}  liq ${p.liquidity_usd:,.0f}  mcap ${p.mcap:,.0f}  age {p.age_minutes or 0:.0f}m")
            print(f"  1h txns {p.txn_count('h1')} (buys {p.buys('h1')})  1h vol ${p.volume.get('h1', 0):,.0f}  1h change {p.price_change.get('h1', 0):+.1f}%")
        if a.safety:
            print(f"  safety: {'PASS' if a.safety.ok else 'FAIL'} {a.safety.reasons or ''} warnings={a.safety.warnings} {a.safety.data}")
        if a.pattern:
            print(f"  chart: {a.pattern.score:.0f} {a.pattern.tags} {a.pattern.metrics}")
        if a.result:
            print(f"  components: {json.dumps(a.result.components)}")
            print(f"  confluence: {a.result.confluence}")
            print(f"  SCORE {a.result.score:.1f} -> {a.decision.upper()} ({a.reason})")
        else:
            print(f"  -> SKIP ({a.reason})")
    finally:
        await app.close()


async def cmd_narrative(cfg: Config, secrets: Secrets, text: str) -> None:
    from .data.dexscreener import DexScreener
    from .data.http import Http
    from .models import Candidate
    from .sources.narrative import NarrativeEngine, Post, extract_keywords

    phrases, tickers = extract_keywords(text)
    print(f"heuristic phrases: {phrases}\nheuristic tickers: {tickers}")
    http = Http()
    cfg.narrative.manual_file = ""
    eng = NarrativeEngine(cfg.narrative, http, DexScreener(http), cfg.general.chains, "", secrets.anthropic_api_key)
    found: list[Candidate] = []

    async def collect(c: Candidate) -> None:
        found.append(c)

    try:
        narratives = await eng.ingest(Post("manual", text, weight=1.0), collect)
        for n in narratives:
            print(f"narrative {n.key}: tickers={n.tickers} phrases={n.phrases} strength={n.strength:.2f}")
        print(f"\n{len(found)} matching token(s) on {cfg.general.chains}:")
        for c in found:
            print(f"  {c.chain} {c.symbol:<12} {c.name[:30]:<30} match={c.context['narrative_match']:.2f} {c.address}")
    finally:
        await http.close()


async def cmd_backtest(cfg: Config, args) -> None:
    from .backtest import load_csv, simulate

    if args.csv:
        candles = load_csv(args.csv)
    else:
        from .data.geckoterminal import GeckoTerminal
        from .data.http import Http

        http = Http()
        try:
            candles = await GeckoTerminal(http).ohlcv(args.chain, args.pool, limit=1000)
        finally:
            await http.close()
    if len(candles) <= args.entry_index + 1:
        sys.exit("not enough candles")
    r = simulate(candles, cfg.exits, args.entry_index, cfg.execution.paper_fee_pct)
    for e in r.events:
        print("  " + e)
    print(f"result: {r.multiple:.2f}x (peak {r.peak_multiple:.2f}x, held {r.minutes_held:.0f}m, exit: {r.exit_reason})")


def cmd_train(cfg: Config) -> None:
    import numpy as np

    from .journal import Journal

    rows = Journal(cfg.general.db_path).training_rows()
    if len(rows) < 15:
        sys.exit(f"only {len(rows)} closed trades in the journal; need at least 15 (40+ before the model is used)")
    X = np.array([model_inputs(f) for f, _c, _m in rows])
    y = np.array([1.0 if m >= 1.3 else 0.0 for _f, _c, m in rows])
    model = LogisticModel.fit(X, y)
    model.save(cfg.scoring.learned_model_path)
    print(f"trained on {len(rows)} trades ({int(y.sum())} winners); saved to {cfg.scoring.learned_model_path}")
    for name, w in model.explain():
        print(f"  {name:<16} {w:+.3f}")


def cmd_stats(cfg: Config) -> None:
    from .journal import Journal

    j = Journal(cfg.general.db_path)
    for k, v in j.stats().items():
        print(f"{k:>18}: {v:.3f}" if isinstance(v, float) else f"{k:>18}: {v}")
    print("\nrecent trades:")
    for t in j.recent_trades(15):
        print(f"  {t['side']:<4} {t['symbol']:<10} ${t['usd']:>9.2f}  {t['reason']}")


async def cmd_wallet(cfg: Config, secrets: Secrets) -> None:
    from .data.http import Http
    from .data.solana_rpc import SolanaRPC

    http = Http()
    try:
        if secrets.solana_private_key:
            from .execution.jupiter import load_keypair

            kp = load_keypair(secrets.solana_private_key)
            lamports = await SolanaRPC(http, secrets.solana_rpc_url).get_balance_lamports(str(kp.pubkey()))
            print(f"solana  {kp.pubkey()}  {lamports / 1e9:.4f} SOL")
        if secrets.evm_private_key:
            from eth_account import Account

            print(f"evm     {Account.from_key(secrets.evm_private_key).address}")
        if not (secrets.solana_private_key or secrets.evm_private_key):
            print("no wallet keys configured (paper mode only)")
    finally:
        await http.close()


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="memebot", description="Memecoin trading bot")
    ap.add_argument("-c", "--config", default="config.yaml")
    ap.add_argument("--env", default=".env")
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("run", help="run the bot (paper or live, per config)")
    chk = sub.add_parser("check", help="analyse one token without trading")
    chk.add_argument("chain")
    chk.add_argument("address")
    nar = sub.add_parser("narrative", help="test how a post/headline is turned into tokens to watch")
    nar.add_argument("text")
    bt = sub.add_parser("backtest", help="replay the exit strategy over candles")
    bt.add_argument("--csv")
    bt.add_argument("--chain", default="solana")
    bt.add_argument("--pool")
    bt.add_argument("--entry-index", type=int, default=0)
    sub.add_parser("train", help="fit the learned model from closed trades")
    sub.add_parser("stats", help="performance summary from the journal")
    sub.add_parser("wallet", help="show configured wallet addresses and balance")
    args = ap.parse_args(argv)
    if args.cmd == "backtest" and not (args.csv or args.pool):
        ap.error("backtest needs --csv FILE or --pool POOL_ADDRESS")

    cfg, secrets = _load(args)
    try:
        if args.cmd == "run":
            asyncio.run(cmd_run(cfg, secrets))
        elif args.cmd == "check":
            asyncio.run(cmd_check(cfg, secrets, args.chain, args.address))
        elif args.cmd == "narrative":
            asyncio.run(cmd_narrative(cfg, secrets, args.text))
        elif args.cmd == "backtest":
            asyncio.run(cmd_backtest(cfg, args))
        elif args.cmd == "train":
            cmd_train(cfg)
        elif args.cmd == "stats":
            cmd_stats(cfg)
        elif args.cmd == "wallet":
            asyncio.run(cmd_wallet(cfg, secrets))
    except KeyboardInterrupt:
        print("\nstopped")
