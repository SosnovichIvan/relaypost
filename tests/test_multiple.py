import json
import sqlite3
from unittest.mock import AsyncMock

import pytest
from cryptography.fernet import Fernet

from relaypost.domain import Rejected, Uncertain
from relaypost.store import SCHEMA, Store
from tests.conftest import OTHER, callback, message, sent_text


def setup(service):
    store = service.store
    account = store.save_account(1, "telegram", {"id": 200, "label": "Bot"}, OTHER)
    alias = store.save_account(1, "telegram", {"id": 201, "label": "Other"}, "201:synthetic")
    for acc, peer in [(account, "-100"), (account, "-200"), (alias, "-100")]:
        store.add_target(1, acc, {"id": peer, "label": peer, "kind": "channel"})
    draft = store.draft(1, {"media": "text", "text": "News", "file_id": ""})
    return draft, service.options(1, store.get_draft(1, draft))


def button(data, index):
    result = callback("ignored", update_id=index)
    result["callback_query"]["data"] = data
    return result


def test_v1_upgrade_backfills_peer_guard(tmp_path):
    path = tmp_path / "v1.db"
    key = Fernet.generate_key().decode()
    with sqlite3.connect(path) as db:
        db.executescript(SCHEMA)
        db.execute(
            "INSERT INTO accounts VALUES(1,1,?,?,?,?)",
            ("telegram", "200", "Bot", Fernet(key.encode()).encrypt(OTHER.encode())),
        )
        db.execute("INSERT INTO targets VALUES(1,1,'-100','channel','News')")
        db.execute("INSERT INTO drafts VALUES('draft',1,'{}',0)")
        db.execute("INSERT INTO deliveries VALUES(1,'draft',1,'post','sent','42')")
    store = Store(str(path), key)
    assert store.db.execute("PRAGMA user_version").fetchone()[0] == 1
    assert (
        store.db.execute("SELECT peer FROM peer_deliveries").fetchone()[0] == "telegram:-100:post"
    )
    assert store.status(1)[0]["result"] == "42"
    assert not store.claim_delivery("draft", 1, "post")
    with pytest.raises(Rejected, match="удалено"):
        store.claim_delivery("draft", 999, "post")
    assert not store.db.execute("PRAGMA foreign_key_check").fetchall()
    store.close()


async def test_selection_review_confirm_and_partial_failure(env):
    bot, service, stub = env
    draft, options = setup(service)
    await bot.handle(button(f"sel:{draft}:1:post", 1))
    assert "Выбраны" in sent_text(stub)
    old = json.loads(stub.requests[-1].content)["reply_markup"]["inline_keyboard"][0][0][
        "callback_data"
    ]
    await bot.handle(button(f"sel:{draft}:2:post", 2))
    confirm = json.loads(stub.requests[-1].content)["reply_markup"]["inline_keyboard"][0][0][
        "callback_data"
    ]
    await bot.handle(button(old, 3))
    assert "изменился" in sent_text(stub)
    original = service.gateway.publish

    async def publish(account, target, *args):
        if target["id"] == 2:
            raise Uncertain("Unknown outcome")
        return await original(account, target, *args)

    service.gateway.publish = publish
    await bot.handle(button(confirm, 4))
    assert "отправлено" in sent_text(stub) and "Unknown outcome" in sent_text(stub)
    assert {r["state"] for r in service.store.status(1)} == {"sent", "unknown"}
    await bot.handle(button(confirm, 5))
    assert "уже обработан" in sent_text(stub)
    await bot.handle(button(f"sel:{draft}:1:post", 6))
    assert "подтверждён" in sent_text(stub)


async def test_alias_replaces_account_and_does_not_repeat_publication(env):
    bot, service, stub = env
    draft, options = setup(service)
    await bot.handle(button(f"sel:{draft}:1:post", 1))
    await bot.handle(button(f"sel:{draft}:3:post", 2))
    assert bot.selections.selected(draft) == [{"target": 3, "mode": "post"}]
    await service.publish(1, draft, 1, "post")
    with pytest.raises(Rejected, match="обработана"):
        await service.publish(1, draft, 3, "post")
    with service.store.db:
        service.store.db.execute("DELETE FROM peer_deliveries")
    # Existing target-level claims still prevent duplicates even without the peer index.
    assert not service.store.claim_delivery(draft, 1, "post")


