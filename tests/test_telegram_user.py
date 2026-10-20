import asyncio
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest
from telethon import errors, functions, types
from telethon.sessions import StringSession

from relaypost.domain import Rejected, Uncertain, formats, validate_content
from relaypost.telegram_user import TelegramUsers


def channel(identifier=1, *, admin=True, broadcast=True):
    return types.Channel(
        id=identifier,
        title=f"Channel {identifier}",
        photo=types.ChatPhotoEmpty(),
        date=None,
        broadcast=broadcast,
        megagroup=not broadcast,
        admin_rights=types.ChatAdminRights(post_messages=admin) if admin else None,
    )


class Client:
    def __init__(self):
        self.me = types.User(id=10, first_name="Ivan", bot=False)
        self.entity = channel()
        self.connect = AsyncMock()
        self.disconnect = AsyncMock()
        self.get_me = AsyncMock(side_effect=lambda: self.me)
        self.get_permissions = AsyncMock(return_value=NS(is_admin=True))
        self.get_dialogs = AsyncMock(return_value=[NS(entity=self.entity)])
        self.get_entity = AsyncMock(side_effect=lambda _: self.entity)
        self.get_input_entity = AsyncMock(return_value=types.InputPeerChannel(1, 42))
        self.upload_file = AsyncMock(return_value=types.InputFile(1, 1, "photo.jpg", ""))
        self.log_out = AsyncMock(return_value=True)
        self.is_user_authorized = AsyncMock(return_value=True)
        self.sign_in = AsyncMock()
        self.session = NS(save=lambda: "synthetic-personal-session")
        self.requests = []
        self.result = "message"
        self.failure = None

    async def __call__(self, request):
        self.requests.append(request)
        if self.failure:
            raise self.failure
        if isinstance(request, functions.stories.CanSendStoryRequest):
            return True
        if self.result == "none":
            return NS(updates=[])
        if self.result == "short":
            return types.UpdateShortSentMessage(id=80, pts=1, pts_count=1, date=None)
        if self.result == "new":
            return NS(
                updates=[
                    types.UpdateNewMessage(types.Message(id=81, peer_id=types.PeerUser(10)), 1, 1)
                ]
            )
        update = (
            types.UpdateStoryID(82, request.random_id)
            if isinstance(request, functions.stories.SendStoryRequest)
            else types.UpdateMessageID(83, request.random_id)
        )
        return NS(updates=[update])


@pytest.fixture
def personal(monkeypatch):
    adapter = TelegramUsers(1, "a" * 32, "http://proxy.example:19840")
    client = Client()
    monkeypatch.setattr(adapter, "client", lambda *args, **kwargs: client)
    return adapter, client


ACCOUNT = {"external_id": "10", "token": "synthetic-personal-session", "platform": "telegram-user"}
TARGET = {"external_id": "-1000000000001", "kind": "channel"}


@pytest.mark.parametrize(
    "api_id,api_hash,proxy",
    [(0, "a" * 32, "http://proxy:1"), (1, "x" * 32, "http://proxy:1"), (1, "a" * 32, "")],
)
def test_config_rejected(api_id, api_hash, proxy):
    with pytest.raises(Rejected):
        TelegramUsers(api_id, api_hash, proxy)


def test_real_client_settings_no_network():
    adapter = TelegramUsers(1, "a" * 32, "http://proxy.example:19840")
    client = adapter.client()
    assert client.session.port == 443
    assert client._proxy["proxy_type"] == "http"
    assert client._request_retries == 0 and not client._auto_reconnect
    assert isinstance(client.session, StringSession)
    assert adapter.client(client.session.save(), login=True).session.port == 443


@pytest.mark.parametrize("story,kind", [(False, "channel"), (True, "user-story")])
async def test_resolve_and_discover(personal, story, kind):
    adapter, client = personal
    result = await adapter.target(ACCOUNT, "@channel", story=story)
    assert result["kind"] == kind
    assert result["id"] == "-1000000000001"
    assert await adapter.discover(ACCOUNT) == ["-1000000000001"]
    client.entity.broadcast = False
    assert (await adapter.target(ACCOUNT, "-1000000000001"))["kind"] == "chat"
    client.entity = client.me
    assert (await adapter.target(ACCOUNT, "me", story=True))["id"] == "10"
    with pytest.raises(Rejected, match="профиль"):
        await adapter.target(ACCOUNT, "10")
    client.entity = types.User(id=20, first_name="Other")
    with pytest.raises(Rejected):
        await adapter.target(ACCOUNT, "20", story=True)
    client.entity = channel(admin=False)
    with pytest.raises(Rejected, match="публикации"):
        await adapter.target(ACCOUNT, "@channel")
    client.get_permissions.return_value = NS(is_admin=False)
    with pytest.raises(Rejected, match="администратора"):
        await adapter.target(ACCOUNT, "@channel")
    client.entity = NS()
    # Peer ID conversion rejects unknown entities before attempting any writes.
    with pytest.raises(Rejected):
        await adapter.target(ACCOUNT, "unknown")


@pytest.mark.parametrize("me", [None, types.User(id=20)])
async def test_wrong_session(personal, me):
    adapter, client = personal
    client.me = me
    with pytest.raises(Rejected, match="недействительна"):
        await adapter.target(ACCOUNT, "@channel")
    client.disconnect.assert_awaited_once()


@pytest.mark.parametrize(
    "failure,error",
    [
        (errors.ChatAdminRequiredError(None), Rejected),
        (OSError(), Uncertain),
        (asyncio.TimeoutError(), Uncertain),
    ],
)
async def test_read_failures_sanitized(personal, failure, error):
    adapter, client = personal
    client.connect.side_effect = failure
    with pytest.raises(error):
        await adapter.target(ACCOUNT, "@channel")
    client.disconnect.assert_awaited_once()


