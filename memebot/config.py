"""Configuration: strategy knobs come from YAML, secrets come from the environment."""

from __future__ import annotations

import os
import types
from dataclasses import dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any, Union, get_args, get_origin, get_type_hints

import yaml

EVM_CHAINS = ("ethereum", "base", "bsc")
SUPPORTED_CHAINS = ("solana",) + EVM_CHAINS


@dataclass
class GeneralConfig:
    mode: str = "paper"  # "paper" or "live"
    chains: list[str] = field(default_factory=lambda: ["solana"])
    db_path: str = "memebot.db"
    log_level: str = "INFO"
    monitor_interval_sec: float = 3.0
    status_interval_sec: float = 60.0
    # The same token is not re-analysed more often than this unless a new source mentions it.
    reevaluate_after_sec: float = 300.0
    # Sources that mention a token within this window count toward confluence together.
    confluence_window_sec: float = 1800.0
    analysis_workers: int = 4


@dataclass
class RiskConfig:
    paper_starting_balance_usd: float = 1000.0
    position_pct: float = 4.0  # % of equity for a normal-conviction entry
    min_position_usd: float = 10.0
    max_position_usd: float = 500.0
    max_pool_liquidity_pct: float = 1.5  # never buy more than this % of pool liquidity
    max_open_positions: int = 4
    max_positions_per_narrative: int = 1
    max_trades_per_day: int = 8
    max_daily_loss_pct: float = 12.0  # kill switch: no new entries for the rest of the UTC day
    loss_streak_pause: int = 3  # consecutive losing trades that trigger a cooling-off pause
    loss_streak_pause_min: float = 90.0
    min_cash_reserve_usd: float = 5.0  # keep this much for fees/gas


@dataclass
class FilterConfig:
    min_liquidity_usd: float = 20_000
    max_liquidity_usd: float = 5_000_000
    min_market_cap_usd: float = 40_000
    max_market_cap_usd: float = 25_000_000
    max_age_hours: float = 72.0
    min_txns_h1: int = 150
    min_volume_h1_usd: float = 25_000
    min_liquidity_to_mcap: float = 0.03
    max_price_change_h1_pct: float = 800.0  # don't buy the top of a vertical candle
    quote_symbols: list[str] = field(
        default_factory=lambda: ["SOL", "WSOL", "USDC", "USDT", "WETH", "ETH", "WBNB", "BNB"]
    )


@dataclass
class SafetyConfig:
    fail_closed: bool = True  # if a safety API is unreachable, skip the token
    require_mint_revoked: bool = True
    require_freeze_revoked: bool = True
    max_top10_holders_pct: float = 30.0
    max_insider_pct: float = 15.0
    max_rugcheck_risk: float = 60.0  # rugcheck score_normalised (0 = clean, 100 = worst)
    max_buy_tax_pct: float = 5.0
    max_sell_tax_pct: float = 5.0
    max_roundtrip_loss_pct: float = 12.0  # buy-then-sell quote; catches honeypots and hidden taxes
    min_holders: int = 150


@dataclass
class ScoringConfig:
    entry_threshold: float = 72.0
    strong_threshold: float = 85.0  # at or above this, one confirming source is enough
    min_confluence: int = 2
    weights: dict[str, float] = field(
        default_factory=lambda: {
            "momentum": 1.0,
            "order_flow": 1.4,
            "liquidity": 1.0,
            "earliness": 0.8,
            "pattern": 1.4,
            "safety": 1.0,
        }
    )
    kol_bonus_max: float = 15.0
    narrative_bonus_max: float = 15.0
    use_ohlcv_patterns: bool = True
    learned_model_path: str = "model.json"
    learned_model_weight: float = 0.3  # blend of the trained model once it has enough samples
    learned_model_min_samples: int = 40


@dataclass
class TakeProfit:
    multiple: float
    sell_fraction: float  # fraction of what is still held when this level hits


@dataclass
class TrailTier:
    from_multiple: float  # applies once the peak has reached this multiple of entry
    drawdown_pct: float  # exit everything if price falls this far below the peak


