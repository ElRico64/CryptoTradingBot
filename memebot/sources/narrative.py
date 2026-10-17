"""Narrative engine: catch the "Trump says superintelligence -> $SI" moment early.

Pipeline:
  1. Watch high-impact feeds: chosen X accounts, RSS/news feeds, and a manual file you can
     append headlines to.
  2. Turn each new post into narratives: phrases and the tickers degens will launch for them
     ("Super Intelligence" -> SI, SUPERINTELLIGENCE, ASI ...). Heuristics always run; Claude can
     optionally be asked to predict the tickers, which catches non-obvious ones.
  3. Immediately search DEXs for tokens matching those names, then keep re-searching while the
     narrative is hot, because the winning token is often launched minutes *after* the post.
  4. Every match goes through the normal safety + scoring pipeline; among copycats the one with
     real liquidity, volume and buyers wins, and only one position per narrative is allowed.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import re
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable

from ..config import NarrativeConfig, XAccount
from ..data.dexscreener import DexScreener
from ..data.http import Http
from ..models import Candidate

log = logging.getLogger(__name__)
Emit = Callable[[Candidate], Awaitable[None]]

STOPWORDS = {
    "THE", "A", "AN", "AND", "OR", "BUT", "OF", "TO", "IN", "ON", "AT", "FOR", "WITH", "IS", "ARE", "WAS",
    "WERE", "BE", "BEEN", "IT", "ITS", "THIS", "THAT", "THESE", "THOSE", "WE", "I", "YOU", "HE", "SHE",
    "THEY", "OUR", "MY", "YOUR", "HIS", "HER", "THEIR", "WILL", "WOULD", "CAN", "COULD", "SHOULD", "HAS",
    "HAVE", "HAD", "NOT", "NO", "YES", "SO", "VERY", "JUST", "ALL", "NEW", "NOW", "TODAY", "GREAT", "BIG",
    "MANY", "MUCH", "MORE", "MOST", "THANK", "THANKS", "AMERICA", "AMERICAN", "PEOPLE", "COUNTRY", "USA",
    "US", "MR", "MRS", "DR", "IF", "AS", "BY", "FROM", "UP", "OUT", "ABOUT", "WHAT", "WHO", "HOW", "WHY",
    "WHEN", "THERE", "HERE", "BEING", "DO", "DOES", "DID", "AM", "PM", "RT", "VIA", "AGAIN", "EVER",
    "NEVER", "EVERY", "ONE", "TWO", "FIRST", "LAST", "BEST", "WORLD", "TIME", "YEAR", "DAY", "BREAKING",
    "NEWS", "REPORT", "SAYS", "SAID", "OFFICIAL", "HTTPS", "HTTP", "WWW", "COM",
}


def normalize(text: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", text.upper())


@dataclass
class Post:
    source: str
    text: str
    url: str = ""
    ts: float = field(default_factory=time.time)
    weight: float = 1.0

    @property
    def id(self) -> str:
        return hashlib.sha1(f"{self.source}|{self.text}".encode()).hexdigest()[:16]


@dataclass
class Narrative:
    key: str
    phrases: list[str]
    tickers: list[str]
    strength: float
    source: str
    text: str
    url: str
    created: float
    expires: float

    def current_strength(self, now: float, half_life_hours: float) -> float:
        if now >= self.expires:
            return 0.0
        if self.source == "watchlist":
            return self.strength  # standing keywords don't fade
        age_h = max(0.0, (now - self.created) / 3600)
        return self.strength * math.pow(0.5, age_h / half_life_hours)


def _acronym(words: list[str]) -> str:
    return "".join(w[0] for w in words if w).upper()


def extract_keywords(text: str) -> tuple[list[str], list[str]]:
    """Heuristic phrase/ticker extraction from a post. Returns (phrases, tickers)."""
    phrases: list[str] = []
    tickers: list[str] = []
    clean = re.sub(r"https?://\S+", " ", text)

    tickers += [t.upper() for t in re.findall(r"\$([A-Za-z][A-Za-z0-9]{1,11})\b", clean)]
    for tag in re.findall(r"#([A-Za-z][A-Za-z0-9_]{2,30})", clean):
        phrases.append(tag)
        if len(tag) <= 10:
            tickers.append(tag.upper())
    phrases += [q.strip() for q in re.findall(r"[\"“]([^\"”]{3,40})[\"”]", clean)]

    # Runs of Capitalised Words (1-4), e.g. "Super Intelligence", "Golden Dome".
    for m in re.finditer(r"\b([A-Z][a-zA-Z]{1,20}(?:[\s-]+[A-Z][a-zA-Z]{1,20}){0,3})\b", clean):
        words = [w for w in re.split(r"[\s-]+", m.group(1)) if w.upper() not in STOPWORDS]
        if not words:
            continue
        if len(words) == 1 and len(words[0]) < 4:
            continue
        phrases.append(" ".join(words))
        # Acronyms only for Title Case runs ("Super Intelligence" -> SI); shouted words are handled below.
        if len(words) >= 2 and all(w[1:].islower() for w in words):
            tickers.append(_acronym(words))
            tickers.append(normalize("".join(words)))
    # ALL-CAPS words are how a lot of posts shout the key idea.
    for w in re.findall(r"\b([A-Z][A-Z0-9]{1,24})\b", clean):
        if w not in STOPWORDS:
            tickers.append(w)
            if len(w) >= 4:
                phrases.append(w.title())

    def dedupe(items: list[str], key) -> list[str]:
        seen, out = set(), []
        for it in items:
            k = key(it)
            if k and k not in seen:
                seen.add(k)
                out.append(it)
        return out

    return dedupe(phrases, normalize), dedupe([normalize(t) for t in tickers if 2 <= len(normalize(t)) <= 20], str)


class NarrativeStore:
    def __init__(self, cfg: NarrativeConfig):
        self.cfg = cfg
        self.items: dict[str, Narrative] = {}

    def add(self, phrases: list[str], tickers: list[str], strength: float, source: str, text: str, url: str = "",
            now: float | None = None) -> Narrative | None:
        now = now or time.time()
        if not phrases and not tickers:
            return None
        key = normalize(phrases[0]) if phrases else tickers[0]
        existing = self.items.get(key)
        if existing:
            existing.phrases = list(dict.fromkeys(existing.phrases + phrases))
            existing.tickers = list(dict.fromkeys(existing.tickers + tickers))
            existing.strength = min(1.0, max(existing.current_strength(now, self.cfg.half_life_hours), strength) + 0.1)
            existing.created = now
            existing.expires = now + self.cfg.ttl_hours * 3600
            return existing
        n = Narrative(key, phrases, tickers, min(1.0, strength), source, text[:280], url, now, now + self.cfg.ttl_hours * 3600)
        self.items[key] = n
        return n

    def active(self, now: float | None = None) -> list[Narrative]:
        now = now or time.time()
        for k in [k for k, n in self.items.items() if n.expires <= now]:
            del self.items[k]
        return list(self.items.values())

    def match(self, symbol: str, name: str, now: float | None = None) -> tuple[float, Narrative | None]:
        """How strongly a token's symbol/name matches a live narrative (0..1)."""
        now = now or time.time()
        sym, nm = normalize(symbol), normalize(name)
        best, best_n = 0.0, None
        for n in self.active(now):
            m = 0.0
            if sym and sym in n.tickers:
                m = 1.0 if len(sym) >= 3 else 0.85  # 2-letter tickers collide more often
            for p in n.phrases:
                pn = normalize(p)
                if not pn:
                    continue
                if pn in (nm, sym):
                    m = max(m, 0.95)
                elif len(pn) >= 5 and (pn in nm or (len(nm) >= 5 and nm in pn)):
                    m = max(m, 0.7)
            score = m * n.current_strength(now, self.cfg.half_life_hours)
            if score > best:
                best, best_n = score, n
        return best, best_n


