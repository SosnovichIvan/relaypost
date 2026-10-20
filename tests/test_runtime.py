import asyncio
import logging

import httpx
import pytest

from relaypost.domain import Rejected
from relaypost.runtime import main, poll, run
from tests.conftest import message


async def test_poll_processes_updates(env):
    bot, _, stub = env
    stop = asyncio.Event()

    def updates(_):
        stop.set()
        return httpx.Response(200, json={"ok": True, "result": [message("/start")]})

    stub.overrides["getUpdates"] = updates
    await poll(bot, stop)
    assert stub.counts["sendMessage"] == 1


async def test_poll_process_failure(env, monkeypatch, caplog):
    bot, _, stub = env
    stop = asyncio.Event()

    def updates(_):
        stop.set()
        return httpx.Response(200, json={"ok": True, "result": [{"update_id": 1}]})

    stub.overrides["getUpdates"] = updates

    async def fail(_):
        raise Rejected("secret")

    monkeypatch.setattr(bot, "handle", fail)
    await poll(bot, stop)
    assert "secret" not in caplog.text
    assert "processing failed" in caplog.text


async def test_poll_reconnect(env, monkeypatch):
    bot, _, stub = env
    stop = asyncio.Event()
    stub.overrides["getUpdates"] = {"ok": False}
    original = asyncio.wait_for
    calls = 0

    async def fast_wait(awaitable, timeout):
        nonlocal calls
        calls += 1
        if calls == 2:
            stop.set()
        return await original(awaitable, timeout=0.001)

    monkeypatch.setattr(asyncio, "wait_for", fast_wait)
    await poll(bot, stop)
    assert calls == 2


async def test_run_missing_config(monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("RELAYPOST_ENCRYPTION_KEY", raising=False)
    with pytest.raises(Rejected, match="Задайте"):
        await run()


def test_main_errors_and_interrupt(monkeypatch, caplog):
    def fail(coroutine):
        coroutine.close()
        raise Rejected("secret")

    monkeypatch.setattr(asyncio, "run", fail)
    with pytest.raises(SystemExit) as error:
        main()
    assert error.value.code == 1
    assert "secret" not in caplog.text
    assert logging.getLogger("httpx").level == logging.CRITICAL

    def interrupt(coroutine):
        coroutine.close()
        raise KeyboardInterrupt

    monkeypatch.setattr(asyncio, "run", interrupt)
    main()


async def test_run_lifecycle(monkeypatch, tmp_path):
    from cryptography.fernet import Fernet

    from relaypost import runtime
    from tests.conftest import TOKEN, Stub

    stub = Stub()
    client = httpx.AsyncClient(transport=httpx.MockTransport(stub))
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.setenv("RELAYPOST_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("RELAYPOST_DATABASE", str(tmp_path / "run.db"))
    monkeypatch.setenv("RELAYPOST_ALLOWED_USERS", "1, 2,")
    monkeypatch.setenv("SUPPORT_CHAT_ID", "99")
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: client)

    async def stop(bot, event):
        assert bot.allowed_users == frozenset({1, 2})
        assert bot.support.recipient == 99

    monkeypatch.setattr(runtime, "poll", stop)
    await runtime.run()
    assert client.is_closed
    assert stub.counts["deleteWebhook"] == 1
    assert stub.counts["setMyCommands"] == 1


def test_module_entrypoint(monkeypatch):
    import runpy

    from relaypost import runtime

    called = []
    monkeypatch.setattr(runtime, "main", lambda: called.append(True))
    runpy.run_module("relaypost", run_name="__main__")
    assert called == [True]
