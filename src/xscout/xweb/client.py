"""One account, one call: TID, request, send, inspect, parse (plan 2.5; wrapped by the gateway in phase 3).

Self-healing done here because it is wire-level: error 336 -> add features and retry once;
404 on a bucket that sends a TID -> rebuild the TID and retry once.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from xscout.transport.session import AccountTransport, RawResponse
from xscout.xweb.constants import TID_PAGES, Bucket
from xscout.xweb.errors import Inspected, inspect
from xscout.xweb.ops import OpCall
from xscout.xweb.pages import fetch_page, fetch_text
from xscout.xweb.registry import OperationRegistry
from xscout.xweb.requests import build_request
from xscout.xweb.tid import TidProvider

log = logging.getLogger("xscout.client")

NULL_SPIKE_MIN_ITEMS = 5  # judge null rates only on pages with at least this many items
NULL_SPIKE_SAMPLE_EVERY_SEC = 600

SampleSink = Callable[..., Awaitable[Any]]


@dataclass
class CallOutcome:
    operation: str
    bucket: Bucket
    response: RawResponse
    inspected: Inspected
    parsed: Any | None
    tid_layer: str
    attempts: int
    parse_error: str | None = None

    @property
    def ok(self) -> bool:
        return self.response.status == 200 and not self.inspected.errors and self.parsed is not None


class AccountClient:
    def __init__(
        self,
        transport: AccountTransport,
        registry: OperationRegistry,
        tids: TidProvider,
        samples: SampleSink | None = None,
    ):
        self.transport = transport
        self.registry = registry
        self.tids = tids
        self.samples = samples
        self._last_spike_sample: dict[str, float] = {}

    async def _tid(self, bucket: Bucket):
        if not bucket.sends_tid:
            return None, "none"
        gen = await self.tids.get(
            self.transport.account_id,
            page=[lambda url=url: fetch_page(self.transport, url) for url in TID_PAGES],
            fetch=lambda url: fetch_text(self.transport, url),
        )
        return gen, gen.layer if gen else "none"

    async def call(self, call: OpCall, bucket: Bucket) -> CallOutcome:
        attempts = 0
        healed = retried_tid = False
        while True:
            attempts += 1
            spec = self.registry.get(call.operation)
            gen, layer = await self._tid(bucket)
            rep = await self.transport.send(build_request(spec, call.variables, bucket, gen))
            ins = inspect(rep)
            if 336 in ins.codes and not healed:
                healed = True
                messages = " ".join(e.message for e in ins.errors if e.code == 336)
                if await self.registry.heal_missing_features(call.operation, messages):
                    continue
            if rep.status == 404 and bucket.sends_tid and not retried_tid:
                retried_tid = True
                self.tids.invalidate(self.transport.account_id)
                continue
            return await self._finish(call, bucket, rep, ins, layer, attempts)

    async def _finish(
        self, call: OpCall, bucket: Bucket, rep: RawResponse, ins: Inspected, layer: str, attempts: int
    ) -> CallOutcome:
        parsed = None
        parse_error = None
        if rep.status == 200 and ins.has_data:
            try:
                parsed = call.parser(ins.body)
            except Exception as e:  # parsers are tolerant; this is a real schema break
                parse_error = f"{type(e).__name__}: {e}"
        stats = getattr(parsed, "stats", None)
        reason = None
        if parse_error or (stats is not None and stats.failed):
            reason = parse_error or f"{stats.failed} entries failed: {stats.errors[:3]}"
        elif stats is not None and stats.parsed >= NULL_SPIKE_MIN_ITEMS:
            spiking = {f: round(stats.null_rate(f), 2) for f in stats.key_field_nulls if stats.null_rate(f) >= 0.5}
            last = self._last_spike_sample.get(call.operation, float("-inf"))
            if spiking and time.monotonic() - last > NULL_SPIKE_SAMPLE_EVERY_SEC:
                self._last_spike_sample[call.operation] = time.monotonic()
                reason = f"null-rate spike: {spiking}"
        if reason:
            log.warning("parse problem", extra={"fields": {"operation": call.operation, "reason": reason}})
            if self.samples is not None:
                await self.samples(
                    operation=call.operation,
                    reason=reason,
                    body=rep.text,
                    status=rep.status,
                    bucket=bucket.name,
                    account_id=self.transport.account_id,
                )
        return CallOutcome(call.operation, bucket, rep, ins, parsed, layer, attempts, parse_error)
