"""Executor interface shared by paper and live trading."""

from __future__ import annotations

from abc import ABC, abstractmethod

from ..models import Fill


class ExecutionError(Exception):
    pass


class Executor(ABC):
    @abstractmethod
    async def buy(self, chain: str, token: str, usd: float) -> Fill: ...

    @abstractmethod
    async def sell(self, chain: str, token: str, fraction: float) -> Fill:
        """Sell `fraction` (0..1] of the wallet's current holding of `token`."""

    @abstractmethod
    async def roundtrip_loss_pct(self, chain: str, token: str, usd: float) -> float:
        """Quote a buy of `usd` then a sell of the proceeds; return % lost (honeypot/tax detector)."""

    @abstractmethod
    async def cash_usd(self, chain: str) -> float: ...

    async def close(self) -> None:
        return None


class ExecutorRouter(Executor):
    """Dispatches to the executor configured for each chain."""

    def __init__(self, executors: dict[str, Executor]):
        self.executors = executors

    def _get(self, chain: str) -> Executor:
        if chain not in self.executors:
            raise ExecutionError(f"No executor configured for chain {chain}")
        return self.executors[chain]

    async def buy(self, chain: str, token: str, usd: float) -> Fill:
        return await self._get(chain).buy(chain, token, usd)

    async def sell(self, chain: str, token: str, fraction: float) -> Fill:
        return await self._get(chain).sell(chain, token, fraction)

    async def roundtrip_loss_pct(self, chain: str, token: str, usd: float) -> float:
        return await self._get(chain).roundtrip_loss_pct(chain, token, usd)

    async def cash_usd(self, chain: str) -> float:
        return await self._get(chain).cash_usd(chain)

    async def total_cash_usd(self) -> float:
        seen: set[int] = set()
        total = 0.0
        for chain, ex in self.executors.items():
            if id(ex) in seen and getattr(ex, "shared_cash", False):
                continue
            seen.add(id(ex))
            total += await ex.cash_usd(chain)
        return total

    async def close(self) -> None:
        for ex in {id(e): e for e in self.executors.values()}.values():
            await ex.close()
