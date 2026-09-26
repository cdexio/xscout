"""x-client-transaction-id provider (spec §6, plan 2.3).

Layers, in order: own generator built from the account's logged-in page -> precomputed key pairs
from fa0311/x-client-transaction-id-pair-dict -> none (only buckets that do not need a TID).

The key-derivation algorithm is vendored from twscrape (twscrape/xclid.py at c1500f2, MIT,
Copyright (c) 2023 vladkens), which takes it from XClientTransaction
(github.com/iSarabjitDhiman/XClientTransaction, MIT). See THIRD_PARTY_NOTICES.md.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
import math
import random
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from urllib.parse import urljoin

import bs4
from curl_cffi.requests import AsyncSession

from xscout.xweb.discovery import LOGGED_OUT_ENTRY_RE, script_urls

log = logging.getLogger("xscout.tid")

PAIR_DICT_URL = "https://raw.githubusercontent.com/fa0311/x-client-transaction-id-pair-dict/refs/heads/main/pair.json"
EPOCH_OFFSET = 1682924400
KEYWORD = "obfiowerehiring"
INDICES_RE = re.compile(r"(\(\w{1}\[(\d{1,2})\],\s*16\))+", flags=re.VERBOSE | re.MULTILINE)
INDICES_FILE_RE = re.compile(r"(?:\.{0,2}/)?[\w./-]*?\b(?:ondemand\.s|sign\.o)[\w.-]*\.js")

FetchText = Callable[[str], Awaitable[tuple[int, str, str]]]


class TidError(Exception):
    pass


# MARK: key derivation (vendored)


class _Cubic:
    def __init__(self, curves: list[float]):
        self.curves = curves

    def value(self, t: float) -> float:
        c = self.curves
        start_gradient = end_gradient = start = mid = 0.0
        end = 1.0
        if t <= 0.0:
            if c[0] > 0.0:
                start_gradient = c[1] / c[0]
            elif c[1] == 0.0 and c[2] > 0.0:
                start_gradient = c[3] / c[2]
            return start_gradient * t
        if t >= 1.0:
            if c[2] < 1.0:
                end_gradient = (c[3] - 1.0) / (c[2] - 1.0)
            elif c[2] == 1.0 and c[0] < 1.0:
                end_gradient = (c[1] - 1.0) / (c[0] - 1.0)
            return 1.0 + end_gradient * (t - 1.0)
        while start < end:
            mid = (start + end) / 2
            x_est = self._calc(c[0], c[2], mid)
            if abs(t - x_est) < 0.00001:
                return self._calc(c[1], c[3], mid)
            if x_est < t:
                start = mid
            else:
                end = mid
        return self._calc(c[1], c[3], mid)

    @staticmethod
    def _calc(a: float, b: float, m: float) -> float:
        return 3.0 * a * (1 - m) * (1 - m) * m + 3.0 * b * (1 - m) * m * m + m * m * m


def _interpolate(a: list[float], b: list[float], f: float) -> list[float]:
    return [x * (1 - f) + y * f for x, y in zip(a, b, strict=True)]


def _solve(value: float, lo: float, hi: float, rounding: bool) -> float:
    result = value * (hi - lo) / 255 + lo
    return math.floor(result) if rounding else round(result, 2)


def _float_to_hex(x: float) -> str:
    out: list[str] = []
    quotient = int(x)
    fraction = x - quotient
    while quotient > 0:
        quotient = int(x / 16)
        remainder = int(x - (float(quotient) * 16))
        out.insert(0, chr(remainder + 55) if remainder > 9 else str(remainder))
        x = float(quotient)
    if fraction == 0:
        return "".join(out)
    out.append(".")
    while fraction > 0:
        fraction *= 16
        integer = int(fraction)
        fraction -= float(integer)
        out.append(chr(integer + 55) if integer > 9 else str(integer))
    return "".join(out)


def anim_key(frames: list[float], target_time: float) -> str:
    from_color = [*frames[:3], 1]
    to_color = [*frames[3:6], 1]
    to_rotation = [_solve(frames[6], 60.0, 360.0, True)]
    curves = [_solve(x, -1.0 if i % 2 else 0.0, 1.0, False) for i, x in enumerate(frames[7:])]
    val = _Cubic(curves).value(target_time)
    color = [max(0, min(255, v)) for v in _interpolate(from_color, to_color, val)]
    rotation = _interpolate([0.0], to_rotation, val)
    rad = math.radians(rotation[0])
    matrix = [math.cos(rad), -math.sin(rad), math.sin(rad), math.cos(rad)]
    parts = [format(round(v), "x") for v in color[:-1]]
    for v in matrix:
        h = _float_to_hex(abs(round(v, 2)))
        parts.append(f"0{h}".lower() if h.startswith(".") else h if h else "0")
    parts.extend(["0", "0"])
    return re.sub(r"[.-]", "", "".join(parts))


def verification_bytes(soup: bs4.BeautifulSoup) -> list[int]:
    el = soup.find("meta", {"name": "twitter-site-verification", "content": True})
    content = str(el.get("content")) if el and isinstance(el, bs4.Tag) else None
    if not content:
        raise TidError("verification key not found in page")
    try:
        return list(base64.b64decode(content.encode(), validate=True))
    except ValueError as e:
        raise TidError("invalid verification key") from e


def anim_frames(soup: bs4.BeautifulSoup, vk: list[int]) -> list[list[float]]:
    paths = [
        str(x.get("d") or "").strip() for x in soup.select("svg[id^='loading-x-anim'] g:first-child path:nth-child(2)")
    ]
    if not paths:
        raise TidError("animation frames not found in page")
    rows = paths[vk[5] % len(paths)][9:].split("C")
    try:
        return [list(map(float, re.sub(r"[^\d]+", " ", r).split())) for r in rows]
    except (IndexError, ValueError) as e:
        raise TidError("invalid animation frames") from e


async def _indices(fetch: FetchText, html: str) -> tuple[list[int], str]:
    scripts = script_urls(html)
    x_web = [u for u in scripts if "/x-web/" in u]
    if x_web:
        if any(LOGGED_OUT_ENTRY_RE.search(u) for u in x_web):
            raise TidError("logged-out web app served")
        scripts = x_web
    direct = [u for u in scripts if INDICES_FILE_RE.search(u)]
    url = direct[0] if direct else await _find_indices_url(fetch, scripts)
    status, _, text = await fetch(url)
    if status >= 400:
        raise TidError(f"indices script HTTP {status}")
    items = [int(m.group(2)) for m in INDICES_RE.finditer(text)]
    if not items:
        raise TidError("signing indices not found")
    return items, url


async def _find_indices_url(fetch: FetchText, scripts: list[str]) -> str:
    sem = asyncio.Semaphore(8)

    async def one(url: str) -> tuple[str, str | None]:
        async with sem:
            try:
                status, _, text = await fetch(url)
                return url, text if status < 400 else None
            except Exception:
                return url, None

    tasks = [asyncio.create_task(one(u)) for u in scripts]
    try:
        for fut in asyncio.as_completed(tasks):
            url, body = await fut
            if body and (m := INDICES_FILE_RE.search(body)):
                return urljoin(url, m.group(0))
    finally:
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    raise TidError("signing script not found")


# MARK: generators


class TidGenerator:
    """Computes transaction ids from (verification key bytes, animation key)."""

    def __init__(self, vk: list[int], animation_key: str, layer: str):
        self.vk = vk
        self.animation_key = animation_key
        self.layer = layer

    @staticmethod
    async def from_page(html: str, fetch: FetchText) -> TidGenerator:
        soup = bs4.BeautifulSoup(html, "html.parser")
        idx, url = await _indices(fetch, html)
        vk = verification_bytes(soup)
        frames = anim_frames(soup, vk)
        frame_time = 1
        for i in idx[1:]:
            frame_time *= vk[i] % 16
        frame_time = math.floor(frame_time / 10 + 0.5) * 10
        key = anim_key(frames[vk[idx[0]] % 16], float(frame_time) / 4096)
        log.info("tid generator built", extra={"fields": {"indices_file": url.rsplit("/", 1)[-1]}})
        return TidGenerator(vk, key, "generator")

    @staticmethod
    def from_pair(verification: str, animation_key: str) -> TidGenerator:
        return TidGenerator(list(base64.b64decode(verification)), animation_key, "pair-dict")

    def calc(self, method: str, path: str, now: float | None = None, rnd: int | None = None) -> str:
        ts = math.floor((now if now is not None else time.time()) - EPOCH_OFFSET)
        ts_bytes = [(ts >> (i * 8)) & 0xFF for i in range(4)]
        digest = hashlib.sha256(f"{method.upper()}!{path}!{ts}{KEYWORD}{self.animation_key}".encode()).digest()
        raw = [*self.vk, *ts_bytes, *digest[:16], 3]
        num = random.randint(0, 255) if rnd is None else rnd
        return base64.b64encode(bytearray([num, *[x ^ num for x in raw]])).decode().strip("=")


# MARK: provider


async def fetch_pairs() -> list[dict[str, str]]:
    async with AsyncSession(impersonate="chrome", timeout=20) as s:
        rep = await s.get(PAIR_DICT_URL)
    data = rep.json() if rep.status_code == 200 else []
    return [p for p in data if isinstance(p, dict) and p.get("verification") and p.get("animationKey")]


@dataclass
class _Cached:
    gen: TidGenerator
    built_at: float


class TidProvider:
    """Per-account generator cache with the pair-dict fallback."""

    def __init__(
        self,
        rebuild_sec: float = 10800,
        pair_ttl_sec: float = 3600,
        clock: Callable[[], float] = time.time,
        pair_fetcher: Callable[[], Awaitable[list[dict[str, str]]]] | None = None,
    ):
        self.rebuild_sec = rebuild_sec
        self.pair_ttl_sec = pair_ttl_sec
        self._clock = clock
        self._pair_fetcher = pair_fetcher or fetch_pairs
        self._gens: dict[int, _Cached] = {}
        self._failed_at: dict[int, float] = {}
        self._pairs: list[dict[str, str]] = []
        self._pairs_at = 0.0
        self._locks: dict[int, asyncio.Lock] = {}
        self.last_error: str | None = None

    def layer_for(self, account_id: int) -> str:
        if account_id in self._gens:
            return "generator"
        return "pair-dict" if self._pairs else "none"

    def invalidate(self, account_id: int) -> None:
        self._gens.pop(account_id, None)

    async def get(self, account_id: int, page: Callable[[], Awaitable[str]], fetch: FetchText) -> TidGenerator | None:
        now = self._clock()
        cached = self._gens.get(account_id)
        if cached and now - cached.built_at < self.rebuild_sec:
            return cached.gen
        lock = self._locks.setdefault(account_id, asyncio.Lock())
        async with lock:
            cached = self._gens.get(account_id)
            if cached and now - cached.built_at < self.rebuild_sec:
                return cached.gen
            # After a failed build, wait a few minutes before retrying the generator.
            if now - self._failed_at.get(account_id, 0) > 300:
                try:
                    gen = await TidGenerator.from_page(await page(), fetch)
                    self._gens[account_id] = _Cached(gen, now)
                    self._failed_at.pop(account_id, None)
                    return gen
                except Exception as e:
                    self._failed_at[account_id] = now
                    self.last_error = f"generator: {e}"
                    log.warning("tid generator failed", extra={"fields": {"error": str(e)}})
        return await self._pair_gen()

    async def _pair_gen(self) -> TidGenerator | None:
        now = self._clock()
        if not self._pairs or now - self._pairs_at > self.pair_ttl_sec:
            try:
                pairs = await self._pair_fetcher()
                if pairs:
                    self._pairs, self._pairs_at = pairs, now
            except Exception as e:
                self.last_error = f"pair-dict: {e}"
        if not self._pairs:
            return None
        p = random.choice(self._pairs)
        return TidGenerator.from_pair(p["verification"], p["animationKey"])
