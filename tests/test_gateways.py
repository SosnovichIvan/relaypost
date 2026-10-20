import httpx
import pytest

from relaypost.domain import Rejected, Uncertain
from tests.conftest import OTHER


@pytest.mark.parametrize(
    "platform,target,mode,media",
    [
        ("telegram", "-100", "post", "text"),
        ("telegram", "-100", "post", "photo"),
        ("telegram", "-100", "post", "video"),
        ("telegram-business", "connection", "story", "photo"),
        ("telegram-business", "connection", "story", "video"),
        ("max", "-300", "post", "text"),
        ("vk", "400", "post", "text"),
        ("vk", "-1", "post", "text"),
        ("max", "-300", "post", "photo"),
        ("max", "-300", "post", "video"),
        ("vk", "-1", "post", "photo"),
        ("vk", "400", "post", "video"),
        ("vk", "-1", "story", "photo"),
        ("vk", "400", "story", "video"),
    ],
)
async def test_provider_contracts(env, platform, target, mode, media):
    _, service, stub = env
    token = OTHER if platform == "telegram" else "opaque-token"
    if platform == "telegram-business":
        account_id = service.store.save_account(
            1, platform, {"id": "connection", "label": "Business"}, "connection"
        )
    else:
        account_id = await service.connect(1, platform, token)
    await service.add(1, account_id, target, story=(mode == "story" and platform == "vk"))
    draft = service.store.draft(
        1,
        {
            "text": "" if mode == "story" and platform == "vk" else "News",
            "media": media,
            "file_id": "file",
        },
    )
    result = await service.publish(1, draft, 1, mode)
    assert result
    assert service.store.status(1)[0]["state"] == "sent"
    request = stub.requests[-1]
    if media != "text" and platform.startswith("telegram"):
        assert b"image-content" in request.content
        assert b"attach://media" in request.content
        if mode == "story":
            assert b"86400" in request.content
            assert b"connection" in request.content
    elif platform == "max":
        assert request.headers["Authorization"] == token
        assert request.url.params["chat_id"] == target
    elif platform == "vk" and mode != "story":
        assert b"guid=" in request.content
        assert f"owner_id={target}".encode() in request.content


@pytest.mark.parametrize(
    "path,override,expected",
    [
        ("getChat", {"ok": True, "result": {"id": 1, "type": "private"}}, "канал или группу"),
        ("getChatMember", {"ok": True, "result": {"status": "member"}}, "администратором"),
        ("getChatMember", {"ok": True, "result": {"status": "administrator"}}, "право публикации"),
    ],
)
async def test_telegram_permissions(env, path, override, expected):
    _, service, stub = env
    await service.connect(1, "telegram", OTHER)
    stub.overrides[path] = override
    with pytest.raises(Rejected, match=expected):
        await service.add(1, 1, "-100")


async def test_group_and_creator(env):
    _, service, stub = env
    await service.connect(1, "telegram", OTHER)
    stub.overrides["getChat"] = {
        "ok": True,
        "result": {"id": -100, "type": "group", "title": "Group"},
    }
    stub.overrides["getChatMember"] = {"ok": True, "result": {"status": "creator"}}
    await service.add(1, 1, "-100")
    assert service.store.targets(1)[0]["kind"] == "chat"


@pytest.mark.parametrize("enabled,rights", [(False, {}), (True, {})])
async def test_business_permissions(env, enabled, rights):
    _, service, stub = env
    service.store.save_account(1, "telegram-business", {"id": "c", "label": "Business"}, "c")
    stub.overrides["getBusinessConnection"] = {
        "ok": True,
        "result": {"is_enabled": enabled, "rights": rights},
    }
    with pytest.raises(Rejected, match="историями"):
        await service.add(1, 1, "c")


async def test_max_and_vk_permissions(env):
    _, service, stub = env
    max_id = await service.connect(1, "max", "token")
    with pytest.raises(Rejected, match="числовой"):
        await service.add(1, max_id, "abc")
    stub.overrides["me"] = {"is_admin": False}
    with pytest.raises(Rejected, match="администратора"):
        await service.add(1, max_id, "-100")
    vk_id = await service.connect(1, "vk", "token")
    with pytest.raises(Rejected, match="принадлежать"):
        await service.add(1, vk_id, "401")
    stub.overrides["groups.getById"] = {"response": {"groups": [{"is_admin": 0, "can_post": 1}]}}
    with pytest.raises(Rejected, match="сообщества"):
        await service.add(1, vk_id, "-1")


