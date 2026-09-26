"""x-client-transaction-id generator (phase 0 probe copy).

Vendored and adapted from twscrape (https://github.com/vladkens/twscrape,
twscrape/xclid.py at c1500f2, MIT License, Copyright (c) 2023 vladkens), which
itself takes the algorithm from XClientTransaction
(https://github.com/iSarabjitDhiman/XClientTransaction, MIT License).

Adaptation: uses a curl_cffi AsyncSession directly instead of twscrape's
HttpClient wrapper, and accepts an already fetched page.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import math
import random
import re
import time
from urllib.parse import urljoin

import bs4
from curl_cffi.requests import AsyncSession


class XClIdError(Exception): ...


class XClIdParseError(XClIdError): ...


class XClIdAccountError(XClIdError): ...


async def get_page_text(session: AsyncSession, url: str) -> str:
    rep = await session.get(url)
    if rep.status_code >= 400:
        raise XClIdError(f"HTTP {rep.status_code} for {url}")
    if ">document.location =" not in rep.text:
        return rep.text

    url = rep.text.split('document.location = "')[1].split('"')[0]
    rep = await session.get(url)
    if rep.status_code >= 400:
        raise XClIdError(f"HTTP {rep.status_code} for {url}")
    if 'action="https://x.com/x/migrate" method="post"' not in rep.text:
        return rep.text

    data = {}
    for chunk in rep.text.split("<input")[1:]:
        name = chunk.split('name="')[1].split('"')[0]
        value = chunk.split('value="')[1].split('"')[0]
        data[name] = value
    rep = await session.post("https://x.com/x/migrate", json=data)
    if rep.status_code >= 400:
        raise XClIdError(f"HTTP {rep.status_code} for migrate")
    return rep.text


def script_url(key: str, value: str) -> str:
    return f"https://abs.twimg.com/responsive-web/client-web/{key}.{value}.js"


ASSET_URL_RE = re.compile(r"https://[\w.-]+/x-web/[\w./-]+\.js")
RESPONSIVE_WEB_URL_RE = re.compile(r"https://[\w.-]+/responsive-web/client-web/[\w./-]+\.js")
LEGACY_MAIN_RE = re.compile(r"/client-web/main\.([^.\"']+)\.js")
LOGGED_OUT_ENTRY_RE = re.compile(r"(?:^|/)entry-client-logged-out(?:[-.][^/?#]+)?\.js(?:[?#].*)?$")
CHALLENGE_SCRIPT_RE = re.compile(r"/cdn-cgi/challenge-platform/(?:scripts|h)/")


def get_scripts_list(text: str) -> list[str]:
    urls = ASSET_URL_RE.findall(text) + RESPONSIVE_WEB_URL_RE.findall(text)
    if main_match := LEGACY_MAIN_RE.search(text):
        urls.append(script_url("main", main_match.group(1)))

    if not urls and CHALLENGE_SCRIPT_RE.search(text):
        raise XClIdParseError("Cloudflare challenge page served instead of X web app")

    hash_map = {m.group(1): m.group(2) for m in re.finditer(r'(\d+):"([0-9a-f]{7}|[0-9a-f]{16})"', text)}
    name_map: dict[str, str] = {}
    for m in re.finditer(r'(\d+):"([^"]+)"', text):
        if not re.fullmatch(r"[0-9a-f]{7}|[0-9a-f]{16}", m.group(2)):
            name_map[m.group(1)] = m.group(2)

    urls.extend(script_url(name_map.get(cid, cid), hv + "a") for cid, hv in hash_map.items())
    if not urls:
        raise XClIdParseError("X web scripts not found")
    return list(dict.fromkeys(urls))


INDICES_REGEX = re.compile(r"(\(\w{1}\[(\d{1,2})\],\s*16\))+", flags=(re.VERBOSE | re.MULTILINE))
INDICES_FILE_RE = re.compile(r"(?:\.{0,2}/)?[\w./-]*?\b(?:ondemand\.s|sign\.o)[\w.-]*\.js")


class Cubic:
    def __init__(self, curves: list[float]):
        self.curves = curves

    def get_value(self, t: float) -> float:
        start_gradient = end_gradient = start = mid = 0.0
        end = 1.0
        if t <= 0.0:
            if self.curves[0] > 0.0:
                start_gradient = self.curves[1] / self.curves[0]
            elif self.curves[1] == 0.0 and self.curves[2] > 0.0:
                start_gradient = self.curves[3] / self.curves[2]
            return start_gradient * t
        if t >= 1.0:
            if self.curves[2] < 1.0:
                end_gradient = (self.curves[3] - 1.0) / (self.curves[2] - 1.0)
            elif self.curves[2] == 1.0 and self.curves[0] < 1.0:
                end_gradient = (self.curves[1] - 1.0) / (self.curves[0] - 1.0)
            return 1.0 + end_gradient * (t - 1.0)
        while start < end:
            mid = (start + end) / 2
            x_est = self.calculate(self.curves[0], self.curves[2], mid)
            if abs(t - x_est) < 0.00001:
                return self.calculate(self.curves[1], self.curves[3], mid)
            if x_est < t:
                start = mid
            else:
                end = mid
        return self.calculate(self.curves[1], self.curves[3], mid)

    @staticmethod
    def calculate(a: float, b: float, m: float) -> float:
        return 3.0 * a * (1 - m) * (1 - m) * m + 3.0 * b * (1 - m) * m * m + m * m * m


def interpolate(from_list: list[float], to_list: list[float], f: float) -> list[float]:
    return [a * (1 - f) + b * f for a, b in zip(from_list, to_list)]


def get_rotation_matrix(rotation: float) -> list[float]:
    rad = math.radians(rotation)
    return [math.cos(rad), -math.sin(rad), math.sin(rad), math.cos(rad)]


def solve(value: float, min_val: float, max_val: float, rounding: bool) -> float:
    result = value * (max_val - min_val) / 255 + min_val
    return math.floor(result) if rounding else round(result, 2)


def float_to_hex(x: float) -> str:
    result: list[str] = []
    quotient = int(x)
    fraction = x - quotient
    while quotient > 0:
        quotient = int(x / 16)
        remainder = int(x - (float(quotient) * 16))
        result.insert(0, chr(remainder + 55) if remainder > 9 else str(remainder))
        x = float(quotient)
    if fraction == 0:
        return "".join(result)
    result.append(".")
    while fraction > 0:
        fraction *= 16
        integer = int(fraction)
        fraction -= float(integer)
        result.append(chr(integer + 55) if integer > 9 else str(integer))
    return "".join(result)


def calc_anim_key(frames: list[float], target_time: float) -> str:
    from_color = [*frames[:3], 1]
    to_color = [*frames[3:6], 1]
    from_rotation = [0.0]
    to_rotation = [solve(frames[6], 60.0, 360.0, True)]
    frames = frames[7:]
    curves = [solve(x, -1.0 if i % 2 else 0.0, 1.0, False) for i, x in enumerate(frames)]
    val = Cubic(curves).get_value(target_time)
    color = [max(0, min(255, v)) for v in interpolate(from_color, to_color, val)]
    rotation = interpolate(from_rotation, to_rotation, val)
    matrix = get_rotation_matrix(rotation[0])
    parts = [format(round(v), "x") for v in color[:-1]]
    for value in matrix:
        rounded = abs(round(value, 2))
        hex_value = float_to_hex(rounded)
        parts.append(f"0{hex_value}".lower() if hex_value.startswith(".") else hex_value if hex_value else "0")
    parts.extend(["0", "0"])
    return re.sub(r"[.-]", "", "".join(parts))


def parse_vk_bytes(soup: bs4.BeautifulSoup) -> list[int]:
    el = soup.find("meta", {"name": "twitter-site-verification", "content": True})
    content = str(el.get("content")) if el and isinstance(el, bs4.Tag) else None
    if not content:
        raise XClIdParseError("X verification key not found")
    try:
        return list(base64.b64decode(bytes(content, "utf-8"), validate=True))
    except ValueError as e:
        raise XClIdParseError("Invalid X verification key") from e


async def _find_indices_url(session: AsyncSession, scripts: list[str]) -> str:
    sem = asyncio.Semaphore(8)

    async def fetch(url: str) -> tuple[str, str | None]:
        async with sem:
            try:
                rep = await session.get(url)
                return url, rep.text if rep.status_code < 400 else None
            except Exception:
                return url, None

    tasks = [asyncio.create_task(fetch(u)) for u in scripts]
    loaded = failed = 0
    try:
        for fut in asyncio.as_completed(tasks):
            url, body = await fut
            if body is None:
                failed += 1
                continue
            loaded += 1
            m = INDICES_FILE_RE.search(body)
            if m:
                return urljoin(url, m.group(0))
    finally:
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    raise XClIdParseError(f"Signing script not found (assets: {loaded} loaded, {failed} failed)")


async def parse_anim_idx(session: AsyncSession, page_text: str) -> tuple[list[int], str]:
    scripts = get_scripts_list(page_text)
    x_web = [u for u in scripts if "/x-web/" in u]
    if x_web:
        if any(LOGGED_OUT_ENTRY_RE.search(u) for u in x_web):
            raise XClIdAccountError("Logged-out X web app")
        scripts = x_web
    direct = [u for u in scripts if INDICES_FILE_RE.search(u)]
    url = direct[0] if direct else await _find_indices_url(session, scripts)
    text = await get_page_text(session, url)
    items = [int(m.group(2)) for m in INDICES_REGEX.finditer(text)]
    if not items:
        raise XClIdParseError("Signing indices not found")
    return items, url


def parse_anim_arr(soup: bs4.BeautifulSoup, vk_bytes: list[int]) -> list[list[float]]:
    els = [str(x.get("d") or "").strip() for x in soup.select("svg[id^='loading-x-anim'] g:first-child path:nth-child(2)")]
    if not els:
        raise XClIdParseError("Animation data not found")
    idx = vk_bytes[5] % len(els)
    rows = els[idx][9:].split("C")
    try:
        return [list(map(float, re.sub(r"[^\d]+", " ", x).split())) for x in rows]
    except (IndexError, ValueError) as e:
        raise XClIdParseError("Invalid animation data") from e


class XClIdGen:
    """Builds transaction ids from a verification key and an animation key."""

    def __init__(self, vk_bytes: list[int], anim_key: str, source: str):
        self.vk_bytes = vk_bytes
        self.anim_key = anim_key
        self.source = source

    @staticmethod
    async def from_page(session: AsyncSession, page_text: str) -> "XClIdGen":
        soup = bs4.BeautifulSoup(page_text, "html.parser")
        anim_idx, indices_url = await parse_anim_idx(session, page_text)
        vk_bytes = parse_vk_bytes(soup)
        anim_arr = parse_anim_arr(soup, vk_bytes)
        frame_time = 1
        for i in anim_idx[1:]:
            frame_time *= vk_bytes[i] % 16
        frame_time = math.floor(frame_time / 10 + 0.5) * 10
        frame_row = anim_arr[vk_bytes[anim_idx[0]] % 16]
        anim_key = calc_anim_key(frame_row, float(frame_time) / 4096)
        return XClIdGen(vk_bytes, anim_key, source=f"generator ({indices_url.rsplit('/', 1)[-1]})")

    @staticmethod
    def from_pair(verification: str, animation_key: str) -> "XClIdGen":
        return XClIdGen(list(base64.b64decode(verification)), animation_key, source="pair-dict")

    def calc(self, method: str, path: str) -> str:
        ts = math.floor((time.time() * 1000 - 1682924400 * 1000) / 1000)
        ts_bytes = [(ts >> (i * 8)) & 0xFF for i in range(4)]
        payload = f"{method.upper()}!{path}!{ts}obfiowerehiring{self.anim_key}"
        digest = list(hashlib.sha256(payload.encode()).digest())
        raw = [*self.vk_bytes, *ts_bytes, *digest[:16], 3]
        num = random.randint(0, 255)
        return base64.b64encode(bytearray([num, *[x ^ num for x in raw]])).decode().strip("=")