class XFeed:
    """Recent posts from chosen X accounts via the official X API v2 (needs a bearer token)."""

    BASE = "https://api.x.com/2"

    def __init__(self, http: Http, bearer: str, accounts: list[XAccount]):
        self.http = http
        self.headers = {"Authorization": f"Bearer {bearer}"}
        self.accounts = {a.username.lower().lstrip("@"): a for a in accounts}
        self.ids: dict[str, str] = {}
        self.since: dict[str, str] = {}

    async def _resolve(self) -> None:
        missing = [u for u in self.accounts if u not in self.ids]
        if not missing:
            return
        data = await self.http.get_json(f"{self.BASE}/users/by", params={"usernames": ",".join(missing)}, headers=self.headers)
        for u in (data or {}).get("data") or []:
            self.ids[u["username"].lower()] = u["id"]

    async def poll(self) -> list[Post]:
        await self._resolve()
        posts: list[Post] = []
        for user, uid in self.ids.items():
            params = {"max_results": 5, "tweet.fields": "created_at", "exclude": "replies"}
            first = user not in self.since
            if not first:
                params["since_id"] = self.since[user]
            try:
                data = await self.http.get_json(f"{self.BASE}/users/{uid}/tweets", params=params, headers=self.headers)
            except Exception as exc:
                log.warning("X poll @%s failed: %s", user, exc)
                continue
            tweets = (data or {}).get("data") or []
            if tweets:
                self.since[user] = tweets[0]["id"]
            if first:
                continue  # don't trade on old posts at startup
            for t in tweets:
                posts.append(Post(f"x:@{user}", t["text"], f"https://x.com/{user}/status/{t['id']}", weight=self.accounts[user].weight))
        return posts


