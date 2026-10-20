"""Short-lived QR authorization, browser binding and explicit encrypted-session consent."""

import asyncio
import re
import secrets
import time
from dataclasses import dataclass, field
from typing import Any, Optional
from urllib.parse import urlsplit

from telethon import errors, types  # type: ignore[import-untyped]

from relaypost.domain import Rejected
from relaypost.store import Store
from relaypost.telegram_user import TelegramUsers


def login_origin(value: str) -> str:
    try:
        url = urlsplit(value)
        port = url.port
    except ValueError:
        raise Rejected("Login requires a valid HTTPS origin.") from None
    if (
        url.scheme != "https"
        or not url.hostname
        or url.username
        or url.password
        or url.path not in {"", "/"}
        or url.query
        or url.fragment
        or port == 0
        or not re.fullmatch(r"[a-zA-Z0-9.-]+", url.hostname)
        or any(c.isspace() for c in value)
    ):
        raise Rejected("Login requires a dedicated HTTPS origin.")
    return f"https://{url.hostname}" + (f":{port}" if port not in {None, 443} else "")


@dataclass
class Login:
    owner: int
    created: float = field(default_factory=time.monotonic)
    csrf: str = field(default_factory=lambda: secrets.token_urlsafe(32))
    browser: str = ""
    state: str = "new"
    client: Any = None
    qr: str = ""
    profile: dict[str, Any] = field(default_factory=dict)
    waiter: Optional[asyncio.Task[None]] = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    attempts: int = 0
    phone: str = ""
    phone_code_hash: str = ""