@pytest.mark.parametrize(
    "media,mode,result",
    [
        ("text", "post", "message"),
        ("text", "message", "short"),
        ("photo", "post", "new"),
        ("photo", "story", "message"),
        ("video", "story", "message"),
        ("video", "post", "message"),
    ],
)
async def test_raw_publication(personal, media, mode, result):
    adapter, client = personal
    client.result = result
    content = {"media": media, "text": "Caption", "duration": 2, "width": 720, "height": 1280}
    response = await adapter.publish(
        ACCOUNT,
        dict(TARGET, kind="chat" if mode == "message" else "channel"),
        content,
        mode,
        "stable-delivery",
        b"fixture",
    )
    assert response in {"80", "81", "82", "83"}
    request = client.requests[-1]
    first = request.random_id if hasattr(request, "random_id") else None
    await adapter.publish(ACCOUNT, TARGET, content, mode, "stable-delivery", b"fixture")
    assert client.requests[-1].random_id == first
    if mode == "story":
        assert request.period == 86400
        assert isinstance(request.privacy_rules[0], types.InputPrivacyValueAllowAll)
    if mode == "message":
        assert isinstance(request.send_as, types.InputPeerSelf)


@pytest.mark.parametrize(
    "failure", [errors.ServerError(None, "private"), errors.RpcCallFailError(None)]
)
async def test_write_unknown(personal, failure):
    adapter, client = personal
    # Fail only the final write, after rights validation.
    client.failure = failure
    with pytest.raises(Uncertain):
        await adapter.publish(ACCOUNT, TARGET, {"media": "text", "text": "x"}, "post", "key", b"")


async def test_incomplete_write_response(personal):
    adapter, client = personal
    client.result = "none"
    with pytest.raises(Uncertain, match="ID"):
        await adapter.publish(ACCOUNT, TARGET, {"media": "text", "text": "x"}, "post", "key", b"")


async def test_personal_story_is_contacts_only(personal):
    adapter, client = personal
    client.entity = client.me
    client.get_input_entity.return_value = types.InputPeerSelf()
    await adapter.publish(
        ACCOUNT,
        {"external_id": "10", "kind": "user-story"},
        {"media": "photo", "text": "Hello"},
        "story",
        "key",
        b"photo",
    )
    assert isinstance(client.requests[-1].privacy_rules[0], types.InputPrivacyValueAllowContacts)


async def test_member_group_rights(personal):
    adapter, client = personal
    client.entity = channel(broadcast=False)
    client.get_permissions.return_value = NS(is_admin=False, has_left=False, anonymous=False)
    assert (await adapter.target(ACCOUNT, "@group"))["kind"] == "chat"
    client.get_permissions.return_value.has_left = True
    with pytest.raises(Rejected, match="права отправлять"):
        await adapter.target(ACCOUNT, "@group")
    client.get_permissions.return_value = NS(is_admin=True, anonymous=True)
    with pytest.raises(Rejected, match="Анонимный"):
        await adapter.target(ACCOUNT, "@group")
    client.get_permissions.return_value = NS(is_admin=False)
    client.entity.default_banned_rights = types.ChatBannedRights(
        until_date=None, send_messages=True
    )
    with pytest.raises(Rejected, match="права отправлять"):
        await adapter.target(ACCOUNT, "@group")


async def test_revoke(personal):
    adapter, client = personal
    await adapter.revoke(ACCOUNT)
    client.log_out.assert_awaited_once()
    client.me = None
    await adapter.revoke(ACCOUNT)
    client.me = types.User(id=11)
    with pytest.raises(Rejected, match="соответствует"):
        await adapter.revoke(ACCOUNT)


async def test_session_lock_serializes_logout_after_active_work(personal):
    adapter, client = personal
    entered = asyncio.Event()
    release = asyncio.Event()

    async def active():
        async with adapter.connected(ACCOUNT):
            entered.set()
            await release.wait()

    operation = asyncio.create_task(active())
    await entered.wait()
    logout = asyncio.create_task(adapter.revoke(ACCOUNT))
    await asyncio.sleep(0)
    client.log_out.assert_not_awaited()
    release.set()
    await asyncio.gather(operation, logout)
    client.log_out.assert_awaited_once()
    client.me = types.User(id=10)
    client.log_out.return_value = False
    with pytest.raises(Rejected, match="отозвать"):
        await adapter.revoke(ACCOUNT)
    client.connect.side_effect = errors.AuthKeyUnregisteredError(None)
    await adapter.revoke(ACCOUNT)
    client.connect.side_effect = OSError()
    with pytest.raises(Rejected, match="проверить"):
        await adapter.revoke(ACCOUNT)


def test_user_formats_and_limits():
    assert formats("telegram-user", "channel", "text") == ["post"]
    assert formats("telegram-user", "chat", "video") == ["message"]
    assert formats("telegram-user", "user-story", "text") == []
    assert formats("telegram-user", "user-story", "photo") == ["story"]
    with pytest.raises(Rejected):
        validate_content({"media": "text", "text": "x" * 4097}, "telegram-user", "post")
    with pytest.raises(Rejected, match="размеры"):
        validate_content(
            {"media": "video", "text": "x", "duration": 1, "width": 0, "height": 1},
            "telegram-user",
            "post",
        )
    validate_content(
        {"media": "video", "text": "x", "duration": 1, "width": 720, "height": 1280},
        "telegram-user",
        "post",
    )
