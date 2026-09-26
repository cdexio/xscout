import pytest

from xscout.store.operations import OperationRecord, OperationRepository
from xscout.xweb.registry import OperationRegistry

pytestmark = pytest.mark.db


def rec(qid: str, features=None) -> OperationRecord:
    return OperationRecord("SearchTimeline", qid, features or {"a": True}, ["a"], ["t"], "discovery")


async def test_save_active_tracks_history_and_changes(sessions):
    repo = OperationRepository(sessions)
    assert await repo.save_active([rec("q1")]) == []
    assert await repo.save_active([rec("q1")]) == []
    assert await repo.save_active([rec("q2")]) == ["SearchTimeline"]
    active = await repo.active()
    assert active["SearchTimeline"].query_id == "q2"
    history = await repo.history("SearchTimeline")
    assert [h[0] for h in history] == ["q1", "q2"] and [h[3] for h in history] == [False, True]


async def test_registry_load_prefers_db_over_fallback(sessions):
    repo = OperationRepository(sessions)
    await repo.save_active([rec("from-db", {"z": False})])
    reg = OperationRegistry(repo)
    await reg.load()
    spec = reg.get("SearchTimeline")
    assert spec.query_id == "from-db" and spec.features == {"z": False} and spec.source == "db:discovery"
    assert reg.get("UserByScreenName").source == "fallback"


async def test_heal_persists_features(sessions):
    repo = OperationRepository(sessions)
    await repo.save_active([rec("q9", {"a": True})])
    reg = OperationRegistry(repo)
    await reg.load()
    assert await reg.heal_missing_features("SearchTimeline", "cannot be null: b")
    assert (await repo.active())["SearchTimeline"].features == {"a": True, "b": True}
