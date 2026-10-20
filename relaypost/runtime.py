"""Single-process long polling with bounded network timeouts and no token logging."""

import asyncio
import logging
import os
import signal
from typing import Optional

from aiohttp import web

from relaypost.bot import Bot
from relaypost.domain import Rejected
from relaypost.gateways import Gateways
from relaypost.health import clear_readiness, mark_ready
from relaypost.login_web import application
from relaypost.network import create_client
from relaypost.service import Service
from relaypost.store import Store
from relaypost.telegram_user import TelegramUsers
from relaypost.user_login import UserLogin

log = logging.getLogger("relaypost")


async def process_update(bot: Bot, update: dict[str, object]) -> None:
    try:
        await bot.handle(update)
    except (Rejected, KeyError, TypeError):
        log.warning("Update processing failed; inspect delivery status")


async def poll(bot: Bot, stop: asyncio.Event) -> None:
    offset: Optional[int] = None
    background: set[asyncio.Task[None]] = set()

    def completed(task: asyncio.Task[None]) -> None:
        background.discard(task)
        if not task.cancelled() and task.exception() is not None:
            log.warning("Batch processing failed; inspect delivery status")

    try:
        while not stop.is_set():
            try:
                if bot.login is not None:
                    await bot.login.reap()
                updates = await bot.gateway.tg(
                    bot.gateway.bot_token,
                    "getUpdates",
                    offset=offset,
                    timeout=25,
                    limit=20,
                    allowed_updates=["message", "callback_query", "business_connection"],
                )
                mark_ready()
                for update in updates:
                    data = update.get("callback_query", {}).get("data", "")
                    if isinstance(data, str) and data.startswith("go:") and len(background) < 20:
                        task = asyncio.create_task(process_update(bot, update))
                        background.add(task)
                        task.add_done_callback(completed)
                    else:
                        await process_update(bot, update)
                    offset = update["update_id"] + 1
            except Rejected:
                log.warning("Polling unavailable; reconnecting in 3 seconds")
                try:
                    await asyncio.wait_for(stop.wait(), timeout=3)
                except asyncio.TimeoutError:
                    pass
    finally:
        pending = list(background)
        if not stop.is_set():
            for task in pending:
                task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)


async def run() -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    key = os.environ.get("RELAYPOST_ENCRYPTION_KEY", "")
    if not token or not key:
        raise Rejected("Задайте TELEGRAM_BOT_TOKEN и RELAYPOST_ENCRYPTION_KEY.")
    allowed = frozenset(
        int(x.strip())
        for x in os.environ.get("RELAYPOST_ALLOWED_USERS", "").split(",")
        if x.strip()
    )
    clear_readiness()
    store = Store(os.environ.get("RELAYPOST_DATABASE", "relaypost.db"), key)
    loop = asyncio.get_running_loop()
    task = asyncio.current_task()
    previous = signal.getsignal(signal.SIGTERM)
    if task is not None:
        loop.add_signal_handler(signal.SIGTERM, task.cancel)
    try:
        async with create_client(os.environ.get("TELEGRAM_PROXY_URL", "")) as client:
            users: Optional[TelegramUsers] = None
            login: Optional[UserLogin] = None
            runner: Optional[web.AppRunner] = None
            try:
                if os.environ.get("RELAYPOST_LOGIN_ORIGIN"):
                    users = TelegramUsers(
                        int(os.environ.get("TELEGRAM_API_ID", "0")),
                        os.environ.get("TELEGRAM_API_HASH", ""),
                        os.environ.get("TELEGRAM_PROXY_URL", ""),
                    )
                    login = UserLogin(store, users, os.environ["RELAYPOST_LOGIN_ORIGIN"])
                    runner = web.AppRunner(application(login), access_log=None)
                    await runner.setup()
                    await web.TCPSite(
                        runner, os.environ.get("RELAYPOST_LOGIN_BIND", "127.0.0.1"), 8080
                    ).start()
                gateway = Gateways(client, token, users)
                await gateway.tg(token, "getMe")
                await gateway.tg(token, "deleteWebhook", drop_pending_updates=False)
                await gateway.tg(
                    token,
                    "setMyCommands",
                    commands=[
                        {"command": "start", "description": "Помощь"},
                        {"command": "login", "description": "Подключить личный Telegram"},
                        {"command": "accounts", "description": "Мои аккаунты"},
                        {"command": "targets", "description": "Каналы и группы"},
                        {"command": "status", "description": "Результаты публикаций"},
                        {"command": "cancel", "description": "Отменить вход"},
                    ],
                )
                await poll(Bot(Service(store, gateway), allowed, login), asyncio.Event())
            finally:
                if runner is not None:
                    await runner.cleanup()
                if login is not None:
                    await login.close()
    finally:
        loop.remove_signal_handler(signal.SIGTERM)
        signal.signal(signal.SIGTERM, previous)
        clear_readiness()
        store.close()


def main() -> None:
    logging.basicConfig(level=logging.WARNING)
    # httpx INFO includes URLs containing bot tokens.
    logging.getLogger("httpx").setLevel(logging.CRITICAL)
    logging.getLogger("httpcore").setLevel(logging.CRITICAL)
    logging.getLogger("telethon").setLevel(logging.CRITICAL)
    try:
        asyncio.run(run())
    except (Rejected, ValueError):
        log.error("Startup failed; check configuration and platform access")
        raise SystemExit(1) from None
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
