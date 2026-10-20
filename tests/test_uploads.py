import httpx
import pytest

from relaypost.domain import Rejected
from relaypost.uploads import upload, upload_url


@pytest.mark.parametrize(
    "url,platform",
    [
        ("http://pu.vk.com/a", "vk"),
        ("https://vk.com.evil.ru/a", "vk"),
        ("https://127.0.0.1/a", "max"),
        ("https://evil.ru/a", "max"),
        ("https://user@iu.oneme.ru/a", "max"),
        ("https://pu.vk.com:444/a", "vk"),
        ("https://pu.vk.com/a#fragment", "vk"),
        ("https://u:p@pu.vk.com/a", "vk"),
    ],
)
def test_url_allowlist(url, platform):
    with pytest.raises(Rejected):
        upload_url(url, platform)


def test_valid_url():
    assert upload_url("https://vk.com/a", "vk") == "https://vk.com/a"


async def test_upload_failures(env):
    _, service, stub = env
    stub.overrides["photo"] = lambda _: httpx.Response(403)
    with pytest.raises(Rejected, match="загрузить"):
        await upload(service.gateway.client, "https://pu.vk.com/photo", "vk", "photo", b"x", True)
    stub.overrides["photo"] = httpx.ReadTimeout("secret")
    with pytest.raises(Rejected, match="подтвердить") as exc:
        await upload(service.gateway.client, "https://pu.vk.com/photo", "vk", "photo", b"x", True)
    assert "secret" not in str(exc.value)
    stub.overrides["photo"] = lambda _: httpx.Response(200, content=b"bad")
    with pytest.raises(Rejected):
        await upload(service.gateway.client, "https://pu.vk.com/photo", "vk", "photo", b"x", True)
