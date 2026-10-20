import sqlite3
import time

import pytest
from cryptography.fernet import Fernet

from relaypost.domain import Rejected, Uncertain, formats, validate_content
from relaypost.store import Store
from tests.conftest import OTHER


@pytest.mark.parametrize(
    "platform,kind,media,expected",
    [
        ("telegram", "channel", "text", ["post"]),
        ("telegram", "chat", "video", ["message"]),
        ("telegram-business", "business", "photo", ["story"]),
        ("telegram-business", "business", "text", []),
        ("telegram-business", "channel", "video", []),
        ("max", "channel", "text", ["post"]),
        ("max", "chat", "text", ["message"]),
        ("max", "channel", "photo", ["post"]),
        ("vk", "wall", "text", ["post"]),
        ("vk", "wall", "photo", ["post"]),
        ("unknown", "wall", "text", []),
    ],
)
def test_capabilities(platform, kind, media, expected):
    assert formats(platform, kind, media) == expected


def test_limits():
    validate_content({"media": "text", "text": "a"}, "telegram", "post")
    validate_content({"media": "photo", "text": "a"}, "telegram-business", "story")
    with pytest.raises(Rejected, match="длинный"):
        validate_content({"media": "text", "text": "😀" * 2049}, "telegram", "post")
    with pytest.raises(Rejected, match="длинный"):
        validate_content({"media": "photo", "text": "a" * 1001}, "telegram", "post")
    with pytest.raises(Rejected, match="требует"):
        validate_content({"media": "text", "text": "a"}, "telegram-business", "story")


def test_encryption_owner_constraints_migration(tmp_path):
    path = str(tmp_path / "db")
    key = Fernet.generate_key().decode()
    store = Store(path, key)
    account = store.save_account(1, "telegram", {"id": 1, "label": "One"}, OTHER)
    assert store.save_account(1, "telegram", {"id": 1, "label": "Updated"}, OTHER) == account
    assert store.accounts(1)[0]["label"] == "Updated"
    assert OTHER.encode() not in store.db.execute("SELECT secret FROM accounts").fetchone()[0]
    with pytest.raises(Rejected):
        store.account(2, account)
    store.add_target(1, account, {"id": "1", "kind": "chat", "label": "Chat"})
    store.add_target(1, account, {"id": "1", "kind": "chat", "label": "Chat"})
    assert len(store.targets(1)) == 1
    with pytest.raises(sqlite3.IntegrityError), store.db:
        store.db.execute(
            "INSERT INTO targets(account_id,external_id,kind,label) VALUES(999,'a','chat','a')"
        )
    draft = store.draft(1, {"media": "text", "text": "News"})
    assert store.claim_delivery(draft, 1, "message")
    store.close()
    store = Store(path, key)
    assert store.status(1)[0]["state"] == "unknown"
    assert not store.claim_delivery(draft, 1, "message")
    assert not store.get_draft(1, draft)["text"] == ""
    with pytest.raises(Rejected):
        store.get_draft(2, draft)
    with store.db:
        store.db.execute("UPDATE drafts SET created=?", (time.time() - 90000,))
    with pytest.raises(Rejected):
        store.get_draft(1, draft)
    store.draft(1, {"media": "text", "text": "new"})
    assert not store.status(1)
    assert store.db.execute("PRAGMA foreign_key_check").fetchall() == []
    assert store.db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    store.disconnect(1, account)
    assert not store.targets(1)
    with store.db:
        store.db.execute("PRAGMA user_version=99")
    store.close()
    with pytest.raises(Rejected, match="версия"):
        Store(path, key)


async def test_service_wrong_owner_and_mode(env):
    _, service, _ = env
    await service.connect(1, "telegram", OTHER)
    await service.add(1, 1, "-100")
    draft = service.store.draft(1, {"media": "text", "text": "a"})
    with pytest.raises(Rejected):
        await service.add(2, 1, "-100")
    with pytest.raises(Rejected, match="недоступно"):
        await service.publish(1, draft, 1, "story")


@pytest.mark.parametrize(
    "error,state", [(Rejected("Denied"), "failed"), (Uncertain("Unknown"), "unknown")]
)
async def test_service_records_delivery_errors(env, monkeypatch, error, state):
    _, service, _ = env
    await service.connect(1, "telegram", OTHER)
    await service.add(1, 1, "-100")
    draft = service.store.draft(1, {"media": "text", "text": "a"})

    async def fail(*args):
        raise error

    monkeypatch.setattr(service.gateway, "publish", fail)
    with pytest.raises(type(error)):
        await service.publish(1, draft, 1, "post")
    assert service.store.status(1)[0]["state"] == state


def test_vk_story_caption_and_capabilities():
    assert formats("vk", "story", "text") == []
    assert formats("vk", "story", "video") == ["story"]
    with pytest.raises(Rejected, match="подпись"):
        validate_content({"media": "photo", "text": "caption"}, "vk", "story")
