import pytest
from sqlalchemy import select, text

from tests.conftest import alembic, guard_test_url
from xscout.crypto import SecretBox
from xscout.store.accounts import AccountError, AccountRepository
from xscout.store.models import Account, AccountStatus

pytestmark = pytest.mark.db

PROFILES = ["chrome150", "chrome146", "chrome145"]
TOKEN = "a" * 40
CT0 = "b" * 160


@pytest.fixture
def repo(sessions):
    return AccountRepository(sessions, SecretBox(SecretBox.generate_key()))


def test_guard_rejects_other_databases():
    with pytest.raises(RuntimeError):
        guard_test_url("postgresql+asyncpg://u:p@localhost:5432/xscout")


async def test_add_stores_ciphertext_and_lists_without_secrets(repo, sessions):
    view = await repo.add("@Alice", TOKEN, CT0, PROFILES)
    assert view.username == "alice" and view.status == AccountStatus.ACTIVE and not view.has_proxy
    async with sessions() as s:
        row = await s.scalar(select(Account).where(Account.username == "alice"))
        assert TOKEN not in row.auth_token_enc and CT0 not in row.ct0_enc
    listed = await repo.list()
    assert [v.username for v in listed] == ["alice"]
    assert "auth_token" not in repr(listed[0]) and TOKEN not in repr(listed[0])


async def test_credentials_decrypt(repo):
    await repo.add("bob", TOKEN, CT0, PROFILES, proxy="http://u:p@1.2.3.4:8080")
    creds = await repo.credentials("BOB")
    assert (creds.auth_token, creds.ct0, creds.proxy) == (TOKEN, CT0, "http://u:p@1.2.3.4:8080")


async def test_duplicate_requires_replace(repo):
    await repo.add("carol", TOKEN, CT0, PROFILES)
    with pytest.raises(AccountError):
        await repo.add("carol", TOKEN, CT0, PROFILES)
    await repo.set_status("carol", AccountStatus.EXPIRED, "code 32")
    view = await repo.add("carol", "c" * 40, CT0, PROFILES, replace=True)
    assert view.status == AccountStatus.ACTIVE
    assert (await repo.credentials("carol")).auth_token == "c" * 40


async def test_status_overflow_and_remove(repo):
    await repo.add("dave", TOKEN, CT0, PROFILES)
    assert (await repo.set_status("dave", AccountStatus.DISABLED, "manual")).status == "disabled"
    assert (await repo.set_overflow("dave", False)).allow_overflow is False
    assert await repo.remove("dave") is True
    assert await repo.remove("dave") is False
    assert await repo.get("dave") is None


async def test_migration_round_trip(migrated, engine):
    alembic(migrated, "downgrade", "base")
    async with engine.connect() as conn:
        assert await conn.scalar(text("select to_regclass('public.accounts')")) is None
    alembic(migrated, "upgrade", "head")
    async with engine.connect() as conn:
        assert await conn.scalar(text("select to_regclass('public.accounts')")) is not None
