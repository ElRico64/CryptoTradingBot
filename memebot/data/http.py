"""Shared async HTTP client with per-host rate limiting and retries."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any
from urllib.parse import urlparse

import aiohttp

log = logging.getLogger(__name__)

# Requests per second allowed per host; conservative relative to the public limits.
DEFAULT_RATE_LIMITS = {
    "api.dexscreener.com": 4.0,
    "api.geckoterminal.com": 0.45,
    "api.rugcheck.xyz": 2.0,
    "api.gopluslabs.io": 1.0,
    "lite-api.jup.ag": 1.5,
    "api.jup.ag": 5.0,
    "api.x.com": 0.5,
    "api.0x.org": 2.0,
}


class HttpError(Exception):
    def __init__(self, status: int, url: str, body: str = ""):
        super().__init__(f"HTTP {status} for {url}: {body[:300]}")
        self.status = status
        self.url = url
        self.body = body


class _HostLimiter:
    def __init__(self, rate_per_sec: float):
        self.interval = 1.0 / rate_per_sec
        self._next = 0.0
        self._lock = asyncio.Lock()

    async def wait(self) -> None:
        async with self._lock:
            now = time.monotonic()
            if self._next > now:
                await asyncio.sleep(self._next - now)
            self._next = max(now, self._next) + self.interval


class Http:
    def __init__(self, timeout: float = 12.0, retries: int = 2, rate_limits: dict[str, float] | None = None):
        self._timeout = aiohttp.ClientTimeout(total=timeout)
        self._retries = retries
        self._rates = {**DEFAULT_RATE_LIMITS, **(rate_limits or {})}
        self._limiters: dict[str, _HostLimiter] = {}
        self._session: aiohttp.ClientSession | None = None

    async def session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            # trust_env picks up HTTPS_PROXY and friends.
            self._session = aiohttp.ClientSession(timeout=self._timeout, trust_env=True)
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    async def _throttle(self, url: str) -> None:
        host = urlparse(url).hostname or ""
        rate = self._rates.get(host)
        if rate:
            limiter = self._limiters.setdefault(host, _HostLimiter(rate))
            await limiter.wait()

    async def request(
        self,
        method: str,
        url: str,
        *,
        params: dict | None = None,
        json: Any = None,
        headers: dict | None = None,
        as_text: bool = False,
    ) -> Any:
        last_exc: Exception | None = None
        for attempt in range(self._retries + 1):
            await self._throttle(url)
            try:
                sess = await self.session()
                async with sess.request(method, url, params=params, json=json, headers=headers) as resp:
                    body = await resp.text()
                    if resp.status == 429 or resp.status >= 500:
                        raise HttpError(resp.status, url, body)
                    if resp.status >= 400:
                        raise HttpError(resp.status, url, body)  # not retried below
                    if as_text:
                        return body
                    return await resp.json(content_type=None) if body else None
            except HttpError as exc:
                if exc.status != 429 and exc.status < 500:
                    raise
                last_exc = exc
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                last_exc = exc
            if attempt < self._retries:
                await asyncio.sleep(0.5 * 2**attempt)
        assert last_exc is not None
        raise last_exc

    async def get_json(self, url: str, params: dict | None = None, headers: dict | None = None) -> Any:
        return await self.request("GET", url, params=params, headers=headers)

    async def get_text(self, url: str, params: dict | None = None, headers: dict | None = None) -> str:
        return await self.request("GET", url, params=params, headers=headers, as_text=True)

    async def post_json(self, url: str, payload: Any, headers: dict | None = None) -> Any:
        return await self.request("POST", url, json=payload, headers=headers)
