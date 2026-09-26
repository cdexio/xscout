"""One account, one call: TID, request, send, inspect, parse (plan 2.5; wrapped by the gateway in phase 3).

Self-healing done here because it is wire-level: error 336 -> add features and retry once;
404 on a bucket that sends a TID -> rebuild the TID and retry once.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from xscout.transport.session import AccountTransport, RawResponse
from xscout.xweb.constants import HOME_URL, Bucket
from xscout.xweb.errors import Inspected, inspect
from xscout.xweb.ops import OpCall
from xscout.xweb.pages import fetch_page, fetch_text
from xscout.xweb.registry import OperationRegistry
from xscout.xweb.requests import build_request
from xscout.xweb.tid import TidProvider

log = logging.getLogger("xscout.client")

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

    async def _tid(self, bucket: Bucket):
        if not bucket.sends_tid:
            return None, "none"
        gen = await self.tids.get(
            self.transport.account_id,
            page=lambda: fetch_page(self.transport, HOME_URL),
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
        if parse_error or (stats is not None and stats.failed):
            reason = parse_error or f"{stats.failed} entries failed: {stats.errors[:3]}"
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
