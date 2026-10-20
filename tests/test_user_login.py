import asyncio
import sqlite3
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest
from aiohttp.test_utils import TestClient, TestServer
from telethon import errors, types

from relaypost.domain import Rejected
from relaypost.login_web import application, page
from relaypost.user_login import Login, UserLogin, login_origin
from tests.test_telegram_user import Client


@pytest.fixture
def login(store):
    client = Client()
    qr = NS(url="tg://login?token=fixture", wait=AsyncMock())
    client.qr_login = AsyncMock(return_value=qr)
    client.send_code_request = AsyncMock(return_value=NS(phone_code_hash="synthetic-hash"))
    users = NS(client=lambda **kwargs: client, revoke=AsyncMock())
    return UserLogin(store, users, "https://login.example"), client, qr


@pytest.mark.parametrize(
    "url",
    [
        "http://login.example",
        "https://user:pass@login.example",
        "https://login.example/path",
        "https://login.example?q=1",
        "https://login.example#fragment",
        "https://login.example:0",
        "https://login.example:invalid",
        "https://login example",
        "https://login.example\n",
        "",
    ],
)
def test_bad_origin(url):
    with pytest.raises(Rejected):
        login_origin(url)


def test_canonical_origin():
    assert login_origin("https://LOGIN.EXAMPLE:443/") == "https://login.example"
    assert login_origin("https://LOGIN.EXAMPLE:8443/") == "https://login.example:8443"


async def start(login):
    manager, client, qr = login
    token = (await manager.issue(1)).rsplit("/", 1)[1]
    await manager.act(token, "begin")
    await manager.tickets[token].waiter
    return token


async def test_full_login_requires_explicit_save_and_encrypts(login, store):
    manager, client, _ = login
    token = await start(login)
    entry = await manager.get(token)
    assert entry.state == "confirm" and not store.accounts(1)
    await manager.act(token, "confirm")
    account = store.account(1, store.accounts(1)[0]["id"])
    assert account["token"] == "synthetic-personal-session"  # noqa: S105 - fake test session
    assert (
        b"synthetic-personal-session"
        not in store.db.execute("SELECT secret FROM accounts").fetchone()[0]
    )
    with pytest.raises(Rejected):
        store.account(2, account["id"])
    assert entry.state == "saved"
    await manager.close()
    client.log_out.assert_not_awaited()


async def test_password_failure_budget_and_cancel(login):
    manager, client, qr = login
    qr.wait.side_effect = errors.SessionPasswordNeededError(None)
    token = await start(login)
    assert manager.tickets[token].state == "password"
    client.sign_in.side_effect = errors.PasswordHashInvalidError(None)
    with pytest.raises(Rejected, match="отклонил"):
        await manager.act(token, "password", "secret")
    with pytest.raises(Rejected, match="Лимит"):
        await manager.act(token, "password", "")
    with pytest.raises(Rejected, match="Лимит"):
        await manager.act(token, "password", "x" * 257)
    with pytest.raises(Rejected, match="Лимит"):
        await manager.act(token, "password", "ok")
    assert not hasattr(manager.tickets[token], "password")
    await manager.act(token, "cancel")
    client.log_out.assert_awaited_once()
    assert not manager.tickets


async def test_password_success(login):
    manager, client, qr = login
    qr.wait.side_effect = errors.SessionPasswordNeededError(None)
    token = await start(login)
    await manager.act(token, "password", "synthetic password")
    assert manager.tickets[token].state == "confirm"
    await manager.close()


async def test_phone_code_2fa_and_confirmation(login, store):
    manager, client, _ = login
    token = (await manager.issue(1)).rsplit("/", 1)[1]
    with pytest.raises(Rejected, match="кодом страны"):
        await manager.act(token, "phone", phone="invalid")
    client.connect.assert_not_awaited()
    await manager.act(token, "phone", phone="+79990000000")
    entry = manager.tickets[token]
    assert entry.state == "code" and not store.accounts(1)
    client.sign_in.side_effect = errors.SessionPasswordNeededError(None)
    await manager.act(token, "code", code="12345")
    assert entry.state == "password" and entry.attempts == 0
    client.sign_in.side_effect = None
    await manager.act(token, "password", password="synthetic-2fa")  # noqa: S106 - fake 2FA
    assert entry.state == "confirm"
    assert entry.phone == entry.phone_code_hash == ""
    assert not hasattr(entry, "code") and not hasattr(entry, "password")
    assert not store.accounts(1)
    await manager.act(token, "confirm")
    assert store.accounts(1)
    await manager.close()


async def test_phone_code_errors_and_budget(login):
    manager, client, _ = login
    token = (await manager.issue(1)).rsplit("/", 1)[1]
    await manager.act(token, "phone", phone="+79990000000")
    client.sign_in.side_effect = errors.PhoneCodeInvalidError(None)
    for _ in range(3):
        with pytest.raises(Rejected, match="отклонил"):
            await manager.act(token, "code", code="wrong")
    with pytest.raises(Rejected, match="Лимит попыток кода"):
        await manager.act(token, "code", code="12345")
    assert client.sign_in.await_count == 3
    await manager.close()


