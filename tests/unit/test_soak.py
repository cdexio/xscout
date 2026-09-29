import asyncio
import os
import signal
from pathlib import Path

from xscout.cli.soak import Soak


async def test_sigint_stops_loop_and_still_cleans_up(tmp_path: Path, monkeypatch):
    soak = Soak("http://127.0.0.1:1", rpm=600, watch_share=0.5, out=tmp_path / "r.json")
    calls: list[str] = []

    async def setup(s):
        calls.append("setup")

    async def one(s):
        calls.append("request")

    async def snapshot(s):
        calls.append("snapshot")

    async def cleanup(s):
        calls.append("cleanup")

    async def write(final):
        calls.append(f"write:{final}")

    monkeypatch.setattr(soak, "setup_watchlist", setup)
    monkeypatch.setattr(soak, "one_request", one)
    monkeypatch.setattr(soak, "snapshot", snapshot)
    monkeypatch.setattr(soak, "cleanup", cleanup)
    monkeypatch.setattr(soak, "write", write)

    asyncio.get_running_loop().call_later(0.3, os.kill, os.getpid(), signal.SIGINT)
    await asyncio.wait_for(soak.run(minutes=5), timeout=10)

    assert calls[0] == "setup" and "request" in calls
    assert calls[-3:] == ["cleanup", "snapshot", "write:True"]