class RSSFeed:
    def __init__(self, http: Http, urls: list[str]):
        self.http = http
        self.urls = urls
        self.seen: set[str] = set()
        self.primed: set[str] = set()

    @staticmethod
    def parse(xml_text: str) -> list[tuple[str, str]]:
        """(text, link) for RSS <item> and Atom <entry> elements."""
        out = []
        try:
            root = ET.fromstring(xml_text)
        except ET.ParseError:
            return out
        for el in root.iter():
            tag = el.tag.split("}")[-1]
            if tag not in ("item", "entry"):
                continue
            fields: dict[str, str] = {}
            for child in el:
                ctag = child.tag.split("}")[-1]
                if ctag == "link" and child.get("href"):
                    fields["link"] = child.get("href", "")
                elif child.text:
                    fields[ctag] = child.text.strip()
            title = fields.get("title", "")
            desc = re.sub(r"<[^>]+>", " ", fields.get("description") or fields.get("summary") or "")[:400]
            out.append((f"{title}. {desc}".strip(". "), fields.get("link", "")))
        return out

    async def poll(self) -> list[Post]:
        posts = []
        for url in self.urls:
            try:
                items = self.parse(await self.http.get_text(url))
            except Exception as exc:
                log.warning("RSS %s failed: %s", url, exc)
                continue
            for text, link in items:
                post = Post(f"rss:{url}", text, link, weight=0.7)
                if post.id in self.seen:
                    continue
                self.seen.add(post.id)
                if url in self.primed:
                    posts.append(post)
            self.primed.add(url)
        return posts


class ManualFeed:
    """Append a line to narratives.txt (e.g. a headline you just saw) to inject it instantly."""

    def __init__(self, path: str):
        self.path = Path(path)
        self.offset = self.path.stat().st_size if self.path.exists() else 0

    async def poll(self) -> list[Post]:
        if not self.path.exists():
            return []
        size = self.path.stat().st_size
        if size < self.offset:
            self.offset = 0
        if size == self.offset:
            return []
        with self.path.open() as f:
            f.seek(self.offset)
            new = f.read()
            self.offset = f.tell()
        return [Post("manual", line.strip(), weight=1.0) for line in new.splitlines() if line.strip()]


LLM_SYSTEM = (
    "You are a memecoin narrative analyst. Given a social media post or headline, identify the "
    "themes that crypto degens are likely to launch memecoins about within minutes, and predict the "
    "exact token names and tickers they will use (tickers are short, uppercase, often acronyms or "
    "the key word itself, e.g. a post about 'super intelligence' -> SI, ASI, SUPERINTELLIGENCE). "
    "Most posts are not market-moving; say so when that is the case. Rate virality from 0 to 1, "
    "where 1 means a world-famous account coining a new, catchy, meme-able term."
)