class UserLogin:
    def __init__(self, store: Store, users: TelegramUsers, origin: str):
        self.store, self.users, self.origin = store, users, login_origin(origin)
        self.tickets: dict[str, Login] = {}
        self.last_started: dict[int, float] = {}

    async def issue(self, owner: int) -> str:
        await self.reap()
        for token, entry in list(self.tickets.items()):
            if entry.owner == owner or time.monotonic() - entry.created > 600:
                await self.discard(token)
        if len(self.tickets) >= 100:
            raise Rejected("Слишком много запросов входа. Попробуйте позже.")
        token = secrets.token_urlsafe(32)
        self.tickets[token] = Login(owner)
        return f"{self.origin}/login/{token}"

    async def get(self, token: str) -> Login:
        entry = self.tickets.get(token)
        if entry is None:
            raise Rejected("Ссылка входа недействительна. Получите новую: /login.")
        if time.monotonic() - entry.created > 600:
            await self.discard(token)
            raise Rejected("Ссылка истекла. Получите новую: /login.")
        return entry

    async def reap(self) -> None:
        await asyncio.gather(
            *(
                self.discard(token)
                for token, entry in list(self.tickets.items())
                if time.monotonic() - entry.created > 600
            )
        )

    async def identity(self, entry: Login) -> None:
        user = await asyncio.wait_for(entry.client.get_me(), 15)
        if user is None or user.bot:
            raise Rejected("Нужен личный Telegram-аккаунт.")
        entry.profile = {"id": user.id, "label": f"{user.first_name or 'Telegram'} · {user.id}"}
        entry.phone, entry.phone_code_hash = "", ""
        entry.state = "confirm"

    async def cancel(self, owner: int) -> None:
        await asyncio.gather(
            *(
                self.discard(token)
                for token, entry in list(self.tickets.items())
                if entry.owner == owner
            )
        )

    async def wait(self, entry: Login, qr: Any) -> None:
        try:
            await qr.wait(timeout=60)
            await self.identity(entry)
        except errors.SessionPasswordNeededError:
            entry.state = "password"
        except (errors.RPCError, OSError, asyncio.TimeoutError, Rejected):
            entry.state = "expired"
            await entry.client.disconnect()

    async def begin(self, entry: Login) -> None:
        now = time.monotonic()
        self.last_started = {k: v for k, v in self.last_started.items() if now - v < 30}
        if entry.owner in self.last_started or len(self.last_started) >= 1000:
            raise Rejected("Новый вход возможен через 30 секунд.")
        self.last_started[entry.owner] = now
        entry.client = self.users.client(login=True)
        await asyncio.wait_for(entry.client.connect(), 15)

    async def act(
        self, token: str, action: str, password: str = "", *, phone: str = "", code: str = ""
    ) -> None:
        entry = await self.get(token)
        async with entry.lock:
            try:
                if action == "begin" and entry.state == "new":
                    await self.begin(entry)
                    qr = await asyncio.wait_for(entry.client.qr_login(), 15)
                    entry.qr, entry.state = qr.url, "qr"
                    entry.waiter = asyncio.create_task(self.wait(entry, qr))
                elif action == "phone" and entry.state == "new":
                    if not re.fullmatch(r"\+[1-9][0-9]{6,14}", phone):
                        raise Rejected("Укажите номер с кодом страны, например +79991234567.")
                    await self.begin(entry)
                    entry.phone = phone
                    result = await asyncio.wait_for(entry.client.send_code_request(phone), 20)
                    if isinstance(result, types.auth.SentCodeSuccess):
                        await self.identity(entry)
                    elif isinstance(result, types.auth.SentCodePaymentRequired):
                        entry.state = "expired"
                        await entry.client.disconnect()
                        raise Rejected(
                            "Telegram требует другой способ входа. "
                            "Получите новую ссылку и используйте QR."
                        )
                    else:
                        entry.phone_code_hash = result.phone_code_hash
                        entry.state = "code"
                elif action == "code" and entry.state == "code":
                    entry.attempts += 1
                    if entry.attempts > 3 or not code.strip() or len(code) > 64:
                        raise Rejected("Лимит попыток кода. Отмените вход и начните заново.")
                    await asyncio.wait_for(
                        entry.client.sign_in(
                            phone=entry.phone, code=code, phone_code_hash=entry.phone_code_hash
                        ),
                        20,
                    )
                    await self.identity(entry)
                elif action == "password" and entry.state == "password":
                    entry.attempts += 1
                    if entry.attempts > 3 or not password or len(password) > 256:
                        raise Rejected("Лимит попыток 2FA. Отмените вход и начните заново.")
                    await asyncio.wait_for(entry.client.sign_in(password=password), 20)
                    await self.identity(entry)
                elif action == "confirm" and entry.state == "confirm":
                    session = entry.client.session.save()
                    existing = self.store.identity_account(
                        entry.owner, "telegram-user", str(entry.profile["id"])
                    )
                    if existing is not None:
                        await self.users.revoke(existing)
                    self.store.save_account(entry.owner, "telegram-user", entry.profile, session)
                    entry.state = "saved"
                    await entry.client.disconnect()
                elif action == "cancel":
                    if entry.state == "saved":
                        raise Rejected(
                            "Аккаунт уже сохранён. Для отзыва используйте "
                            "/accounts и /disconnect в боте."
                        )
                    await self.remove(token, entry)
                else:
                    raise Rejected("Действие устарело. Обновите страницу.")
            except errors.SessionPasswordNeededError:
                if action not in {"begin", "phone", "code"}:
                    raise Rejected("Telegram отклонил проверку 2FA. Повторите вход.") from None
                entry.state, entry.attempts = "password", 0
            except (errors.RPCError, OSError, asyncio.TimeoutError):
                if action in {"begin", "phone"} and entry.client is not None:
                    entry.state = "expired"
                    await entry.client.disconnect()
                raise Rejected(
                    "Telegram отклонил вход или не ответил. Проверьте данные и попробуйте позже."
                ) from None

    async def discard(self, token: str) -> None:
        entry = self.tickets.get(token)
        if entry is not None:
            async with entry.lock:
                await self.remove(token, entry)

    async def remove(self, token: str, entry: Login) -> None:
        self.tickets.pop(token, None)
        if entry.waiter is not None:
            entry.waiter.cancel()
            await asyncio.gather(entry.waiter, return_exceptions=True)
        if entry.client is not None:
            try:
                if entry.state != "saved":
                    await asyncio.wait_for(entry.client.connect(), 5)
                    # Always try logout: the library's cached authorization flag can
                    # be stale after a lost sign-in response.
                    await asyncio.wait_for(entry.client.log_out(), 5)
            except (errors.RPCError, OSError, asyncio.TimeoutError):
                pass  # No session is persisted; user can revoke this device in Telegram.
            finally:
                await entry.client.disconnect()

    async def close(self) -> None:
        await asyncio.gather(*(self.discard(token) for token in list(self.tickets)))
