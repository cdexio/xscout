import base64
import hashlib

import pytest

from xscout.xweb.tid import EPOCH_OFFSET, KEYWORD, TidGenerator, TidProvider

VK = list(range(48))
ANIM = "4fc59800a3d70a3d70a3d81100a3d70a3d70a3d800"


def decode(tid: str) -> list[int]:
    raw = base64.b64decode(tid + "=" * (-len(tid) % 4))
    key = raw[0]
    return [b ^ key for b in raw[1:]]


def test_calc_matches_the_documented_layout():
    gen = TidGenerator(VK, ANIM, "test")
    now = EPOCH_OFFSET + 100_000_000.7
    tid = gen.calc("post", "/i/api/graphql/abc/SearchTimeline", now=now, rnd=77)
    assert "=" not in tid
    raw = base64.b64decode(tid + "=" * (-len(tid) % 4))
    assert raw[0] == 77
    plain = decode(tid)
    ts = 100_000_000
    assert plain[:48] == VK
    assert plain[48:52] == [(ts >> (8 * i)) & 0xFF for i in range(4)]
    digest = hashlib.sha256(f"POST!/i/api/graphql/abc/SearchTimeline!{ts}{KEYWORD}{ANIM}".encode()).digest()
    assert plain[52:68] == list(digest[:16])
    assert plain[68] == 3 and len(plain) == 69


def test_from_pair_decodes_verification():
    verification = base64.b64encode(bytes(VK)).decode()
    gen = TidGenerator.from_pair(verification, ANIM)
    assert gen.vk == VK and gen.layer == "pair-dict"


class Clock:
    def __init__(self):
        self.t = 1_000_000.0

    def __call__(self):
        return self.t


async def fetch_nothing(url: str):
    return 404, "text/html", ""


async def test_provider_falls_back_to_pairs_then_none():
    clock = Clock()
    pairs = [{"verification": base64.b64encode(bytes(VK)).decode(), "animationKey": ANIM}]
    served = {"pairs": pairs}

    async def pair_fetcher():
        return served["pairs"]

    async def broken_page():
        raise RuntimeError("page down")

    p = TidProvider(clock=clock, pair_fetcher=pair_fetcher)
    gen = await p.get(1, broken_page, fetch_nothing)
    assert gen is not None and gen.layer == "pair-dict"
    assert "generator" in (p.last_error or "")

    p2 = TidProvider(clock=clock, pair_fetcher=lambda: _empty())
    assert await p2.get(1, broken_page, fetch_nothing) is None
    assert p2.layer_for(1) == "none"


async def _empty():
    return []


async def test_provider_caches_generator_and_rebuilds_after_interval(monkeypatch):
    clock = Clock()
    built: list[int] = []

    async def fake_from_page(html, fetch):
        built.append(1)
        return TidGenerator(VK, ANIM, "generator")

    monkeypatch.setattr(TidGenerator, "from_page", staticmethod(fake_from_page))

    async def page():
        return "<html></html>"

    p = TidProvider(rebuild_sec=100, clock=clock, pair_fetcher=_empty)
    g1 = await p.get(5, page, fetch_nothing)
    g2 = await p.get(5, page, fetch_nothing)
    assert g1 is g2 and len(built) == 1 and p.layer_for(5) == "generator"
    clock.t += 101
    await p.get(5, page, fetch_nothing)
    assert len(built) == 2
    p.invalidate(5)
    assert p.layer_for(5) == "none"


@pytest.mark.parametrize("frames", [[10.0] * 7 + [100.0, 50.0, 200.0, 25.0]])
def test_anim_key_is_deterministic(frames):
    from xscout.xweb.tid import anim_key

    assert anim_key(frames, 0.5) == anim_key(frames, 0.5)
    assert anim_key(frames, 0.1) != anim_key(frames, 0.9)
