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
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: client)

    async def stop(bot, event):
        assert bot.allowed_users == frozenset({1, 2})

    monkeypatch.setattr(runtime, "poll", stop)
    await runtime.run()
    assert client.is_closed
    assert stub.counts["deleteWebhook"] == 1


def test_module_entrypoint(monkeypatch):
    import runpy

    from relaypost import runtime

    called = []
    monkeypatch.setattr(runtime, "main", lambda: called.append(True))
    runpy.run_module("relaypost", run_name="__main__")
    assert called == [True]


async def test_personal_runtime_lifecycle(monkeypatch, tmp_path):
    from unittest.mock import AsyncMock

    from cryptography.fernet import Fernet

    from relaypost import runtime
    from tests.conftest import TOKEN, Stub

    stub = Stub()
    client = httpx.AsyncClient(transport=httpx.MockTransport(stub))
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.setenv("RELAYPOST_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("RELAYPOST_DATABASE", str(tmp_path / "runtime.db"))
    monkeypatch.setenv("TELEGRAM_API_ID", "1")
    monkeypatch.setenv("TELEGRAM_API_HASH", "a" * 32)
    monkeypatch.setenv("TELEGRAM_PROXY_URL", "http://proxy.example:19840")
    monkeypatch.setenv("RELAYPOST_LOGIN_ORIGIN", "https://login.example")
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: client)
    started = AsyncMock()
    monkeypatch.setattr(runtime.web.TCPSite, "start", started)
    captured = []

    async def stop(bot, event):
        assert bot.login is not None and bot.gateway.users is not None
        await bot.login.issue(1)
        captured.append(bot.login)

    monkeypatch.setattr(runtime, "poll", stop)
    await runtime.run()
    assert not captured[0].tickets
    assert client.is_closed
    started.assert_awaited_once()


async def test_batch_dispatch_keeps_polling_and_reaps_login(env, monkeypatch):
    from unittest.mock import AsyncMock

    from tests.conftest import callback

    bot, _, stub = env
    stop = asyncio.Event()
    bot.login = type("Login", (), {"reap": AsyncMock()})()
    update = callback("draft")
    update["callback_query"]["data"] = "go:draft:fingerprint"
    calls = 0

    def updates(_):
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(200, json={"ok": True, "result": [update]})
        stop.set()
        return httpx.Response(200, json={"ok": True, "result": []})

    processed = asyncio.Event()

    async def handle(update):
        await asyncio.sleep(0)
        processed.set()

    monkeypatch.setattr(bot, "handle", handle)
    stub.overrides["getUpdates"] = updates
    await poll(bot, stop)
    assert calls == 2 and processed.is_set()
    assert bot.login.reap.await_count == 2


async def test_batch_task_errors_are_sanitized(env, monkeypatch, caplog):
    from tests.conftest import callback

    bot, _, stub = env
    stop = asyncio.Event()
    update = callback("draft")
    update["callback_query"]["data"] = "go:draft:fingerprint"

    def updates(_):
        stop.set()
        return httpx.Response(200, json={"ok": True, "result": [update]})

    async def fail(update):
        raise RuntimeError("secret-session")

    monkeypatch.setattr(bot, "handle", fail)
    stub.overrides["getUpdates"] = updates
    await poll(bot, stop)
    assert "secret-session" not in caplog.text
    assert "Batch processing failed" in caplog.text


async def test_batch_cancelled_on_shutdown(env, monkeypatch):
    from tests.conftest import callback

    bot, _, stub = env
    stop = asyncio.Event()
    update = callback("draft")
    update["callback_query"]["data"] = "go:draft:fingerprint"
    started = asyncio.Event()
    finished = asyncio.Event()
    calls = 0

    def updates(_):
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(200, json={"ok": True, "result": [update]})
        raise httpx.ReadTimeout("synthetic")

    async def handle(update):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            finished.set()

    monkeypatch.setattr(bot, "handle", handle)
    stub.overrides["getUpdates"] = updates
    task = asyncio.create_task(poll(bot, stop))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert finished.is_set()