async def test_toggle_empty_foreign_and_malformed(env):
    bot, service, stub = env
    draft, options = setup(service)
    for index, data in enumerate(
        [
            f"rev:{draft}",
            f"sel:{draft}:999:post",
            f"sel:{draft}:1:story",
            f"sel:{draft}:1:post",
            f"sel:{draft}:1:post",
            f"go:{draft}",
            f"sel:{draft}",
        ],
        1,
    ):
        await bot.handle(button(data, index))
    assert not bot.selections.selected(draft)
    alien = button(f"sel:{draft}:1:post", 30)
    alien["callback_query"]["from"]["id"] = 2
    alien["callback_query"]["message"]["chat"]["id"] = 2
    await bot.handle(alien)
    assert "не найден" in sent_text(stub)


async def test_login_disabled_and_enabled_command(env):
    bot, _, stub = env
    await bot.handle(message("/login"))
    assert "не настроен" in sent_text(stub)
    bot.login = type(
        "Login", (), {"issue": AsyncMock(return_value="https://login.example/login/opaque")}
    )()
    await bot.handle(message("/login", update_id=2))
    payload = json.loads(stub.requests[-1].content)
    assert payload["reply_markup"]["inline_keyboard"][0][0]["url"].startswith("https://")
    assert payload["link_preview_options"]["is_disabled"]


async def test_personal_service_wiring_story_and_disconnect(env):
    bot, service, stub = env
    users = type("Users", (), {})()
    users.target = AsyncMock(return_value={"id": "10", "label": "Me", "kind": "user-story"})
    users.discover = AsyncMock(return_value=["@channel"])
    users.publish = AsyncMock(return_value="88")
    users.revoke = AsyncMock()
    service.gateway.users = users
    account = service.store.save_account(
        1, "telegram-user", {"id": 10, "label": "Ivan"}, "private-session"
    )
    await service.add(1, account, "me", story=True)
    assert await service.discover(1, account) == 1
    assert await service.gateway.target(service.store.account(1, account), "me", story=True)
    draft = service.store.draft(1, {"media": "photo", "text": "Story", "file_id": "photo"})
    result = await service.publish(1, draft, 1, "story")
    assert result == "88"
    users.publish.assert_awaited_once()
    assert users.publish.call_args.args[-1] == b"image-content"
    await service.gateway.publish(
        service.store.account(1, account),
        {"external_id": "10"},
        {"media": "text", "text": "Hi"},
        "post",
        "key",
    )
    assert users.publish.call_args.args[-1] == b""
    users.revoke.side_effect = Rejected("temporary")
    with pytest.raises(Rejected):
        await service.disconnect(1, account)
    assert service.store.accounts(1)
    users.revoke.side_effect = None
    await bot.handle(message(f"/disconnect {account}", update_id=40))
    assert users.revoke.await_count == 2
    assert not service.store.accounts(1)


async def test_personal_not_configured(env):
    _, service, _ = env
    account = service.store.save_account(
        1, "telegram-user", {"id": 10, "label": "Ivan"}, "private-session"
    )
    with pytest.raises(Rejected, match="не настроен"):
        await service.add(1, account, "me", story=True)


async def test_unknown_credentials_never_fall_back_to_another_platform(env):
    _, service, stub = env
    unknown = {
        "platform": "future-user-session",
        "token": "synthetic-private-session",
        "external_id": "1",
    }
    with pytest.raises(Rejected, match="Неподдерживаемый"):
        await service.gateway.target(unknown, "-100")
    with pytest.raises(Rejected, match="Неподдерживаемый"):
        await service.gateway.publish(
            unknown, {"external_id": "-100"}, {"media": "text", "text": "x"}, "post", "key"
        )
    assert not stub.requests


async def test_personal_sessions_are_not_credentials_for_legacy_adapters(env):
    _, service, stub = env
    store = service.store
    session = "synthetic-private-session"
    account = store.save_account(1, "telegram-user", {"id": 10, "label": "Personal"}, session)
    legacy = dict(store.db.execute("SELECT * FROM accounts WHERE id=?", (account,)).fetchone())
    legacy["token"] = store.vault.decrypt(legacy.pop("secret")).decode()
    assert legacy["platform"] == "telegram" and not legacy["token"]
    assert store.account(1, account)["platform"] == "telegram-user"
    assert store.account(1, account)["token"] == session
    encrypted = store.db.execute("SELECT secret FROM personal_sessions").fetchone()[0]
    assert session.encode() not in encrypted and store.vault.decrypt(encrypted).decode() == session
    with pytest.raises(Rejected, match="формат"):
        await service.gateway.target(legacy, "-100")
    assert not stub.requests
    with pytest.raises(sqlite3.IntegrityError), store.db:
        store.db.execute("DELETE FROM accounts WHERE id=?", (account,))
    assert store.accounts(1)
    with pytest.raises(Rejected, match="заменить"):
        store.save_account(1, "telegram", {"id": 10, "label": "Wrong role"}, "10:synthetic")
    users = type("Users", (), {"revoke": AsyncMock()})()
    service.gateway.users = users
    await service.disconnect(1, account)
    assert not store.accounts(1)
    assert not store.db.execute("SELECT * FROM personal_sessions").fetchall()


