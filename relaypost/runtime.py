"""Single-process long polling with bounded network timeouts and no token logging."""

import asyncio
import logging
import os
import signal
from typing import Optional

import httpx

from relaypost.bot import Bot
from relaypost.domain import Rejected
from relaypost.gateways import Gateways
from relaypost.service import Service
from relaypost.store import Store

log = logging.getLogger("relaypost")


async def poll(bot: Bot, stop: asyncio.Event) -> None:
    offset: Optional[int] = None
    while not stop.is_set():
        try:
            updates = await bot.gateway.tg(
                bot.gateway.bot_token,
                "getUpdates",
                offset=offset,
                timeout=25,
                limit=20,
                allowed_updates=["message", "callback_query", "business_connection"],
            )
            for update in updates:
                try:
                    await bot.handle(update)
                except (Rejected, KeyError, TypeError):
                    # Do not log provider exceptions: their URLs can contain credentials.
                    log.warning("Update processing failed; inspect delivery status")
                offset = update["update_id"] + 1
        except Rejected:
            log.warning("Polling unavailable; reconnecting in 3 seconds")
            try:
                await asyncio.wait_for(stop.wait(), timeout=3)
            except asyncio.TimeoutError:
                pass


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
    store = Store(os.environ.get("RELAYPOST_DATABASE", "relaypost.db"), key)
    loop = asyncio.get_running_loop()
    task = asyncio.current_task()
    previous = signal.getsignal(signal.SIGTERM)
    if task is not None:
        loop.add_signal_handler(signal.SIGTERM, task.cancel)
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(40, connect=10), follow_redirects=False
        ) as client:
            gateway = Gateways(client, token)
            await gateway.tg(token, "getMe")
            await gateway.tg(token, "deleteWebhook", drop_pending_updates=False)
            await poll(Bot(Service(store, gateway), allowed), asyncio.Event())
    finally:
        loop.remove_signal_handler(signal.SIGTERM)
        signal.signal(signal.SIGTERM, previous)
        store.close()


def main() -> None:
    logging.basicConfig(level=logging.WARNING)
    # httpx INFO includes URLs containing bot tokens.
    logging.getLogger("httpx").setLevel(logging.CRITICAL)
    logging.getLogger("httpcore").setLevel(logging.CRITICAL)
    try:
        asyncio.run(run())
    except (Rejected, ValueError):
        log.error("Startup failed; check configuration and platform access")
        raise SystemExit(1) from None
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