LLM_SCHEMA = {
    "type": "object",
    "properties": {
        "market_moving": {"type": "boolean"},
        "narratives": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "theme": {"type": "string"},
                    "names": {"type": "array", "items": {"type": "string"}},
                    "tickers": {"type": "array", "items": {"type": "string"}},
                    "virality": {"type": "number"},
                },
                "required": ["theme", "names", "tickers", "virality"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["market_moving", "narratives"],
    "additionalProperties": False,
}


class LLMExtractor:
    """Optional: ask Claude which tickers a post will spawn (set narrative.use_llm and ANTHROPIC_API_KEY)."""

    def __init__(self, api_key: str, model: str, effort: str):
        from anthropic import AsyncAnthropic

        self.client = AsyncAnthropic(api_key=api_key, timeout=30.0)
        self.model = model
        self.effort = effort

    async def extract(self, post: Post) -> list[tuple[str, list[str], list[str], float]]:
        resp = await self.client.beta.messages.create(
            model=self.model,
            max_tokens=2000,
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            system=LLM_SYSTEM,
            output_config={"effort": self.effort, "format": {"type": "json_schema", "schema": LLM_SCHEMA}},
            messages=[{"role": "user", "content": f"Source: {post.source}\nPost: {post.text}"}],
        )
        if resp.stop_reason == "refusal":
            return []
        text = next((b.text for b in resp.content if b.type == "text"), "")
        data = json.loads(text) if text else {}
        if not data.get("market_moving"):
            return []
        out = []
        for n in data.get("narratives") or []:
            tickers = [normalize(t) for t in n.get("tickers") or [] if 2 <= len(normalize(t)) <= 20]
            out.append((n.get("theme", ""), n.get("names") or [], tickers, max(0.0, min(1.0, float(n.get("virality", 0))))))
        return out


class NarrativeEngine:
    def __init__(self, cfg: NarrativeConfig, http: Http, dex: DexScreener, chains: list[str],
                 x_bearer: str = "", anthropic_key: str = ""):
        self.cfg = cfg
        self.dex = dex
        self.chains = set(chains)
        self.store = NarrativeStore(cfg)
        self.feeds: list[tuple[object, float]] = []
        if cfg.x_accounts and x_bearer:
            self.feeds.append((XFeed(http, x_bearer, cfg.x_accounts), cfg.x_poll_interval_sec))
        elif cfg.x_accounts:
            log.warning("narrative.x_accounts set but X_BEARER_TOKEN missing; X feed disabled")
        if cfg.rss_feeds:
            self.feeds.append((RSSFeed(http, cfg.rss_feeds), cfg.rss_poll_interval_sec))
        if cfg.manual_file:
            self.feeds.append((ManualFeed(cfg.manual_file), 2.0))
        self.llm = LLMExtractor(anthropic_key, cfg.llm_model, cfg.llm_effort) if cfg.use_llm and anthropic_key else None
        self._last_search: dict[str, float] = {}
        for kw in cfg.watch_keywords:
            phrases, tickers = extract_keywords(kw)
            n = self.store.add(phrases or [kw], tickers or [normalize(kw)], 0.6, "watchlist", kw)
            if n:
                n.expires = float("inf")

    def strong_enough(self, score: float, n: Narrative) -> bool:
        """score is match quality x narrative strength; gate on the match quality part."""
        strength = n.current_strength(time.time(), self.cfg.half_life_hours)
        return strength > 0 and score / strength >= self.cfg.min_match

    async def ingest(self, post: Post, emit: Emit) -> list[Narrative]:
        added: list[Narrative] = []
        phrases, tickers = extract_keywords(post.text)
        # Heuristics alone are noisy; without the LLM they produce weaker narratives.
        base_strength = 0.5 * post.weight
        if self.llm is not None:
            try:
                for theme, names, llm_tickers, virality in await self.llm.extract(post):
                    n = self.store.add([theme, *names], llm_tickers, virality * post.weight, post.source, post.text, post.url)
                    if n:
                        added.append(n)
                base_strength *= 0.6
            except Exception as exc:
                log.warning("LLM narrative extraction failed: %s", exc)
        if phrases or tickers:
            n = self.store.add(phrases[:6], tickers[:10], base_strength, post.source, post.text, post.url)
            if n:
                added.append(n)
        for n in added:
            log.info("Narrative from %s: %s tickers=%s strength=%.2f", post.source, n.key, n.tickers[:6], n.strength)
            await self.search(n, emit)
        return added

    async def search(self, n: Narrative, emit: Emit) -> int:
        self._last_search[n.key] = time.time()
        queries = list(dict.fromkeys(n.tickers[:5] + n.phrases[:3]))
        found = 0
        for q in queries:
            try:
                pairs = await self.dex.search(q)
            except Exception as exc:
                log.debug("search %s failed: %s", q, exc)
                continue
            for p in pairs:
                if p.chain not in self.chains:
                    continue
                score, matched = self.store.match(p.symbol, p.name)
                if matched is n and self.strong_enough(score, n):
                    found += 1
                    await emit(Candidate(p.chain, p.token_address, "narrative", p.symbol, p.name,
                                         context={"narrative": n.key, "narrative_match": score, "post": n.url or n.text[:120]}))
        return found

    async def on_new_launch(self, msg: dict, emit: Emit) -> None:
        """pump.fun create events: flag launches that ride a live narrative."""
        score, n = self.store.match(msg.get("symbol", ""), msg.get("name", ""))
        if n and self.strong_enough(score, n):
            await emit(Candidate("solana", msg["mint"], "narrative", msg.get("symbol", ""), msg.get("name", ""),
                                 context={"narrative": n.key, "narrative_match": score, "post": n.url or n.text[:120]}))

    async def _feed_loop(self, feed, interval: float, emit: Emit) -> None:
        while True:
            try:
                for post in await feed.poll():
                    await self.ingest(post, emit)
            except Exception as exc:
                log.warning("narrative feed error: %s", exc)
            await asyncio.sleep(interval)

    async def _research_loop(self, emit: Emit) -> None:
        while True:
            await asyncio.sleep(self.cfg.research_interval_sec)
            now = time.time()
            for n in self.store.active(now):
                # Hot narratives are re-searched constantly; standing watch keywords every 5 min.
                every = 300.0 if n.source == "watchlist" else self.cfg.research_interval_sec
                if n.current_strength(now, self.cfg.half_life_hours) >= 0.1 and now - self._last_search.get(n.key, 0) >= every:
                    await self.search(n, emit)

    async def run(self, emit: Emit) -> None:
        tasks = [asyncio.create_task(self._feed_loop(f, i, emit)) for f, i in self.feeds]
        tasks.append(asyncio.create_task(self._research_loop(emit)))
        await asyncio.gather(*tasks)
