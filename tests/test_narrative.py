import asyncio
import time
from types import SimpleNamespace

from memebot.config import NarrativeConfig
from memebot.data.dexscreener import DexScreener
from memebot.sources.narrative import (
    LLMExtractor,
    ManualFeed,
    NarrativeEngine,
    NarrativeStore,
    Post,
    RSSFeed,
    extract_keywords,
)

from .fakes import FakeHttp, make_pair, pair_json

SI_POST = "We will name it Super Intelligence. AMERICA will lead the world in AI SUPERINTELLIGENCE!"


def test_extracts_si_from_trump_style_post():
    phrases, tickers = extract_keywords(SI_POST)
    assert "SI" in tickers and "SUPERINTELLIGENCE" in tickers
    assert "Super Intelligence" in phrases
    assert "AMERICA" not in tickers  # stopword


def test_cashtags_and_hashtags():
    phrases, tickers = extract_keywords("buying $WIF and #GoldenDome https://t.co/abc")
    assert "WIF" in tickers and "GOLDENDOME" in tickers


def test_store_matching_and_decay():
    cfg = NarrativeConfig(half_life_hours=1.0, ttl_hours=6.0)
    store = NarrativeStore(cfg)
    now = 1_700_000_000.0
    phrases, tickers = extract_keywords(SI_POST)
    store.add(phrases, tickers, 1.0, "x:@realDonaldTrump", SI_POST, now=now)
    s, n = store.match("SI", "Super Intelligence", now=now)
    assert s >= 0.95 and n is not None
    s2, _ = store.match("ASI", "superintelligence coin", now=now)
    assert 0.6 < s2 < 1.0
    assert store.match("BONK", "Bonk", now=now)[0] == 0.0
    later, _ = store.match("SI", "Super Intelligence", now=now + 3600)
    assert abs(later - s / 2) < 1e-6
    assert store.match("SI", "x", now=now + 7 * 3600)[0] == 0.0  # expired


def test_rss_parse():
    xml = """<rss><channel><item><title>Trump unveils Golden Dome</title><description>&lt;p&gt;Big news&lt;/p&gt;</description>
    <link>https://n.example/1</link></item></channel></rss>"""
    items = RSSFeed.parse(xml)
    assert items[0][0].startswith("Trump unveils Golden Dome") and items[0][1] == "https://n.example/1"


def test_manual_feed(tmp_path):
    f = tmp_path / "n.txt"
    f.write_text("old line\n")
    feed = ManualFeed(str(f))
    assert asyncio.run(feed.poll()) == []
    with f.open("a") as fh:
        fh.write("Elon names his dog Floki Junior\n")
    posts = asyncio.run(feed.poll())
    assert [p.text for p in posts] == ["Elon names his dog Floki Junior"]


def test_engine_ingest_searches_and_emits_matching_tokens():
    si = make_pair(address="SIMint", symbol="SI", name="Super Intelligence")
    fake_si = make_pair(address="OtherMint", symbol="BONK", name="Bonk")
    evm = make_pair(address="0xabc", chain="base", symbol="SI", name="Super Intelligence")
    http = FakeHttp([("GET", "/latest/dex/search", {"pairs": [pair_json(si), pair_json(fake_si), pair_json(evm)]})])
    eng = NarrativeEngine(NarrativeConfig(manual_file=""), http, DexScreener(http), ["solana"])
    out = []

    async def emit(c):
        out.append(c)

    asyncio.run(eng.ingest(Post("x:@realDonaldTrump", SI_POST, weight=1.0), emit))
    addrs = {c.address for c in out}
    assert addrs == {"SIMint"}  # wrong name and wrong chain are ignored
    assert out[0].source == "narrative" and out[0].context["narrative_match"] > 0


def test_pump_launch_matching_narrative():
    eng = NarrativeEngine(NarrativeConfig(manual_file="", watch_keywords=["Golden Dome"]), FakeHttp(), None, ["solana"])
    out = []

    async def emit(c):
        out.append(c)

    asyncio.run(eng.on_new_launch({"mint": "GDMint", "symbol": "GDOME", "name": "Golden Dome"}, emit))
    asyncio.run(eng.on_new_launch({"mint": "Other", "symbol": "CAT", "name": "Cat"}, emit))
    assert [c.address for c in out] == ["GDMint"]


def test_llm_extractor_parses_structured_output():
    calls = {}

    async def fake_create(**kwargs):
        calls.update(kwargs)
        text = '{"market_moving": true, "narratives": [{"theme": "superintelligence", "names": ["Super Intelligence"], "tickers": ["SI", "$ASI"], "virality": 1.4}]}'
        return SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(type="text", text=text)])

    ex = LLMExtractor.__new__(LLMExtractor)
    ex.client = SimpleNamespace(beta=SimpleNamespace(messages=SimpleNamespace(create=fake_create)))
    ex.model, ex.effort = "claude-opus-5-5", "low"
    res = asyncio.run(ex.extract(Post("x", SI_POST)))
    assert res == [("superintelligence", ["Super Intelligence"], ["SI", "ASI"], 1.0)]
    assert calls["output_config"]["format"]["type"] == "json_schema"
    assert calls["fallbacks"] == "default"


def test_watchlist_never_decays():
    eng = NarrativeEngine(NarrativeConfig(manual_file="", watch_keywords=["Golden Dome"], half_life_hours=0.1), FakeHttp(), None, ["solana"])
    s, _ = eng.store.match("GD", "Golden Dome", now=time.time() + 10 * 3600)
    assert s > 0.5
