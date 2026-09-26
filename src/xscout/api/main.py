"""Production wiring: Runtime + XService behind the FastAPI app."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from xscout.api.app import Backend, create_app
from xscout.api.service import XService
from xscout.config import get_settings
from xscout.gateway.runtime import Runtime
from xscout.store.tweets import TweetRepository


@asynccontextmanager
async def open_runtime_backend() -> AsyncIterator[Backend]:
    runtime = Runtime(get_settings())
    await runtime.start()
    tweets = TweetRepository(runtime.sessions)
    service = XService(runtime.gateway, runtime.t, archive=tweets.upsert)
    try:
        yield Backend(service=service, status=runtime)
    finally:
        await service.drain()
        await runtime.stop()


def build_app() -> FastAPI:
    return create_app(open_runtime_backend)
