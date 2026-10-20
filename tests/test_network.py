import asyncio
import runpy

import httpx
import pytest

from relaypost.domain import Rejected
from relaypost.gateways import Gateways
from relaypost.network import create_client, main, preflight, validate_proxy


@pytest.mark.parametrize(
    "value", ["", "http://telegram-proxy:19840", "http://127.0.0.1:19840/", "http://[::1]:19840"]
)
def test_proxy_configuration(value):
    assert validate_proxy(value) == value


@pytest.mark.parametrize(
    "value",
    [
        "https://proxy:443",
        "socks5://proxy:1080",
        "http://proxy",
        "http://proxy:0",
        "http://proxy:70000",
        "http://proxy:invalid",
        "http://user:password@proxy:3128",
        "http://proxy:3128/private",
        "http://proxy:3128?secret=value",
        "http://proxy:3128#secret",
        "http://proxy:3128/\n",
    ],
)
def test_invalid_proxy_does_not_disclose_configuration(value):
    with pytest.raises(Rejected) as error:
        validate_proxy(value)
    assert value not in str(error.value)


async def test_telegram_only_routing_including_file_downloads(monkeypatch):
    requests = []
    transports = []

    def transport(**kwargs):
        proxied = bool(kwargs.get("proxy"))
        transports.append(kwargs)

        def receive(request):
            requests.append((str(request.url), proxied))
            return httpx.Response(200, json={"ok": True, "result": True})

        return httpx.MockTransport(receive)

    monkeypatch.setattr(httpx, "AsyncHTTPTransport", transport)
    monkeypatch.setattr("httpx._client.AsyncHTTPTransport", transport)
    async with create_client("http://telegram-proxy:19840") as client:
        for url in [
            "https://api.telegram.org/bot100:synthetic/getMe",
            "https://api.telegram.org/file/bot100:synthetic/photos/a.jpg",
            "https://api.vk.com/method/users.get",
            "https://platform-api2.max.ru/me",
            "https://api.telegram.org.example.com/",
        ]:
            await client.get(url)
    assert [proxied for _, proxied in requests] == [True, True, False, False, False]
    assert all(item["trust_env"] is False for item in transports)
    assert all(item.get("verify") is not False for item in transports)


async def test_no_fallback_when_proxy_fails(monkeypatch):
    paths = []

    def transport(**kwargs):
        proxy = bool(kwargs.get("proxy"))

        def receive(request):
            paths.append(proxy)
            if proxy:
                raise httpx.ProxyError("private infrastructure diagnostic")
            return httpx.Response(200, json={"ok": True, "result": True})

        return httpx.MockTransport(receive)

    monkeypatch.setattr(httpx, "AsyncHTTPTransport", transport)
    async with create_client("http://telegram-proxy:19840") as client:
        gateway = Gateways(client, "100:synthetic")
        with pytest.raises(Rejected) as error:
            await gateway.tg("100:synthetic", "getMe")
    assert paths == [True]
    assert "private infrastructure" not in str(error.value)


@pytest.mark.parametrize("status,error", [(200, False), (302, False), (503, True)])
async def test_token_free_preflight(monkeypatch, status, error):
    requests = []

    def receive(request):
        requests.append(request)
        return httpx.Response(status)

    client = httpx.AsyncClient(transport=httpx.MockTransport(receive))
    monkeypatch.setattr("relaypost.network.create_client", lambda _: client)
    monkeypatch.setenv("TELEGRAM_PROXY_URL", "http://telegram-proxy:19840")
    if error:
        with pytest.raises(Rejected):
            await preflight()
    else:
        await preflight()
    assert client.is_closed
    assert requests[0].method == "HEAD"
    assert str(requests[0].url) == "https://api.telegram.org/"


async def test_preflight_connection_error(monkeypatch):
    def receive(request):
        raise httpx.ProxyError("private-address")

    client = httpx.AsyncClient(transport=httpx.MockTransport(receive))
    monkeypatch.setattr("relaypost.network.create_client", lambda _: client)
    with pytest.raises(Rejected) as error:
        await preflight()
    assert str(error.value) == "Telegram proxy preflight unavailable"
    assert error.value.__cause__ is None
    assert client.is_closed


def test_preflight_cli_failure(monkeypatch):
    def fail(coroutine):
        coroutine.close()
        raise Rejected("private-configuration")

    monkeypatch.setattr(asyncio, "run", fail)
    with pytest.raises(SystemExit) as error:
        main()
    assert str(error.value) == "Telegram proxy preflight failed"


def test_preflight_module_entrypoint(monkeypatch):
    called = []

    def succeed(coroutine):
        coroutine.close()
        called.append(True)

    monkeypatch.setattr(asyncio, "run", succeed)
    runpy.run_path("relaypost/network.py", run_name="__main__")
    assert called == [True]
