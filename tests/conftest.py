import json
from collections import defaultdict

import httpx
import pytest
from cryptography.fernet import Fernet

from relaypost.bot import Bot
from relaypost.gateways import Gateways
from relaypost.service import Service
from relaypost.store import Store

TOKEN = "100:main_token"  # noqa: S105 - synthetic credential used only by MockTransport
OTHER = "200:other_token"


class Stub:
    def __init__(self):
        self.requests = []
        self.overrides = {}
        self.counts = defaultdict(int)

    def __call__(self, request):
        self.requests.append(request)
        name = request.url.path.rsplit("/", 1)[-1]
        self.counts[name] += 1
        if name in self.overrides:
            value = self.overrides[name]
            if isinstance(value, Exception):
                raise value
            if callable(value):
                return value(request)
            return httpx.Response(200, json=value)
        if request.url.host == "api.telegram.org":
            data = {
                "getMe": {"id": 200, "username": "connected"},
                "getChat": {"id": -100, "type": "channel", "title": "Channel"},
                "getChatMember": {"status": "administrator", "can_post_messages": True},
                "getBusinessConnection": {
                    "is_enabled": True,
                    "rights": {"can_manage_stories": True},
                },
                "getFile": {"file_path": "photos/a.jpg", "file_size": 100},
                "getUpdates": [],
                "sendMessage": {"message_id": 123},
                "sendPhoto": {"message_id": 124},
                "sendVideo": {"message_id": 125},
                "postStory": {"id": 126},
            }.get(name, True)
            if "/file/" in request.url.path:
                return httpx.Response(200, content=b"image-content")
            return httpx.Response(200, json={"ok": True, "result": data})
        if request.url.host == "platform-api2.max.ru":
            if name == "me" and "/members/" in request.url.path:
                data = {"is_admin": True}
            elif name == "me":
                data = {"user_id": 300, "name": "MAX bot"}
            elif name == "uploads":
                data = {"url": "https://iu.oneme.ru/uploadImage", "token": "media-token"}
            elif name == "messages":
                data = {"message": {"body": {"mid": "max-id"}}}
            else:
                data = {"type": "channel", "title": "MAX channel"}
            return httpx.Response(200, json=data)
        if request.url.host == "api.vk.com":
            data = {
                "users.get": [{"id": 400, "first_name": "Ivan", "last_name": "Test"}],
                "groups.getById": {
                    "groups": [{"id": 1, "name": "VK group", "is_admin": 1, "can_post": 1}]
                },
                "wall.post": {"post_id": 127},
                "photos.getWallUploadServer": {"upload_url": "https://pu.vk.com/photo"},
                "photos.saveWallPhoto": [{"owner_id": -1, "id": 77}],
                "video.save": {
                    "upload_url": "https://vu.vk.com/video",
                    "owner_id": -1,
                    "video_id": 78,
                },
                "stories.getPhotoUploadServer": {"upload_url": "https://su.vk.com/story"},
                "stories.getVideoUploadServer": {"upload_url": "https://su.vk.com/story"},
                "stories.save": {"items": [{"id": 79}]},
            }[name]
            return httpx.Response(200, json={"response": data})
        if request.url.host == "iu.oneme.ru":
            return httpx.Response(200, json={"photos": {"id": {"token": "media-token"}}})
        if request.url.host == "pu.vk.com":
            return httpx.Response(200, json={"server": 1, "photo": "photo-data", "hash": "hash"})
        if request.url.host == "vu.vk.com":
            return httpx.Response(200, json={"video_id": 78})
        if request.url.host == "su.vk.com":
            return httpx.Response(200, json={"response": {"upload_result": "story-data"}})
        raise AssertionError(f"Unexpected outbound host: {request.url.host}")


@pytest.fixture
def store(tmp_path):
    result = Store(str(tmp_path / "test.db"), Fernet.generate_key().decode())
    yield result
    result.close()


@pytest.fixture
async def env(store):
    stub = Stub()
    async with httpx.AsyncClient(transport=httpx.MockTransport(stub)) as client:
        gateway = Gateways(client, TOKEN)
        service = Service(store, gateway)
        yield Bot(service), service, stub


def message(text="", *, owner=1, update_id=1, **kwargs):
    return {
        "update_id": update_id,
        "message": {
            "message_id": update_id,
            "from": {"id": owner},
            "chat": {"id": owner, "type": "private"},
            "text": text,
            **kwargs,
        },
    }


def callback(draft_id, target_id=1, mode="post", *, owner=1, update_id=20):
    return {
        "update_id": update_id,
        "callback_query": {
            "id": "cb",
            "from": {"id": owner},
            "message": {"chat": {"id": owner, "type": "private"}},
            "data": f"pub:{draft_id}:{target_id}:{mode}",
        },
    }


def sent_text(stub):
    return json.loads(stub.requests[-1].content)["text"]


@pytest.fixture(autouse=True)
def forbid_real_network(monkeypatch):
    async def deny_async(*args, **kwargs):
        raise AssertionError("Live network forbidden in tests")

    def deny_sync(*args, **kwargs):
        raise AssertionError("Live network forbidden in tests")

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", deny_async)
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", deny_sync)
