"""Core data types shared across the bot."""

from __future__ import annotations

import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any

from .config import EVM_CHAINS


def norm_addr(chain: str, address: str) -> str:
    """EVM addresses are case-insensitive; Solana base58 addresses are not."""
    return address.lower() if chain in EVM_CHAINS else address


def token_key(chain: str, address: str) -> str:
    return f"{chain}:{norm_addr(chain, address)}"


@dataclass
class PairSnapshot:
    chain: str
    token_address: str
    pair_address: str
    dex_id: str
    symbol: str
    name: str
    price_usd: float
    liquidity_usd: float
    market_cap: float
    fdv: float
    created_at_ms: int | None
    txns: dict[str, dict[str, int]]
    volume: dict[str, float]
    price_change: dict[str, float]
    quote_symbol: str = ""
    boosts: int = 0
    socials: int = 0
    websites: int = 0
    url: str = ""
    fetched_at: float = field(default_factory=time.time)

    @property
    def age_minutes(self) -> float | None:
        if not self.created_at_ms:
            return None
        return max(0.0, (self.fetched_at * 1000 - self.created_at_ms) / 60_000)

    def buys(self, window: str) -> int:
        return int(self.txns.get(window, {}).get("buys", 0) or 0)

    def sells(self, window: str) -> int:
        return int(self.txns.get(window, {}).get("sells", 0) or 0)

    def txn_count(self, window: str) -> int:
        return self.buys(window) + self.sells(window)

    @property
    def mcap(self) -> float:
        return self.market_cap or self.fdv


@dataclass
class Candidate:
    chain: str
    address: str
    source: str
    symbol: str = ""
    name: str = ""
    context: dict[str, Any] = field(default_factory=dict)
    ts: float = field(default_factory=time.time)

    @property
    def key(self) -> str:
        return token_key(self.chain, self.address)


@dataclass
class SafetyReport:
    ok: bool
    reasons: list[str] = field(default_factory=list)  # hard failures
    warnings: list[str] = field(default_factory=list)
    data: dict[str, Any] = field(default_factory=dict)


@dataclass
class Fill:
    qty: float  # tokens bought or sold, UI units
    usd: float  # USD spent on a buy / received from a sell, net of fees
    price: float  # effective USD per token
    tx: str = ""


@dataclass
class Position:
    chain: str
    token_address: str
    symbol: str
    pair_address: str
    entry_price: float
    entry_ts: float
    cost_usd: float
    qty: float
    entry_liquidity_usd: float
    score: float
    sources: list[str] = field(default_factory=list)
    initial_qty: float = 0.0
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    realized_usd: float = 0.0
    peak_price: float = 0.0
    tp_hits: list[float] = field(default_factory=list)
    status: str = "open"
    exit_reason: str = ""
    closed_ts: float | None = None
    narrative: str = ""
    kol_wallets: list[str] = field(default_factory=list)
    signal_id: int | None = None
    last_price: float = 0.0
    missing_checks: int = 0

    def __post_init__(self) -> None:
        if not self.initial_qty:
            self.initial_qty = self.qty
        if not self.peak_price:
            self.peak_price = self.entry_price
        if not self.last_price:
            self.last_price = self.entry_price

    @property
    def key(self) -> str:
        return token_key(self.chain, self.token_address)

    def multiple(self, price: float) -> float:
        return price / self.entry_price if self.entry_price > 0 else 0.0

    @property
    def peak_multiple(self) -> float:
        return self.multiple(self.peak_price)

    @property
    def remaining_fraction(self) -> float:
        return self.qty / self.initial_qty if self.initial_qty > 0 else 0.0

    def value_usd(self, price: float | None = None) -> float:
        return self.qty * (self.last_price if price is None else price)

    def pnl_usd(self, price: float | None = None) -> float:
        return self.realized_usd + self.value_usd(price) - self.cost_usd

    @property
    def realized_multiple(self) -> float:
        return self.realized_usd / self.cost_usd if self.cost_usd > 0 else 0.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Position":
        return cls(**data)