@dataclass
class ExitConfig:
    stop_loss_pct: float = 30.0
    take_profits: list[TakeProfit] = field(
        default_factory=lambda: [
            TakeProfit(2.0, 0.5),  # at 2x take the initial stake back: the rest is house money
            TakeProfit(5.0, 0.5),
            TakeProfit(10.0, 0.5),
        ]
    )
    # After the first take-profit the remainder may never close below this multiple of entry.
    breakeven_floor_multiple: float = 1.1
    trail_tiers: list[TrailTier] = field(
        default_factory=lambda: [
            TrailTier(1.6, 35.0),
            TrailTier(3.0, 30.0),
            TrailTier(6.0, 25.0),
            TrailTier(15.0, 20.0),
        ]
    )
    time_stop_min: float = 30.0  # exit if it hasn't moved by then...
    time_stop_min_peak_multiple: float = 1.25  # ...meaning the peak never reached this
    max_hold_hours: float = 24.0
    liquidity_drop_exit_pct: float = 40.0  # LP pulled / drained
    kol_exit_min_sellers: int = 1  # copy-trade entries exit when this many source KOLs sell
    flow_reversal_exit: bool = True  # before TP1, exit losers when sellers take over
    flow_reversal_buy_ratio: float = 0.32
    missing_price_exit_checks: int = 20  # pair vanished from the data feed


@dataclass
class ExecutionConfig:
    slippage_bps: int = 1500
    sell_slippage_bps_max: int = 4900
    max_price_impact_pct: float = 4.0
    priority_fee_max_lamports: int = 3_000_000
    priority_level: str = "veryHigh"
    skip_preflight: bool = True
    confirm_timeout_sec: float = 60.0
    jupiter_base_url: str = "https://lite-api.jup.ag"
    paper_fee_pct: float = 1.0  # DEX + priority fees, modelled per side
    paper_extra_slippage_pct: float = 1.0  # latency slippage on top of modelled price impact


@dataclass
class KolWallet:
    address: str
    name: str = ""
    weight: float = 1.0


@dataclass
class KOLConfig:
    enabled: bool = False
    wallets: list[KolWallet] = field(default_factory=list)
    min_weight: float = 2.0  # sum of weights of distinct KOLs buying inside the window
    window_min: float = 30.0
    min_buy_sol: float = 0.5
    poll_interval_sec: float = 8.0


@dataclass
class XAccount:
    username: str
    weight: float = 1.0


@dataclass
class NarrativeConfig:
    enabled: bool = True
    x_accounts: list[XAccount] = field(default_factory=list)
    x_poll_interval_sec: float = 60.0
    rss_feeds: list[str] = field(default_factory=list)
    rss_poll_interval_sec: float = 60.0
    manual_file: str = "narratives.txt"  # append a headline here to inject a narrative instantly
    watch_keywords: list[str] = field(default_factory=list)
    ttl_hours: float = 12.0
    half_life_hours: float = 3.0
    research_interval_sec: float = 45.0
    min_match: float = 0.6
    use_llm: bool = False
    llm_model: str = "claude-opus-5-5"
    llm_effort: str = "low"


@dataclass
class SourcesConfig:
    dexscreener: bool = True
    dexscreener_interval_sec: float = 20.0
    geckoterminal: bool = True
    geckoterminal_interval_sec: float = 60.0
    pumpportal: bool = True


@dataclass
class NotifyConfig:
    telegram: bool = False