async def test_disconnect_does_not_delete_a_replaced_unrevoked_session(env):
    _, service, _ = env
    store = service.store
    profile = {"id": 10, "label": "Personal"}
    account = store.save_account(1, "telegram-user", profile, "old-session")

    async def reconnected(_):
        store.save_account(1, "telegram-user", profile, "new-session")

    users = type("Users", (), {"revoke": AsyncMock(side_effect=reconnected)})()
    service.gateway.users = users
    with pytest.raises(Rejected, match="изменилось"):
        await service.disconnect(1, account)
    assert store.account(1, account)["token"] == "new-session"  # noqa: S105 - fake session
    users.revoke.side_effect = None
    await service.disconnect(1, account)
    assert not store.accounts(1)


async def test_story_review_explains_audience(env):
    bot, service, stub = env
    account = service.store.save_account(
        1, "telegram-user", {"id": 10, "label": "Ivan"}, "private-session"
    )
    service.store.add_target(1, account, {"id": "10", "kind": "user-story", "label": "My story"})
    draft = service.store.draft(1, {"media": "photo", "text": "Story", "file_id": "photo"})
    await bot.handle(button(f"sel:{draft}:1:story", 1))
    assert "контакты" in sent_text(stub)


async def test_pagination_and_selection_limits(env):
    bot, service, stub = env
    draft, options = setup(service)
    for index in range(4, 26):
        service.store.add_target(
            1, 1, {"id": str(-1000 - index), "kind": "channel", "label": f"Channel {index}"}
        )
    options = service.options(1, service.store.get_draft(1, draft))
    await bot.destinations(1, draft, 0)
    payload = json.loads(stub.requests[-1].content)
    assert any(
        "Следующие" in b["text"] for row in payload["reply_markup"]["inline_keyboard"] for b in row
    )
    await bot.handle(button(f"page:{draft}:1", 10))
    assert "Предыдущие" in stub.requests[-1].content.decode()
    await bot.handle(button(f"page:{draft}:9", 11))
    assert "недоступна" in sent_text(stub)
    for option in options:
        if option["id"] == 3:
            continue
        if len(bot.selections.selected(draft)) < 20:
            bot.selections.toggle(draft, option, options)
        else:
            with pytest.raises(Rejected, match="20 назначений"):
                bot.selections.toggle(draft, option, options)
    empty = service.store.draft(1, {"media": "text", "text": "Empty"})
    with pytest.raises(Rejected, match="выберите"):
        bot.selections.review(empty)


async def test_shared_destination_account_choice_and_pages(env):
    bot, service, stub = env
    draft, options = setup(service)
    await bot.destinations(1, draft, 0)
    payload = json.loads(stub.requests[-1].content)
    rows = payload["reply_markup"]["inline_keyboard"]
    assert len(rows) == 3  # Two unique destinations plus review, despite three targets.
    choose = rows[0][0]["callback_data"]
    assert choose.startswith("acct:")
    await bot.handle(button(choose, 10))
    assert "#1" in stub.requests[-1].content.decode() and "#2" in stub.requests[-1].content.decode()
    for i in range(20):
        aid = service.store.save_account(
            1, "telegram", {"id": 300 + i, "label": str(i)}, f"{300 + i}:synthetic"
        )
        service.store.add_target(1, aid, {"id": "-100", "label": "Shared", "kind": "channel"})
    await bot.account_choices(1, draft, 1, "post", 0)
    assert "Следующие аккаунты" in stub.requests[-1].content.decode()
    await bot.account_choices(1, draft, 1, "post", 1)
    assert "Предыдущие аккаунты" in stub.requests[-1].content.decode()
    with pytest.raises(Rejected):
        await bot.account_choices(1, draft, 1, "post", 9)
    with pytest.raises(Rejected):
        await bot.account_choices(1, draft, 999, "post", 0)


async def test_busy_batch_and_cancel_login(env):
    bot, service, stub = env
    draft, options = setup(service)
    async with bot.batch_lock:
        with pytest.raises(Rejected, match="ещё отправляется"):
            await bot.batch(1, draft, "fingerprint")
    bot.login = type("Login", (), {"cancel": AsyncMock()})()
    await bot.handle(message("/cancel"))
    bot.login.cancel.assert_awaited_once_with(1)