@pytest.mark.parametrize("code", ["", "x" * 65])
async def test_invalid_code_never_sent(login, code):
    manager, client, _ = login
    token = (await manager.issue(1)).rsplit("/", 1)[1]
    await manager.act(token, "phone", phone="+79990000000")
    with pytest.raises(Rejected, match="Лимит попыток кода"):
        await manager.act(token, "code", code=code)
    client.sign_in.assert_not_awaited()
    await manager.close()


async def test_phone_direct_authorization_and_paid_rejection(login):
    manager, client, _ = login
    client.send_code_request.return_value = types.auth.SentCodeSuccess(
        types.auth.Authorization(client.me)
    )
    token = (await manager.issue(1)).rsplit("/", 1)[1]
    await manager.act(token, "phone", phone="+79990000000")
    assert manager.tickets[token].state == "confirm"
    await manager.close()
    manager.last_started.clear()
    client.send_code_request.return_value = types.auth.SentCodePaymentRequired(
        "product", "hash", "support@example.org", "subject", 1, "USD", 100
    )
    token = (await manager.issue(1)).rsplit("/", 1)[1]
    with pytest.raises(Rejected, match="другой способ"):
        await manager.act(token, "phone", phone="+79990000000")
    assert manager.tickets[token].state == "expired"
    await manager.close()


async def test_web_phone_login(login):
    manager, client, _ = login
    token = (await manager.issue(1)).rsplit("/", 1)[1]
    route = "/login/" + token
    async with TestClient(TestServer(application(manager))) as browser:
        response = await browser.get(route)
        assert 'type="tel"' in await response.text()
        entry = manager.tickets[token]
        headers = {"Cookie": "__Host-relaypost=" + entry.browser, "Origin": manager.origin}
        response = await browser.post(
            route,
            data={"csrf": entry.csrf, "action": "phone", "phone": "+79990000000"},
            headers=headers,
            allow_redirects=False,
        )
        assert response.status == 303
        response = await browser.get(route, headers=headers)
        assert "one-time-code" in await response.text()
        response = await browser.post(
            route,
            data={"csrf": entry.csrf, "action": "code", "code": "12345"},
            headers=headers,
            allow_redirects=False,
        )
        assert response.status == 303 and entry.state == "confirm"
        response = await browser.post(
            route, data={"csrf": entry.csrf, "action": "cancel"}, headers=headers
        )
        assert response.status == 200 and "отменён" in await response.text()
    await manager.close()


async def test_repeated_2fa_challenge_does_not_reset_budget(login):
    manager, client, qr = login
    qr.wait.side_effect = errors.SessionPasswordNeededError(None)
    token = await start(login)
    client.sign_in.side_effect = errors.SessionPasswordNeededError(None)
    for attempt in range(1, 4):
        with pytest.raises(Rejected, match="отклонил"):
            await manager.act(token, "password", "synthetic")
        assert manager.tickets[token].attempts == attempt
    with pytest.raises(Rejected, match="Лимит"):
        await manager.act(token, "password", "synthetic")
    await manager.close()


async def test_database_failure_keeps_confirmation_and_hides_details(login, monkeypatch):
    manager, client, _ = login
    token = await start(login)
    route = "/login/" + token

    def fail(*args):
        raise sqlite3.OperationalError("private-session-detail")

    monkeypatch.setattr(manager.store, "save_account", fail)
    async with TestClient(TestServer(application(manager))) as browser:
        await browser.get(route)
        entry = manager.tickets[token]
        headers = {"Cookie": "__Host-relaypost=" + entry.browser, "Origin": manager.origin}
        response = await browser.post(
            route, data={"csrf": entry.csrf, "action": "confirm"}, headers=headers
        )
        assert response.status == 503
        assert "private-session-detail" not in await response.text()
        assert entry.state == "confirm" and not manager.store.accounts(1)
    await manager.close()


async def test_reauthorization_revokes_old_session_before_replacing(login, store):
    manager, client, _ = login
    existing = store.save_account(1, "telegram-user", {"id": 10, "label": "Old"}, "old-session")
    token = await start(login)
    manager.users.revoke.side_effect = Rejected("temporary")
    with pytest.raises(Rejected):
        await manager.act(token, "confirm")
    assert store.account(1, existing)["token"] == "old-session"  # noqa: S105 - fake test session
    manager.users.revoke.side_effect = None
    await manager.act(token, "confirm")
    assert store.accounts(1)[0]["id"] == existing
    assert store.account(1, existing)["token"] != "old-session"  # noqa: S105 - fake test session
    await manager.close()


async def test_begin_rate_limit_and_cancel_owner_scope(login):
    manager, client, _ = login
    token = await start(login)
    await manager.issue(2)
    await manager.cancel(1)
    assert all(entry.owner == 2 for entry in manager.tickets.values())
    token = (await manager.issue(1)).rsplit("/", 1)[1]
    with pytest.raises(Rejected, match="30 секунд"):
        await manager.act(token, "begin")
    await manager.close()


