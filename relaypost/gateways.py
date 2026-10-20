"""Official APIs; provider payloads never become user-visible errors."""

import json
import re
from typing import Any, Optional

import httpx

from relaypost.domain import Rejected, Uncertain
from relaypost.telegram_user import TelegramUsers
from relaypost.uploads import upload


class Gateways:
    def __init__(
        self, client: httpx.AsyncClient, bot_token: str, users: Optional[TelegramUsers] = None
    ):
        self.client = client
        self.bot_token = bot_token
        self.users = users

    def personal(self) -> TelegramUsers:
        if self.users is None:
            raise Rejected("Вход в личный Telegram-аккаунт пока не настроен.")
        return self.users

    async def request(self, method: str, url: str, *, write: bool = False, **kwargs: Any) -> Any:
        try:
            response = await self.client.request(method, url, **kwargs)
            if response.status_code == 429:
                raise Rejected("Лимит площадки. Повторите позже новым черновиком.")
            if response.status_code >= 500 and write:
                raise Uncertain("Площадка не подтвердила доставку. Проверьте её вручную.")
            if response.is_error:
                raise Rejected("Площадка отклонила запрос. Проверьте доступ и права.")
            data = response.json()
        except (httpx.TransportError, ValueError) as exc:
            error = Uncertain if write else Rejected
            raise error("Не удалось подтвердить ответ площадки. Проверьте статус вручную.") from exc
        if isinstance(data, dict) and data.get("code") == "attachment.not.ready":
            raise Rejected("Медиа ещё обрабатывается MAX. Подождите и создайте новый черновик.")
        if isinstance(data, dict) and (
            data.get("ok") is False or "error" in data or "code" in data
        ):
            raise Rejected("Площадка отклонила запрос. Проверьте токен и права.")
        return data

    async def tg(self, token: str, method: str, **payload: Any) -> Any:
        if not re.fullmatch(r"\d+:[A-Za-z0-9_-]+", token):
            raise Rejected("Неверный формат Telegram-токена.")
        data = await self.request(
            "POST",
            f"https://api.telegram.org/bot{token}/{method}",
            json=payload,
            write=method.startswith(("send", "post", "delete")),
        )
        return data["result"]

    async def vk(self, token: str, method: str, **payload: Any) -> Any:
        data = await self.request(
            "POST",
            f"https://api.vk.com/method/{method}",
            data={**payload, "access_token": token, "v": "5.199"},
            write=method in {"wall.post", "video.save", "stories.save"},
        )
        return data["response"]

    async def profile(self, platform: str, token: str) -> dict[str, Any]:
        if platform == "telegram":
            data = await self.tg(token, "getMe")
            return {"id": data["id"], "label": data.get("username", str(data["id"]))}
        if platform == "max":
            data = await self.request(
                "GET", "https://platform-api2.max.ru/me", headers={"Authorization": token}
            )
            return {"id": data["user_id"], "label": data["name"]}
        if platform == "vk":
            data = (await self.vk(token, "users.get"))[0]
            return {"id": data["id"], "label": data["first_name"] + " " + data["last_name"]}
        raise Rejected("Поддерживаются telegram, max, vk.")

    async def target(
        self, account: dict[str, Any], external_id: str, *, story: bool = False
    ) -> dict[str, Any]:
        token, platform = account["token"], account["platform"]
        if platform not in {"telegram-user", "telegram-business", "telegram", "max", "vk"}:
            raise Rejected("Неподдерживаемый тип подключения.")
        if platform == "telegram-user":
            return await self.personal().target(account, external_id, story=story)
        if platform == "telegram-business":
            data = await self.tg(
                self.bot_token, "getBusinessConnection", business_connection_id=token
            )
            if not data["is_enabled"] or not data.get("rights", {}).get("can_manage_stories"):
                raise Rejected("Для историй разрешите управление историями бизнес-аккаунта.")
            return {"id": token, "kind": "business", "label": account["label"]}
        if platform == "telegram":
            chat = await self.tg(token, "getChat", chat_id=external_id)
            if chat["type"] not in {"channel", "group", "supergroup"}:
                raise Rejected("Выберите канал или группу.")
            member = await self.tg(
                token, "getChatMember", chat_id=chat["id"], user_id=int(account["external_id"])
            )
            if member["status"] not in {"administrator", "creator"}:
                raise Rejected("Добавьте подключённого бота администратором.")
            if chat["type"] == "channel" and not member.get("can_post_messages", False):
                raise Rejected("Боту нужно право публикации в канале.")
            return {
                "id": str(chat["id"]),
                "kind": "channel" if chat["type"] == "channel" else "chat",
                "label": chat.get("title", external_id),
            }
        if not re.fullmatch(r"-?\d+", external_id):
            raise Rejected("Для MAX/ВК нужен числовой ID.")
        if platform == "max":
            data = await self.request(
                "GET",
                f"https://platform-api2.max.ru/chats/{external_id}",
                headers={"Authorization": token},
            )
            me = await self.request(
                "GET",
                f"https://platform-api2.max.ru/chats/{external_id}/members/me",
                headers={"Authorization": token},
            )
            if not me.get("is_admin") or data["type"] not in {"channel", "chat"}:
                raise Rejected("Нужны права администратора MAX-чата или канала.")
            return {"id": external_id, "kind": data["type"], "label": data["title"]}
        if int(external_id) > 0:
            if external_id != account["external_id"]:
                raise Rejected("Личная стена должна принадлежать подключённому аккаунту.")
            return {"id": external_id, "kind": "wall", "label": account["label"]}
        data = await self.vk(
            token, "groups.getById", group_ids=external_id.lstrip("-"), fields="can_post,is_admin"
        )
        group = data["groups"][0]
        if not group.get("is_admin") or not group.get("can_post"):
            raise Rejected("Нужны права публикации и администратора сообщества ВК.")
        return {"id": external_id, "kind": "wall", "label": group["name"]}

    async def discover(self, account: dict[str, Any]) -> list[str]:
        if account["platform"] == "telegram-user":
            return await self.personal().discover(account)
        platform, token = account["platform"], account["token"]
        if platform == "max":
            data = await self.request(
                "GET",
                "https://platform-api2.max.ru/chats",
                params={"count": 20},
                headers={"Authorization": token},
            )
            return [str(chat["chat_id"]) for chat in data["chats"][:20]]
        if platform == "vk":
            data = await self.vk(token, "groups.get", filter="admin", extended=1, count=20)
            return [account["external_id"]] + [str(-group["id"]) for group in data["items"][:20]]
        raise Rejected(
            "Telegram Bot API не перечисляет каналы. Добавьте их через /add; "
            "бизнес-аккаунт для историй подключается автоматически."
        )

    async def media(self, file_id: str) -> bytes:
        data = await self.tg(self.bot_token, "getFile", file_id=file_id)
        path = data["file_path"]
        if not re.fullmatch(r"[a-zA-Z0-9_./-]+", path) or ".." in path:
            raise Rejected("Неверный путь медиафайла.")
        if data.get("file_size", 0) > 20_000_000:
            raise Rejected("Максимальный размер медиа — 20 МБ.")
        async with self.client.stream(
            "GET", f"https://api.telegram.org/file/bot{self.bot_token}/{path}"
        ) as response:
            if response.is_error:
                raise Rejected("Не удалось загрузить медиа из Telegram.")
            result = bytearray()
            async for chunk in response.aiter_bytes():
                result.extend(chunk)
                if len(result) > 20_000_000:
                    raise Rejected("Максимальный размер медиа — 20 МБ.")
            return bytes(result)

    async def publish(
        self,
        account: dict[str, Any],
        target: dict[str, Any],
        content: dict[str, Any],
        mode: str,
        delivery_key: str,
    ) -> str:
        token, platform = account["token"], account["platform"]
        if platform not in {"telegram-user", "telegram-business", "telegram", "max", "vk"}:
            raise Rejected("Неподдерживаемый тип подключения.")
        if platform == "telegram-user":
            media = await self.media(content["file_id"]) if content["media"] != "text" else b""
            return await self.personal().publish(
                account, target, content, mode, delivery_key, media
            )
        if platform in {"telegram", "telegram-business"}:
            if content["media"] == "text":
                data = await self.tg(
                    token, "sendMessage", chat_id=target["external_id"], text=content["text"]
                )
                return str(data["message_id"])
            binary = await self.media(content["file_id"])
            photo = content["media"] == "photo"
            field = "photo" if photo else "video"
            filename = "media.jpg" if photo else "media.mp4"
            payload = {"caption": content["text"]}
            if mode == "story":
                method = "postStory"
                token_for_request = self.bot_token
                payload.update(
                    business_connection_id=token,
                    active_period="86400",
                    content=json.dumps({"type": field, field: "attach://media"}),
                )
            else:
                method = "sendPhoto" if photo else "sendVideo"
                token_for_request = token
                payload.update(chat_id=target["external_id"], **{field: "attach://media"})
            data = await self.request(
                "POST",
                f"https://api.telegram.org/bot{token_for_request}/{method}",
                data=payload,
                files={"media": (filename, binary)},
                write=True,
            )
            return str(data["result"].get("id", data["result"].get("message_id", "")))
        if platform == "max":
            attachments = []
            if content["media"] != "text":
                photo = content["media"] == "photo"
                binary = await self.media(content["file_id"])
                media_type = "image" if photo else "video"
                server = await self.request(
                    "POST",
                    "https://platform-api2.max.ru/uploads",
                    params={"type": media_type},
                    headers={"Authorization": token},
                )
                uploaded = await upload(
                    self.client,
                    server["url"],
                    "max",
                    "data",
                    binary,
                    photo,
                    token=token if photo else "",
                    json_response=photo,
                )
                media_token = (
                    next(iter(uploaded["photos"].values()))["token"] if photo else server["token"]
                )
                attachments = [{"type": media_type, "payload": {"token": media_token}}]
            data = await self.request(
                "POST",
                "https://platform-api2.max.ru/messages",
                params={"chat_id": target["external_id"]},
                headers={"Authorization": token},
                json={"text": content["text"], "attachments": attachments},
                write=True,
            )
            return str(data["message"]["body"]["mid"])
        attachments_vk = ""
        if content["media"] != "text":
            binary = await self.media(content["file_id"])
            photo = content["media"] == "photo"
            group = (
                {"group_id": abs(int(target["external_id"]))}
                if int(target["external_id"]) < 0
                else {}
            )
            if mode == "story":
                method = "stories.getPhotoUploadServer" if photo else "stories.getVideoUploadServer"
                server = await self.vk(token, method, add_to_news=1, **group)
                uploaded = await upload(
                    self.client,
                    server["upload_url"],
                    "vk",
                    "file" if photo else "video_file",
                    binary,
                    photo,
                )
                result = await self.vk(
                    token, "stories.save", upload_results=uploaded["response"]["upload_result"]
                )
                return str(result["items"][0]["id"])
            if photo:
                server = await self.vk(token, "photos.getWallUploadServer", **group)
                uploaded = await upload(
                    self.client, server["upload_url"], "vk", "photo", binary, True
                )
                saved = (
                    await self.vk(
                        token,
                        "photos.saveWallPhoto",
                        **group,
                        photo=uploaded["photo"],
                        server=uploaded["server"],
                        hash=uploaded["hash"],
                    )
                )[0]
                attachments_vk = f"photo{saved['owner_id']}_{saved['id']}"
            else:
                saved = await self.vk(
                    token,
                    "video.save",
                    name=content["text"][:100] or "RelayPost video",
                    wallpost=0,
                    **group,
                )
                await upload(self.client, saved["upload_url"], "vk", "video_file", binary, False)
                attachments_vk = f"video{saved['owner_id']}_{saved['video_id']}"
        data = await self.vk(
            token,
            "wall.post",
            owner_id=target["external_id"],
            message=content["text"],
            guid=delivery_key,
            attachments=attachments_vk,
        )
        return str(data["post_id"])
