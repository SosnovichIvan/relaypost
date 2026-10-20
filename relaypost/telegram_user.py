"""Personal Telegram adapter; session secrets never enter responses or logs."""

import asyncio
import hashlib
import io
import re
import weakref
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from urllib.parse import urlsplit

from telethon import TelegramClient, errors, functions, types, utils  # type: ignore[import-untyped]
from telethon.sessions import StringSession  # type: ignore[import-untyped]

from relaypost.domain import Rejected, Uncertain
from relaypost.network import validate_proxy


class TelegramUsers:
    def __init__(self, api_id: int, api_hash: str, proxy: str):
        url = urlsplit(validate_proxy(proxy))
        if api_id <= 0 or not re.fullmatch(r"[a-fA-F0-9]{32}", api_hash) or not url.hostname:
            raise Rejected("Configure Telegram API credentials and the closed proxy.")
        self.api_id, self.api_hash = api_id, api_hash
        self.locks: weakref.WeakValueDictionary[str, asyncio.Lock] = weakref.WeakValueDictionary()
        self.proxy = {"proxy_type": "http", "addr": url.hostname, "port": url.port, "rdns": True}

    def client(self, session: str = "", *, login: bool = False) -> Any:
        stored = StringSession(session)
        # Existing Telegram DCs use 443 for CONNECT through the private Squid.
        stored.set_dc(stored.dc_id or 2, stored.server_address or "149.154.167.51", 443)
        return TelegramClient(
            stored,
            self.api_id,
            self.api_hash,
            proxy=self.proxy,
            request_retries=0,
            connection_retries=0,
            auto_reconnect=False,
            flood_sleep_threshold=0,
            timeout=10,
            receive_updates=login,
            device_model="RelayPost",
            app_version="0.2.0",
        )

    def session_lock(self, session: str) -> asyncio.Lock:
        key = hashlib.sha256(session.encode()).hexdigest()
        lock = self.locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self.locks[key] = lock
        return lock

    @asynccontextmanager
    async def connected(self, account: dict[str, Any]) -> AsyncIterator[Any]:
        async with self.session_lock(account["token"]):
            client = self.client(account["token"])
            try:
                await asyncio.wait_for(client.connect(), 15)
                me = await asyncio.wait_for(client.get_me(), 15)
                if me is None or str(me.id) != account["external_id"]:
                    raise Rejected("Telegram-сессия недействительна. Подключите аккаунт снова.")
                yield client
            except errors.RPCError:
                raise Rejected(
                    "Telegram отклонил действие. Проверьте сессию, права и лимиты."
                ) from None
            except (OSError, asyncio.TimeoutError):
                raise Uncertain(
                    "Telegram не подтвердил действие. Проверьте результат вручную."
                ) from None
            finally:
                await client.disconnect()

    async def resolve(
        self, client: Any, external_id: str, *, story: bool = False
    ) -> dict[str, Any]:
        # StringSession retains auth keys, not the peer access-hash cache. Refill it
        # from a bounded dialog list before resolving numeric channel IDs.
        await asyncio.wait_for(client.get_dialogs(limit=100), 20)
        entity = await asyncio.wait_for(
            client.get_entity(
                int(external_id) if external_id.lstrip("-").isdigit() else external_id
            ),
            20,
        )
        if not isinstance(entity, (types.User, types.Channel, types.Chat)):
            raise Rejected("Неподдерживаемое назначение Telegram.")
        peer_id = str(utils.get_peer_id(entity))
        if isinstance(entity, types.User):
            me = await asyncio.wait_for(client.get_me(), 15)
            if not story or entity.id != me.id:
                raise Rejected("Выберите свой профиль для истории или канал/группу для поста.")
        else:
            rights = await asyncio.wait_for(client.get_permissions(entity, "me"), 20)
            if (story or getattr(entity, "broadcast", False)) and not rights.is_admin:
                raise Rejected("Нужны права администратора назначения.")
            if not story and not getattr(entity, "broadcast", False):
                bans = [
                    getattr(entity, "default_banned_rights", None),
                    getattr(getattr(rights, "participant", None), "banned_rights", None),
                ]
                if getattr(rights, "has_left", False) or any(
                    ban and (ban.view_messages or ban.send_messages) for ban in bans
                ):
                    raise Rejected("Нет права отправлять сообщения в эту группу.")
                if getattr(entity, "megagroup", False) and getattr(rights, "anonymous", False):
                    raise Rejected(
                        "Анонимный администратор не отправляет от личного имени. "
                        "Измените настройку группы."
                    )
            if (
                not story
                and getattr(entity, "broadcast", False)
                and not (
                    getattr(entity, "creator", False)
                    or getattr(getattr(entity, "admin_rights", None), "post_messages", False)
                )
            ):
                raise Rejected("Нет права публикации в канале.")
        if story:
            peer = await asyncio.wait_for(client.get_input_entity(entity), 20)
            await asyncio.wait_for(client(functions.stories.CanSendStoryRequest(peer=peer)), 20)
        return {
            "id": peer_id,
            "kind": "user-story"
            if story
            else ("channel" if getattr(entity, "broadcast", False) else "chat"),
            "label": getattr(entity, "title", None) or getattr(entity, "first_name", "Моя история"),
        }

    async def target(
        self, account: dict[str, Any], external_id: str, *, story: bool = False
    ) -> dict[str, Any]:
        async with self.connected(account) as client:
            return await self.resolve(client, external_id, story=story)

    async def discover(self, account: dict[str, Any]) -> list[str]:
        async with self.connected(account) as client:
            dialogs = await asyncio.wait_for(client.get_dialogs(limit=100), 20)
            return [
                str(utils.get_peer_id(d.entity))
                for d in dialogs
                if isinstance(d.entity, (types.Channel, types.Chat))
                and not getattr(d.entity, "left", False)
                and (
                    not getattr(d.entity, "broadcast", False)
                    or getattr(d.entity, "creator", False)
                    or getattr(d.entity, "admin_rights", None)
                )
            ]

    async def revoke(self, account: dict[str, Any]) -> None:
        async with self.session_lock(account["token"]):
            client = self.client(account["token"])
            try:
                await asyncio.wait_for(client.connect(), 15)
                me = await asyncio.wait_for(client.get_me(), 15)
                if me is not None and str(me.id) != account["external_id"]:
                    raise Rejected("Сессия не соответствует аккаунту.")
                if me is not None and not await asyncio.wait_for(client.log_out(), 15):
                    raise Rejected("Не удалось отозвать Telegram-сессию. Повторите отключение.")
            except errors.AuthKeyUnregisteredError:
                pass  # Telegram has already revoked this session.
            except (errors.RPCError, OSError, asyncio.TimeoutError):
                raise Rejected(
                    "Не удалось проверить отзыв сессии. Повторите отключение позже."
                ) from None
            finally:
                await client.disconnect()

    async def publish(
        self,
        account: dict[str, Any],
        target: dict[str, Any],
        content: dict[str, Any],
        mode: str,
        nonce: str,
        media: bytes,
    ) -> str:
        async with self.connected(account) as client:
            await self.resolve(client, target["external_id"], story=mode == "story")
            peer = await asyncio.wait_for(client.get_input_entity(int(target["external_id"])), 20)
            send_as = types.InputPeerSelf() if target["kind"] == "chat" else None
            random_id = int.from_bytes(
                hashlib.sha256(nonce.encode()).digest()[:8], "big", signed=True
            )
            if content["media"] == "text":
                request = functions.messages.SendMessageRequest(
                    peer, content["text"], random_id=random_id, send_as=send_as
                )
            else:
                file = io.BytesIO(media)
                file.name = "story.jpg" if content["media"] == "photo" else "video.mp4"
                uploaded = await asyncio.wait_for(client.upload_file(file), 60)
                attachment = (
                    types.InputMediaUploadedPhoto(uploaded)
                    if content["media"] == "photo"
                    else types.InputMediaUploadedDocument(
                        uploaded,
                        "video/mp4",
                        [
                            types.DocumentAttributeVideo(
                                duration=content["duration"],
                                w=content["width"],
                                h=content["height"],
                                supports_streaming=True,
                            )
                        ],
                    )
                )
                if mode == "story":
                    privacy = (
                        types.InputPrivacyValueAllowContacts()
                        if target["external_id"] == account["external_id"]
                        else types.InputPrivacyValueAllowAll()
                    )
                    request = functions.stories.SendStoryRequest(
                        peer,
                        attachment,
                        [privacy],
                        caption=content["text"],
                        random_id=random_id,
                        period=86400,
                    )
                else:
                    request = functions.messages.SendMediaRequest(
                        peer, attachment, content["text"], random_id=random_id, send_as=send_as
                    )
            try:
                result = await asyncio.wait_for(client(request), 60)
            except (errors.ServerError, errors.RpcCallFailError):
                raise Uncertain("Telegram не подтвердил отправку. Проверьте назначение.") from None
            for update in getattr(result, "updates", []):
                if (
                    isinstance(update, (types.UpdateMessageID, types.UpdateStoryID))
                    and update.random_id == random_id
                ):
                    return str(update.id)
                if isinstance(update, (types.UpdateNewMessage, types.UpdateNewChannelMessage)):
                    return str(update.message.id)
            if isinstance(result, types.UpdateShortSentMessage):
                return str(result.id)
            raise Uncertain("Telegram не вернул ID публикации. Проверьте назначение.")
