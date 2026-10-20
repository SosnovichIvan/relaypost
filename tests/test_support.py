import asyncio
import json
import sqlite3
import time

import httpx
import pytest
from cryptography.fernet import Fernet

from relaypost.bot import Bot
from relaypost.domain import Rejected
from relaypost.store import SCHEMA, Store
from relaypost.support import PROMPT, support_recipient
from relaypost.support_store import SupportStore
from tests.conftest import message, sent_text


def parent(message_id=123, **extra):
    return {"message_id": message_id, "from": {"id": 100, "is_bot": True}, **extra}


def configure(env):
    _, service, stub = env
    bot = Bot(service, support_chat_id=99)

    def send(request):
        return httpx.Response(200, json={"ok": True, "result": {"message_id": len(stub.requests)}})

    stub.overrides["sendMessage"] = send
    return bot, service.store, stub


@pytest.mark.parametrize("value", ["", "-1", "@user", "1\nEVIL=1", " 1", str(2**52), "١"])
def test_bad_recipient(value):
    with pytest.raises(ValueError, match="SUPPORT_CHAT_ID"):
        support_recipient(value)


def test_valid_recipient():
    assert support_recipient("0") == 0
    assert support_recipient(str(2**52 - 1)) == 2**52 - 1


async def test_inline_newline_and_bot_mention(env):
    bot, store, _ = configure(env)
    await bot.handle(message("/support@relay_PostBot\nMultiline\nDetails"))
    assert (
        store.db.execute("SELECT body FROM support_messages").fetchone()[0] == "Multiline\nDetails"
    )


async def test_explicit_support_command_overrides_owner_reply(env):
    bot, store, _ = configure(env)
    await bot.handle(message("/support User issue"))
    ticket = dict(store.db.execute("SELECT * FROM support_messages").fetchone())
    await bot.handle(
        message(
            "/support Owner issue",
            owner=99,
            update_id=2,
            reply_to_message=parent(ticket["remote_id"]),
        )
    )
    row = store.db.execute("SELECT * FROM support_messages WHERE owner=99").fetchone()
    assert row["ticket"] is None and row["recipient"] == 99


async def test_support_owner_can_reply_during_closed_pilot(env):
    bot, store, stub = configure(env)
    bot.allowed_users = frozenset({1})
    await bot.handle(message("/support Test"))
    ticket = dict(store.db.execute("SELECT * FROM support_messages").fetchone())
    await bot.handle(
        message("Answer", owner=99, update_id=2, reply_to_message=parent(ticket["remote_id"]))
    )
    assert "доставлен" in sent_text(stub)
    before = stub.counts["sendMessage"]
    await bot.handle(message("/accounts", owner=99, update_id=3))
    assert stub.counts["sendMessage"] == before
    from tests.conftest import callback

    await bot.handle(callback("no-access", owner=99, update_id=4))
    assert stub.counts["answerCallbackQuery"] == 0


