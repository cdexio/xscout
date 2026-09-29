import json

import pytest

from xscout.crypto import SecretBox, SecretBoxError
from xscout.store.accounts import AccountError, AccountRepository
from xscout.store.models import AccountStatus

pytestmark = pytest.mark.db
PROFILES = ["chrome150"]
TOKEN, CT0 = "a" * 40, "b" * 64


async def test_export_import_keeps_ciphertext_and_checks_key(sessions):
    key = SecretBox.generate_key()
    src = AccountRepository(sessions, SecretBox(key))
    await src.add("alice", TOKEN, CT0, PROFILES, proxy="http://u:p@1.2.3.4:80")
    await src.add("bob", TOKEN, CT0, PROFILES)
    data = await src.export_rows()
    text = json.dumps(data)
    assert TOKEN not in text and CT0 not in text and "1.2.3.4" not in text
    await src.remove("alice")
    await src.remove("bob")

    with pytest.raises(SecretBoxError):  # another machine's key cannot read the export
        await AccountRepository(sessions, SecretBox(SecretBox.generate_key())).import_rows(data)

    result = await src.import_rows(data, status=AccountStatus.DISABLED)
    assert result == {"added": ["alice", "bob"], "replaced": [], "skipped": []}
    assert {v.status for v in await src.list()} == {"disabled"}
    creds = await src.credentials("alice")
    assert (creds.auth_token, creds.ct0, creds.proxy) == (TOKEN, CT0, "http://u:p@1.2.3.4:80")
    assert (await src.import_rows(data))["skipped"] == ["alice", "bob"]
    assert (await src.import_rows(data, replace=True))["replaced"] == ["alice", "bob"]


async def test_import_rejects_foreign_format(sessions):
    repo = AccountRepository(sessions, SecretBox(SecretBox.generate_key()))
    with pytest.raises(AccountError):
        await repo.import_rows({"format": "other"})