@dataclass
class Config:
    general: GeneralConfig = field(default_factory=GeneralConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    filters: FilterConfig = field(default_factory=FilterConfig)
    safety: SafetyConfig = field(default_factory=SafetyConfig)
    scoring: ScoringConfig = field(default_factory=ScoringConfig)
    exits: ExitConfig = field(default_factory=ExitConfig)
    execution: ExecutionConfig = field(default_factory=ExecutionConfig)
    kol: KOLConfig = field(default_factory=KOLConfig)
    narrative: NarrativeConfig = field(default_factory=NarrativeConfig)
    sources: SourcesConfig = field(default_factory=SourcesConfig)
    notify: NotifyConfig = field(default_factory=NotifyConfig)

    def validate(self) -> None:
        if self.general.mode not in ("paper", "live"):
            raise ValueError("general.mode must be 'paper' or 'live'")
        for chain in self.general.chains:
            if chain not in SUPPORTED_CHAINS:
                raise ValueError(f"Unsupported chain {chain!r}; choose from {SUPPORTED_CHAINS}")
        if not 0 < self.exits.stop_loss_pct < 100:
            raise ValueError("exits.stop_loss_pct must be between 0 and 100")
        last = 1.0
        for tp in self.exits.take_profits:
            if tp.multiple <= last:
                raise ValueError("exits.take_profits must be in increasing multiple order, all > 1")
            if not 0 < tp.sell_fraction <= 1:
                raise ValueError("take profit sell_fraction must be in (0, 1]")
            last = tp.multiple
        if self.risk.position_pct <= 0 or self.risk.position_pct > 25:
            raise ValueError("risk.position_pct must be in (0, 25]")


@dataclass
class Secrets:
    solana_private_key: str = ""
    solana_rpc_url: str = "https://api.mainnet-beta.solana.com"
    solana_ws_url: str = ""
    evm_private_key: str = ""
    evm_rpc_urls: dict[str, str] = field(default_factory=dict)
    zeroex_api_key: str = ""
    jupiter_api_key: str = ""
    x_bearer_token: str = ""
    anthropic_api_key: str = ""
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    live_ack: bool = False

    @classmethod
    def from_env(cls) -> "Secrets":
        env = os.environ.get
        return cls(
            solana_private_key=env("SOLANA_PRIVATE_KEY", ""),
            solana_rpc_url=env("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com"),
            solana_ws_url=env("SOLANA_WS_URL", ""),
            evm_private_key=env("EVM_PRIVATE_KEY", ""),
            evm_rpc_urls={
                c: env(f"EVM_RPC_URL_{c.upper()}", "") for c in EVM_CHAINS if env(f"EVM_RPC_URL_{c.upper()}")
            },
            zeroex_api_key=env("ZEROX_API_KEY", ""),
            jupiter_api_key=env("JUPITER_API_KEY", ""),
            x_bearer_token=env("X_BEARER_TOKEN", ""),
            anthropic_api_key=env("ANTHROPIC_API_KEY", ""),
            telegram_bot_token=env("TELEGRAM_BOT_TOKEN", ""),
            telegram_chat_id=env("TELEGRAM_CHAT_ID", ""),
            live_ack=env("MEMEBOT_I_ACCEPT_THE_RISK", "").lower() == "yes",
        )


def _coerce(tp: Any, value: Any) -> Any:
    if is_dataclass(tp):
        return build_dataclass(tp, value)
    origin = get_origin(tp)
    if origin is list:
        (inner,) = get_args(tp)
        return [_coerce(inner, v) for v in (value or [])]
    if origin is dict:
        return dict(value or {})
    if origin in (Union, types.UnionType):
        if value is None:
            return None
        inner = [a for a in get_args(tp) if a is not type(None)][0]
        return _coerce(inner, value)
    if tp is bool and isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    if tp in (int, float, str, bool):
        return tp(value)
    return value


def build_dataclass(cls: type, data: dict | None) -> Any:
    data = data or {}
    if not isinstance(data, dict):
        raise ValueError(f"Expected a mapping for {cls.__name__}, got {type(data).__name__}")
    hints = get_type_hints(cls)
    known = {f.name for f in fields(cls)}
    unknown = set(data) - known
    if unknown:
        raise ValueError(f"Unknown config keys for {cls.__name__}: {sorted(unknown)}")
    return cls(**{name: _coerce(hints[name], value) for name, value in data.items()})


def load_config(path: str | Path | None) -> Config:
    data: dict = {}
    if path:
        p = Path(path)
        if p.exists():
            data = yaml.safe_load(p.read_text()) or {}
        else:
            raise FileNotFoundError(f"Config file not found: {p}")
    cfg = build_dataclass(Config, data)
    cfg.validate()
    return cfg
