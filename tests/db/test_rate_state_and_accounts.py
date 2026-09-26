from datetime import UTC, datetime

import pytest

from xscout.crypto import SecretBox
from xscout.pool.pool import BucketState
from xscout.store.accounts import AccountRepository
from xscout.store.models import AccountStatus
from xscout.store.rate_state import RateStateRepository
from xscout.store.request_log import RequestLogRepository, RequestLogRow

pytestmark = pytest.mark.db
PROFILES = ["chrome150"]


@pytest.fixture
def accounts(sessions):
    return AccountRepository(sessions, SecretBox(SecretBox.generate_key()))


async def test_rate_state_round_trip_and_upsert(sessions, accounts):
    acc = await accounts.add("alice", "a" * 40, "b" * 64, PROFILES)
    repo = RateStateRepository(sessions)
    b = BucketState(
        limit=187, remaining=150, reset_at=1_800_000_600.0, cooling_until=1_800_000_300.0, last_used_at=1_800_000_000.0
    )
    await repo.upsert([(acc.id, "SearchTimeline", "POST/main", b)])
    b.remaining = 149
    await repo.upsert([(acc.id, "SearchTimeline", "POST/main", b)])
    loaded = await repo.load()
    got = loaded[acc.id][("SearchTimeline", "POST/main")]
    assert (got.limit, got.remaining, got.reset_at, got.cooling_until) == (187, 149, 1_800_000_600.0, 1_800_000_300.0)


async def test_account_runtime_updates(sessions, accounts):
    acc = await accounts.add("bob", "a" * 40, "b" * 64, PROFILES)
    assert [c.username for c in await accounts.active_credentials()] == ["bob"]
    await accounts.update_ct0(acc.id, "c" * 64)
    assert (await accounts.credentials("bob")).ct0 == "c" * 64
    now = datetime.now(UTC)
    await accounts.record_use(acc.id, now, error="rate_abuse", cooling_until=now)
    view = await accounts.get("bob")
    assert view.last_error == "rate_abuse" and view.cooling_until is not None
    await accounts.set_status_by_id(acc.id, AccountStatus.LOCKED, "326")
    assert (await accounts.get("bob")).status == "locked"
    assert await accounts.active_credentials() == []


async def test_request_log_insert(sessions, accounts):
    acc = await accounts.add("carol", "a" * 40, "b" * 64, PROFILES)
    repo = RequestLogRepository(sessions)
    row = RequestLogRow(datetime.now(UTC), "zetryn", "P0", "SearchTimeline", "POST/main", acc.id, 200, None, 900, "ok")
    await repo.add_many([row, row])
    from sqlalchemy import func, select

    from xscout.store.models import RequestLog

    async with sessions() as s:
        assert await s.scalar(select(func.count()).select_from(RequestLog)) == 2