def test_support_deploy_env_validated_before_writing(tmp_path, monkeypatch):
    from scripts.build_deploy_env import write_env

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "100:synthetic_token")
    monkeypatch.setenv("RELAYPOST_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("SUPPORT_CHAT_ID", "99")
    target = tmp_path / "private.env"
    write_env(target)
    assert "SUPPORT_CHAT_ID=99\n" in target.read_text()
    target.unlink()
    monkeypatch.setenv("SUPPORT_CHAT_ID", "99\nINJECT=1")
    with pytest.raises(ValueError):
        write_env(target)
    assert not target.exists()


async def test_support_disabled(env):
    bot, _, stub = env
    await bot.handle(message("/support Hello"))
    assert "не настроена" in sent_text(stub)
    assert stub.counts["sendMessage"] == 1


async def test_roundtrip_and_publication_isolation(env):
    bot, store, stub = configure(env)
    await bot.handle(message("/start support"))
    assert sent_text(stub) == PROMPT
    assert json.loads(stub.requests[-1].content)["reply_markup"]["force_reply"]
    prompt_id = len(stub.requests)
    await bot.handle(message("<b>Проблема 😀</b>", update_id=2, reply_to_message=parent(prompt_id)))
    row = dict(store.db.execute("SELECT * FROM support_messages").fetchone())
    assert row["state"] == "sent" and row["recipient"] == 99
    request = json.loads(stub.requests[-2].content)
    assert request["chat_id"] == 99 and "<b>Проблема 😀</b>" in request["text"]
    assert "parse_mode" not in request and "forwardMessage" not in stub.counts
    assert request["link_preview_options"]["is_disabled"]
    assert not store.db.execute("SELECT * FROM drafts").fetchall()
    await bot.handle(
        message("Исправили", owner=99, update_id=3, reply_to_message=parent(row["remote_id"]))
    )
    outgoing = json.loads(stub.requests[-2].content)
    assert outgoing["chat_id"] == 1
    assert outgoing["text"] == f"Ответ техподдержки RelayPost\nОбращение №{row['id']}\n\nИсправили"
    assert "forward" not in outgoing and "from" not in outgoing
    assert store.db.execute("SELECT count(*) FROM support_messages").fetchone()[0] == 2
    before = stub.counts["sendMessage"]
    await bot.handle(
        message("Исправили", owner=99, update_id=3, reply_to_message=parent(row["remote_id"]))
    )
    assert stub.counts["sendMessage"] == before


async def test_inline_rate_limit_replay_and_owner_scope(env):
    bot, store, stub = configure(env)
    await bot.handle(message("/support Details"))
    row = dict(store.db.execute("SELECT * FROM support_messages").fetchone())
    assert row["body"] == "Details"
    await bot.handle(message("/support Changed", update_id=2))
    assert "минуту" in sent_text(stub)
    # A different update delivering the same source message cannot resend it.
    replay = message("/support changed", update_id=3)
    replay["message"]["message_id"] = 1
    await bot.handle(replay)
    assert "уже принято" in sent_text(stub)
    assert store.db.execute("SELECT body FROM support_messages").fetchone()[0] == "Details"
    # A different person cannot reply to a support card to contact its author.
    await bot.handle(
        message("Fake reply", owner=2, update_id=4, reply_to_message=parent(row["remote_id"]))
    )
    assert store.db.execute("SELECT count(*) FROM support_messages").fetchone()[0] == 1
    assert "Назначений" in sent_text(stub) or "назначен" in sent_text(stub)
    with store.db:
        store.db.execute("UPDATE support_messages SET created=0")
    await bot.handle(message("/support Another", update_id=5))
    assert store.db.execute("SELECT count(*) FROM support_messages").fetchone()[0] == 2


@pytest.mark.parametrize(
    "bad_parent",
    [
        parent(999),
        parent(1, forward_origin={"type": "user"}),
        {"message_id": 1, "from": {"id": 200}},
    ],
)
async def test_forwarded_or_unrelated_prompt_never_collects_messages(env, bad_parent):
    bot, store, _ = configure(env)
    await bot.handle(message("/support"))
    await bot.handle(message("Ordinary draft", update_id=2, reply_to_message=bad_parent))
    assert store.db.execute("SELECT count(*) FROM support_messages").fetchone()[0] == 0


@pytest.mark.parametrize("command", ["/cancel", "/start", "/help"])
async def test_cancel_and_commands_do_not_become_tickets(env, command):
    bot, store, _ = configure(env)
    await bot.handle(message("/support"))
    await bot.handle(message(command, update_id=2, reply_to_message=parent(1)))
    assert not store.db.execute("SELECT * FROM support_prompts").fetchall()
    assert not store.db.execute("SELECT * FROM support_messages").fetchall()


@pytest.mark.parametrize("body", ["", "   ", "😀" * 1501])
def test_text_limits(store, body):
    with pytest.raises(Rejected, match="3000"):
        SupportStore(store.db).claim(1, 1, 99, body)


async def test_media_and_expired_prompt(env):
    bot, store, stub = configure(env)
    await bot.handle(message("/support"))
    await bot.handle(message(update_id=2, reply_to_message=parent(1), photo=[{"file_id": "photo"}]))
    assert "3000" in sent_text(stub)
    await bot.handle(message("Text", update_id=3, reply_to_message=parent(1), media_group_id="a"))
    assert "только текст" in sent_text(stub)
    with store.db:
        store.db.execute("UPDATE support_prompts SET created=0")
    await bot.handle(message("Draft", update_id=4, reply_to_message=parent(1)))
    assert not store.db.execute("SELECT * FROM support_messages").fetchall()


@pytest.mark.parametrize(
    "failure,state",
    [
        (httpx.ReadTimeout("secret"), "unknown"),
        (lambda _: httpx.Response(503), "unknown"),
        ({"ok": True, "result": {}}, "unknown"),
        ({"ok": True, "result": None}, "unknown"),
        ({"ok": True, "result": {"message_id": None}}, "unknown"),
        ({"ok": True, "result": {"message_id": 0}}, "unknown"),
        ({"ok": False}, "failed"),
        (lambda _: httpx.Response(429), "failed"),
    ],
)
async def test_delivery_failures_are_persisted_and_never_retried(env, failure, state, caplog):
    bot, store, stub = configure(env)
    calls = 0

    def response(request):
        nonlocal calls
        calls += 1
        if calls > 1:
            return httpx.Response(200, json={"ok": True, "result": {"message_id": 900}})
        if isinstance(failure, Exception):
            raise failure
        return failure(request) if callable(failure) else httpx.Response(200, json=failure)

    stub.overrides["sendMessage"] = response
    await bot.handle(message("/support Issue"))
    row = store.db.execute("SELECT * FROM support_messages").fetchone()
    assert row["state"] == state
    assert row["id"] in sent_text(stub) and "secret" not in sent_text(stub) + caplog.text
    await bot.handle(message("/accounts", update_id=2))
    assert calls == 3  # One attempted delivery, one status, one accounts response.


async def test_support_reply_failure_and_recipient_change(env):
    bot, store, stub = configure(env)
    await bot.handle(message("/support Test"))
    ticket = dict(store.db.execute("SELECT * FROM support_messages").fetchone())
    bot.support.recipient = 98
    await bot.handle(
        message("Old owner", owner=99, update_id=2, reply_to_message=parent(ticket["remote_id"]))
    )
    assert store.db.execute("SELECT count(*) FROM support_messages").fetchone()[0] == 1
    bot.support.recipient = 99
    await bot.handle(
        message("/accounts", owner=99, update_id=3, reply_to_message=parent(ticket["remote_id"]))
    )
    assert store.db.execute("SELECT count(*) FROM support_messages").fetchone()[0] == 1


def test_populated_v1_migration_and_crash_recovery(tmp_path):
    path, key = str(tmp_path / "v1.db"), Fernet.generate_key().decode()
    with sqlite3.connect(path) as db:
        db.executescript(SCHEMA)
        db.execute("INSERT INTO drafts VALUES('keep',1,'{}',?)", (time.time(),))
    store = Store(path, key)
    assert store.db.execute("PRAGMA user_version").fetchone()[0] == 2
    assert store.get_draft(1, "keep") == {}
    data = SupportStore(store.db)
    data.prompt(1, 23)
    identifier = data.claim(1, 1, 99, "Saved before network")
    store.close()
    store = Store(path, key)
    assert SupportStore(store.db).is_prompt(1, 23)
    assert store.db.execute("SELECT state FROM support_messages").fetchone()[0] == "unknown"
    SupportStore(store.db).finish(identifier, "sent", 3)
    assert store.db.execute("SELECT state FROM support_messages").fetchone()[0] == "unknown"
    assert store.db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert not store.db.execute("PRAGMA foreign_key_check").fetchall()
    with pytest.raises(sqlite3.IntegrityError), store.db:
        store.db.execute("UPDATE support_messages SET state='bad'")
    store.close()


async def test_parallel_duplicate_has_one_effect(env):
    bot, store, stub = configure(env)
    await asyncio.gather(bot.handle(message("/support A")), bot.handle(message("/support A")))
    assert store.db.execute("SELECT count(*) FROM support_messages").fetchone()[0] == 1
    assert stub.counts["sendMessage"] == 2