@pytest.mark.parametrize(
    "failure", [asyncio.TimeoutError(), errors.AuthKeyUnregisteredError(None), OSError()]
)
async def test_qr_expiry_and_network_errors(login, failure):
    manager, client, qr = login
    qr.wait.side_effect = failure
    token = await start(login)
    assert manager.tickets[token].state == "expired"
    with pytest.raises(Rejected, match="устарело"):
        await manager.act(token, "confirm")
    client.is_user_authorized.return_value = False
    await manager.close()


@pytest.mark.parametrize("me", [None, NS(id=1, bot=True)])
async def test_only_personal_users(login, me):
    manager, client, qr = login
    client.me = me
    token = await start(login)
    assert manager.tickets[token].state == "expired"
    await manager.close()


async def test_expiry_and_invalidation(login):
    manager, client, _ = login
    token = (await manager.issue(1)).rsplit("/", 1)[1]
    manager.tickets[token].created -= 601
    with pytest.raises(Rejected, match="истекла"):
        await manager.get(token)
    with pytest.raises(Rejected, match="недействительна"):
        await manager.get(token)
    token = (await manager.issue(1)).rsplit("/", 1)[1]
    await manager.issue(1)
    assert token not in manager.tickets
    for item in manager.tickets.values():
        item.created -= 601
    await manager.reap()
    assert not manager.tickets
    manager.tickets = {str(i): Login(i + 100) for i in range(100)}
    with pytest.raises(Rejected, match="много"):
        await manager.issue(1)
    await manager.close()


async def test_reaper_and_issue_expired_with_revoke_failure(login):
    manager, client, _ = login
    token = await start(login)
    manager.tickets[token].created -= 601
    client.log_out.side_effect = OSError("private")
    await manager.issue(2)
    assert token not in manager.tickets
    client.disconnect.assert_awaited()
    await manager.close()


async def test_login_start_error(login):
    manager, client, _ = login
    client.connect.side_effect = OSError("private")
    token = (await manager.issue(1)).rsplit("/", 1)[1]
    with pytest.raises(Rejected):
        await manager.act(token, "begin")
    await manager.close()


async def test_web_csrf_browser_binding_and_states(login):
    manager, client, _ = login
    token = (await manager.issue(1)).rsplit("/", 1)[1]
    route = "/login/" + token
    async with TestClient(TestServer(application(manager))) as browser:
        response = await browser.get(route)
        assert response.status == 200 and response.headers["Cache-Control"] == "no-store"
        text = await response.text()
        assert "широкий доступ" in text and "<script" not in text
        entry = manager.tickets[token]
        cookie = {"Cookie": "__Host-relaypost=" + entry.browser, "Origin": manager.origin}
        response = await browser.post(
            route, data={"action": "begin", "csrf": "wrong"}, headers=cookie
        )
        assert response.status == 403
        response = await browser.post(
            route, data={"action": "begin", "csrf": "ошибка"}, headers=cookie
        )
        assert response.status == 403
        response = await browser.get(route)
        assert (
            response.status == 403
        )  # Another browser does not receive the Secure cookie over HTTP.
        response = await browser.post(
            route,
            data={"action": "begin", "csrf": entry.csrf},
            headers=cookie,
            allow_redirects=False,
        )
        assert response.status == 303
        await entry.waiter
        response = await browser.get(route, headers=cookie)
        assert "Сохранить аккаунт" in await response.text()
        response = await browser.post(
            route, data={"action": "password", "csrf": entry.csrf}, headers=cookie
        )
        assert response.status == 400
        response = await browser.post(
            route,
            data={"action": "confirm", "csrf": entry.csrf},
            headers=cookie,
            allow_redirects=False,
        )
        assert response.status == 303
        response = await browser.get(route, headers=cookie)
        assert "Аккаунт подключён" in await response.text()
        response = await browser.post(
            route, data={"action": "cancel", "csrf": entry.csrf}, headers=cookie
        )
        assert response.status == 400 and "уже сохранён" in await response.text()
        await manager.cancel(1)
        assert (await browser.get(route)).status == 403
    await manager.close()


def test_qr_password_expired_pages_escape_html():
    entry = Login(1, state="qr", qr="tg://login?token=fixture")
    assert "data:image/svg+xml;base64," in page(entry)
    assert 'http-equiv="refresh"' in page(entry)
    entry.state = "password"
    assert 'type="password"' in page(entry) and 'http-equiv="refresh"' not in page(entry)
    entry.state = "confirm"
    entry.profile = {"id": 1, "label": "<script>bad</script>"}
    assert "<script>bad" not in page(entry)
    entry.state = "expired"
    assert "Время входа истекло" in page(entry)
    assert "&lt;b&gt;" in page(entry, "<b>error</b>")
