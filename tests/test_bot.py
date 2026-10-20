import json

import pytest

from relaypost.bot import Bot
from tests.conftest import OTHER, callback, message, sent_text


async def test_full_publish_flow_and_repeat(env):
    bot, service, stub = env
    await bot.handle(message(f"/connect telegram {OTHER}"))
    assert "подключён" in sent_text(stub)
    assert stub.requests[0].url.path.endswith("deleteMessage")
    await bot.handle(message("/add 1 -100", update_id=2))
    await bot.handle(message("News", update_id=3))
    reply = json.loads(stub.requests[-1].content)
    data = reply["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
    draft_id = data.split(":")[1]
    await bot.handle(callback(draft_id))
    assert service.store.status(1)[0]["state"] == "sent"
    count = len([r for r in stub.requests if r.url.path == f"/bot{OTHER}/sendMessage"])
    await bot.handle(callback(draft_id))
    await bot.handle(callback(draft_id, update_id=21))
    assert "уже обработана" in sent_text(stub)
    assert len([r for r in stub.requests if r.url.path == f"/bot{OTHER}/sendMessage"]) == count
    await bot.handle(callback(draft_id, owner=2, update_id=22))
    assert "не найден" in sent_text(stub)


@pytest.mark.parametrize(
    "command,expected",
    [
        ("/start", "RelayPost"),
        ("/help", "RelayPost"),
        ("/cancel", "RelayPost"),
        ("/connect", "RelayPost"),
        ("/accounts", "Подключений нет"),
        ("/targets", "Назначений нет"),
        ("/status", "Публикаций нет"),
        ("/add x -1", "Неверный формат"),
        ("/add", "Неизвестная команда"),
        ("/disconnect 999", "не найден"),
        ("/wat", "Неизвестная команда"),
        ("/connect bad secret", "Поддерживаются"),
        ("/connect telegram", "Формат:"),
        ("/connect telegram bad", "Неверный формат"),
        ("Hello", "Нет назначений"),
    ],
)
async def test_commands_empty(env, command, expected):
    bot, _, stub = env
    await bot.handle(message(command))
    assert expected in sent_text(stub)
    assert "secret" not in sent_text(stub)


async def test_list_status_disconnect(env):
    bot, service, stub = env
    await service.connect(1, "telegram", OTHER)
    await service.add(1, 1, "-100")
    for index, command in enumerate(["/accounts", "/targets"], 1):
        await bot.handle(message(command, update_id=index))
        assert "#1" in sent_text(stub)
    draft = service.store.draft(1, {"media": "text", "text": "a"})
    await service.publish(1, draft, 1, "post")
    await bot.handle(message("/status", update_id=3))
    assert "sent" in sent_text(stub)
    await bot.handle(message("/disconnect 1", update_id=4))
    assert not service.store.accounts(1)
    assert "удалены" in sent_text(stub)


async def test_ignores_untrusted_updates(env):
    bot, service, stub = env
    updates = [
        {"update_id": 1},
        message("a", chat={"id": -1, "type": "group"}),
        message("a", **{"from": {"id": 1, "is_bot": True}}),
        message("a", **{"from": {}}),
        message("a", chat={"id": 2, "type": "private"}),
        {"update_id": 3, "callback_query": {"from": {"id": 1}}},
    ]
    for update in updates:
        await bot.handle(update)
    restricted = Bot(service, frozenset({9}))
    await restricted.handle(message("/start"))
    assert not stub.requests


async def test_business_and_revoke(env):
    bot, service, stub = env
    data = {
        "id": "connection",
        "is_enabled": True,
        "user": {"id": 1, "first_name": "Ivan"},
        "rights": {"can_manage_stories": True},
    }
    restricted = Bot(service, frozenset({9}))
    await restricted.handle({"update_id": 8, "business_connection": data})
    assert not service.store.accounts(1)
    await bot.handle({"update_id": 8, "business_connection": data})
    await bot.handle({"update_id": 8, "business_connection": data})
    assert len(service.store.accounts(1)) == 1
    await bot.handle(message(photo=[{"file_id": "small"}, {"file_id": "big"}], update_id=9))
    assert "История" in stub.requests[-1].content.decode()
    draft = service.store.db.execute("SELECT id FROM drafts").fetchone()[0]
    assert service.store.get_draft(1, draft)["file_id"] == "big"
    await bot.handle(callback(draft, mode="story"))
    assert service.store.status(1)[0]["state"] == "sent"
    data["is_enabled"] = False
    await bot.handle({"update_id": 30, "business_connection": data})
    assert not service.store.accounts(1)


@pytest.mark.parametrize(
    "kwargs,expected",
    [
        ({"media_group_id": "album"}, "Альбомы"),
        ({"video": {"file_id": "v", "file_size": 30_000_000}}, "20 МБ"),
        ({"document": {"file_id": "d"}}, "Отправьте текст"),
    ],
)
async def test_content_rejections(env, kwargs, expected):
    bot, _, stub = env
    await bot.handle(message(**kwargs))
    assert expected in sent_text(stub)


async def test_video_and_bad_callbacks(env):
    bot, service, stub = env
    await service.connect(1, "telegram", OTHER)
    await service.add(1, 1, "-100")
    await bot.handle(message(video={"file_id": "video"}, caption="Caption"))
    draft = service.store.db.execute("SELECT id FROM drafts").fetchone()[0]
    assert service.store.get_draft(1, draft)["media"] == "video"
    bad = callback(draft)
    bad["callback_query"]["data"] = "bad"
    await bot.handle(bad)
    assert "Неизвестная кнопка" in sent_text(stub)
    bad["update_id"] += 1
    bad["callback_query"]["data"] = f"pub:{draft}:bad:post"
    await bot.handle(bad)
    assert "Неверный формат" in sent_text(stub)