@pytest.mark.parametrize(
    "status,write,kind",
    [
        (429, False, Rejected),
        (403, False, Rejected),
        (503, False, Rejected),
        (503, True, Uncertain),
        (429, True, Rejected),
        (403, True, Rejected),
    ],
)
async def test_http_failures(env, status, write, kind):
    _, service, stub = env
    stub.overrides["test"] = lambda _: httpx.Response(status, json={"description": "secret"})
    with pytest.raises(kind) as error:
        await service.gateway.request("POST", "https://api.telegram.org/test", write=write)
    assert "secret" not in str(error.value)
    if kind is Rejected:
        assert not isinstance(error.value, Uncertain)


@pytest.mark.parametrize("write,kind", [(False, Rejected), (True, Uncertain)])
@pytest.mark.parametrize("failure", ["timeout", "malformed"])
async def test_timeout_and_malformed(env, write, kind, failure):
    _, service, stub = env
    stub.overrides["test"] = (
        httpx.ReadTimeout("secret-token")
        if failure == "timeout"
        else lambda _: httpx.Response(200, content=b"invalid")
    )
    with pytest.raises(kind) as error:
        await service.gateway.request("POST", "https://api.telegram.org/test", write=write)
    assert "secret-token" not in str(error.value)


@pytest.mark.parametrize(
    "payload", [{"ok": False, "description": "secret"}, {"error": {"error_msg": "secret"}}]
)
async def test_api_rejection(env, payload):
    _, service, stub = env
    stub.overrides["test"] = payload
    with pytest.raises(Rejected) as error:
        await service.gateway.request("GET", "https://api.telegram.org/test")
    assert "secret" not in str(error.value)


@pytest.mark.parametrize(
    "path,size,expected",
    [
        ("../token", 10, "путь"),
        ("https://evil/secret", 10, "путь"),
        ("photos/a.jpg", 30_000_000, "20 МБ"),
    ],
)
async def test_media_limits(env, path, size, expected):
    _, service, stub = env
    stub.overrides["getFile"] = {"ok": True, "result": {"file_path": path, "file_size": size}}
    with pytest.raises(Rejected, match=expected):
        await service.gateway.media("file")


async def test_media_download_failures(env):
    _, service, stub = env
    stub.overrides["a.jpg"] = lambda _: httpx.Response(403)
    with pytest.raises(Rejected, match="загрузить"):
        await service.gateway.media("file")
    stub.overrides["a.jpg"] = lambda _: httpx.Response(200, content=b"a" * 20_000_001)
    with pytest.raises(Rejected, match="20 МБ"):
        await service.gateway.media("file")


async def test_story_only_vk(env):
    _, service, _ = env
    await service.connect(1, "telegram", OTHER)
    with pytest.raises(Rejected, match="историй ВК"):
        await service.add(1, 1, "-100", story=True)


async def test_bad_provider_payload_after_intent(env):
    _, service, stub = env
    await service.connect(1, "telegram", OTHER)
    await service.add(1, 1, "-100")
    draft = service.store.draft(1, {"media": "text", "text": "a"})
    stub.overrides["sendMessage"] = {"ok": True, "result": {}}
    with pytest.raises(Uncertain):
        await service.publish(1, draft, 1, "post")
    assert service.store.status(1)[0]["state"] == "unknown"


async def test_max_pending_attachment(env):
    _, service, stub = env
    stub.overrides["messages"] = {"code": "attachment.not.ready"}
    with pytest.raises(Rejected, match="обрабатывается"):
        await service.gateway.request("POST", "https://platform-api2.max.ru/messages", write=True)


async def test_discovery(env):
    bot, service, stub = env
    from tests.conftest import message, sent_text

    max_id = await service.connect(1, "max", "token")
    stub.overrides["chats"] = {"chats": [{"chat_id": -300}, {"chat_id": -301}]}
    await bot.handle(message(f"/discover {max_id}"))
    assert "2" in sent_text(stub)
    stub.overrides["me"] = {"is_admin": False}
    assert await service.discover(1, max_id) == 0
    vk_id = await service.connect(1, "vk", "token")
    stub.overrides["groups.get"] = {"response": {"items": [{"id": 1}]}}
    assert await service.discover(1, vk_id) == 2
    tg_id = await service.connect(1, "telegram", OTHER)
    with pytest.raises(Rejected, match="не перечисляет"):
        await service.discover(1, tg_id)
