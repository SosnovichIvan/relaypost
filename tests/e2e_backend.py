"""Isolated HTTPS browser fixture; never imported/copied by the production image."""

import asyncio
import ipaddress
import json
import os
import ssl
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
from aiohttp import web
from cryptography import x509
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from telethon import TelegramClient, errors, functions, types

from relaypost.bot import Bot
from relaypost.gateways import Gateways
from relaypost.login_web import application
from relaypost.service import Service
from relaypost.store import Store
from relaypost.telegram_user import TelegramUsers
from relaypost.user_login import UserLogin
from tests.conftest import TOKEN, Stub, callback, message
from tests.test_telegram_user import Client, channel


def certificate(directory):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "RelayPost local E2E")])
    now = datetime.now(timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(
            x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]),
            critical=False,
        )
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    (directory / "cert.pem").write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    target = directory / "key.pem"
    target.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    target.chmod(0o600)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(directory / "cert.pem", target)
    return context


async def forbidden(*args, **kwargs):
    raise AssertionError("Real Telegram/network forbidden in E2E")


def build(directory):
    # Local HTTPS/browser networking remains real. External provider I/O is denied.
    TelegramClient.connect = forbidden
    httpx.AsyncHTTPTransport.handle_async_request = forbidden
    store = Store(str(directory / "fixture.db"), Fernet.generate_key().decode())
    stub = Stub()
    http = httpx.AsyncClient(transport=httpx.MockTransport(stub))
    users = TelegramUsers(1, "a" * 32, "http://blocked.invalid:1")
    current = {}
    users.client = lambda *args, **kwargs: current["client"]
    login = UserLogin(store, users, "https://127.0.0.1:8443")
    gateway = Gateways(http, TOKEN, users)
    bot = Bot(Service(store, gateway), login=login)
    app = application(login)

    async def start(request):
        scenario = (await request.json()).get("scenario", "phone")
        await login.close()
        login.last_started.clear()
        store.db.executescript(
            "DELETE FROM personal_sessions; DELETE FROM accounts; "
            "DELETE FROM drafts; DELETE FROM updates;"
        )
        stub.requests.clear()
        client = Client()
        accepted = asyncio.Event()
        peers = [channel(1), channel(2)]

        def entity(value):
            if isinstance(value, int) and value > 0:
                return client.me
            return peers[1] if str(value) in {"@two", "-1000000000002"} else peers[0]

        client.get_entity = AsyncMock(side_effect=entity)
        client.get_dialogs = AsyncMock(return_value=[SimpleNamespace(entity=p) for p in peers])
        client.get_input_entity = AsyncMock(
            side_effect=lambda value: types.InputPeerChannel(entity(value).id, 42)
        )
        client.send_code_request = AsyncMock(
            return_value=SimpleNamespace(phone_code_hash="fixture-code-hash")
        )

        async def sign_in(**values):
            if "code" in values:
                if values["code"] != "12345":
                    raise errors.PhoneCodeInvalidError(None)
                if scenario == "2fa":
                    raise errors.SessionPasswordNeededError(None)
            elif values.get("password") != "fixture-password":
                raise errors.PasswordHashInvalidError(None)
            return client.me

        async def wait(timeout):
            await asyncio.wait_for(accepted.wait(), timeout)

        client.sign_in = AsyncMock(side_effect=sign_in)
        client.qr_login = AsyncMock(
            return_value=SimpleNamespace(url="tg://login?token=local-fixture", wait=wait)
        )
        current.update(client=client, accepted=accepted)
        # Enter through the actual Telegram dispatcher, not UserLogin.issue directly.
        await bot.handle(message("/login"))
        payload = json.loads(stub.requests[-1].content)
        return web.json_response({"url": payload["reply_markup"]["inline_keyboard"][0][0]["url"]})

    async def state(request):
        accounts = store.accounts(1)
        private = store.db.execute("SELECT secret FROM personal_sessions").fetchone()
        writes = [
            r
            for r in current["client"].requests
            if isinstance(r, functions.messages.SendMessageRequest)
        ]
        return web.json_response(
            {
                "accounts": accounts,
                "encrypted": bool(private and b"synthetic-personal-session" not in private[0]),
                "legacy_empty": all(
                    store.vault.decrypt(r[0]) == b""
                    for r in store.db.execute("SELECT secret FROM accounts")
                ),
                "writes": [{"peer": r.peer.channel_id, "text": r.message} for r in writes],
                "status": store.status(1),
                "revocations": current["client"].log_out.await_count,
            }
        )

    async def publish(request):
        account = store.accounts(1)[0]["id"]
        await bot.handle(message(f"/discover {account}", update_id=2))
        await bot.handle(message("Browser-confirmed news", update_id=3))
        draft = store.db.execute("SELECT id FROM drafts").fetchone()[0]
        for index, target in enumerate(store.targets(1), 4):
            update = callback(draft, update_id=index)
            update["callback_query"]["data"] = f"sel:{draft}:{target['id']}:post"
            await bot.handle(update)
        confirmation = json.loads(stub.requests[-1].content)["reply_markup"]["inline_keyboard"][0][
            0
        ]["callback_data"]
        update = callback(draft, update_id=10)
        update["callback_query"]["data"] = confirmation
        await bot.handle(update)
        update["update_id"] = 11
        await bot.handle(update)  # Replay must not send a second batch.
        return await state(request)

    async def scan(request):
        current["accepted"].set()
        return web.json_response({"accepted": True})

    async def expire(request):
        for entry in login.tickets.values():
            entry.created -= 601
        return web.json_response({"expired": True})

    async def health(request):
        return web.json_response({"ready": True})

    async def close(app):
        await login.close()
        await http.aclose()
        store.close()

    app.router.add_get("/__fixture/health", health)
    app.router.add_post("/__fixture/start", start)
    app.router.add_get("/__fixture/state", state)
    app.router.add_post("/__fixture/publish", publish)
    app.router.add_post("/__fixture/scan", scan)
    app.router.add_post("/__fixture/expire", expire)
    app.on_cleanup.append(close)
    return app


if __name__ == "__main__":
    directory = Path(os.environ["RELAYPOST_E2E_DIR"])
    web.run_app(
        build(directory),
        host="127.0.0.1",
        port=8443,
        ssl_context=certificate(directory),
        access_log=None,
        print=None,
    )
