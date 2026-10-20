"""Private Telegram command dispatcher; tokens are deleted before validation."""

from typing import Any

from relaypost.domain import Rejected
from relaypost.service import Service

HELP = """RelayPost — публикации в Telegram, MAX и ВК.

1. /connect telegram| max| vk ТОКЕН — подключить отдельного бота/аккаунт.
Сообщение с токеном удаляется перед проверкой. Используйте выделенные токены:
сообщение проходит через Telegram. Не отправляйте пароли и коды входа.
2. /accounts — список подключений.
3. /add ID_АККАУНТА ID_КАНАЛА — проверить и добавить назначение.
4. Отправьте текст, фото или видео и выберите кнопку публикации.

/targets — назначения; /discover ID — найти до 20 площадок MAX/ВК;
/status — результаты;
/disconnect ID — удалить подключение; /cancel — помощь.

Истории Telegram: подключите RelayPost в настройках бизнес-аккаунта,
разрешив управление историями. Подключение появится автоматически.
MAX и ВК: тексты, фото, видео. /addstory ID_АККАУНТА ID_ВК — истории ВК.
Личные Telegram-сессии и MAX-аккаунты через код входа пока не поддержаны."""


class Bot:
    def __init__(self, service: Service, allowed_users: frozenset[int] = frozenset()):
        self.service = service
        self.gateway = service.gateway
        self.store = service.store
        self.allowed_users = allowed_users

    async def say(self, owner: int, text: str, **payload: Any) -> None:
        await self.gateway.tg(
            self.gateway.bot_token, "sendMessage", chat_id=owner, text=text[:4000], **payload
        )

    async def handle(self, update: dict[str, Any]) -> None:
        business = update.get("business_connection")
        if business is not None:
            if not self.allowed_users or business["user"]["id"] in self.allowed_users:
                if self.store.claim_update(update["update_id"]):
                    await self.service.business(business)
            return
        callback = update.get("callback_query")
        message = update.get("message")
        if callback is not None:
            message = callback.get("message")
            actor = callback["from"]
        elif message is not None:
            actor = message.get("from", {})
        else:
            return
        if not message or message.get("chat", {}).get("type") != "private" or actor.get("is_bot"):
            return
        owner = actor.get("id")
        if not isinstance(owner, int) or owner != message["chat"]["id"]:
            return
        if self.allowed_users and owner not in self.allowed_users:
            return
        if not self.store.claim_update(update["update_id"]):
            return
        try:
            if callback is not None:
                await self.gateway.tg(
                    self.gateway.bot_token, "answerCallbackQuery", callback_query_id=callback["id"]
                )
                parts = callback.get("data", "").split(":")
                if len(parts) != 4 or parts[0] != "pub":
                    raise Rejected("Неизвестная кнопка.")
                result = await self.service.publish(owner, parts[1], int(parts[2]), parts[3])
                await self.say(owner, f"Опубликовано. ID: {result}. Результаты: /status")
            else:
                await self.message(owner, message)
        except (Rejected, ValueError, IndexError, KeyError, TypeError) as exc:
            text = (
                str(exc)
                if isinstance(exc, Rejected)
                else "Неверный формат команды. /start — помощь."
            )
            await self.say(owner, text)

    async def message(self, owner: int, message: dict[str, Any]) -> None:
        text = message.get("text", "")
        if text.startswith("/"):
            parts = text.split()
            command = parts[0].split("@")[0]
            if command in {"/start", "/help", "/cancel", "/connect"} and len(parts) == 1:
                await self.say(owner, HELP)
            elif command == "/connect":
                # If deletion is rejected, do not persist or use the credential.
                await self.gateway.tg(
                    self.gateway.bot_token,
                    "deleteMessage",
                    chat_id=owner,
                    message_id=message["message_id"],
                )
                if len(parts) != 3:
                    raise Rejected("Формат: /connect telegram|max|vk ТОКЕН")
                account_id = await self.service.connect(owner, parts[1], parts[2])
                await self.say(
                    owner, f"Аккаунт #{account_id} подключён. /add {account_id} ID_КАНАЛА"
                )
            elif command == "/accounts":
                rows = self.store.accounts(owner)
                await self.say(
                    owner,
                    "\n".join(f"#{r['id']} · {r['platform']} · {r['label']}" for r in rows)
                    or "Подключений нет. /connect",
                )
            elif command in {"/add", "/addstory"} and len(parts) == 3:
                await self.service.add(owner, int(parts[1]), parts[2], story=command == "/addstory")
                await self.say(owner, "Назначение добавлено. Отправьте контент для публикации.")
            elif command == "/discover" and len(parts) == 2:
                count = await self.service.discover(owner, int(parts[1]))
                await self.say(
                    owner, f"Проверено и добавлено назначений: {count}. Список: /targets"
                )
            elif command == "/targets":
                rows = self.store.targets(owner)
                await self.say(
                    owner,
                    "\n".join(
                        f"#{r['id']} · {r['platform']} · {r['label']} ({r['kind']})" for r in rows
                    )
                    or "Назначений нет. /add",
                )
            elif command == "/disconnect" and len(parts) == 2:
                self.store.disconnect(owner, int(parts[1]))
                await self.say(owner, "Подключение и его назначения удалены.")
            elif command == "/status":
                rows = self.store.status(owner)
                await self.say(
                    owner,
                    "\n".join(
                        f"#{r['id']} · {r['label']} · {r['state']} · {r['result']}" for r in rows
                    )
                    or "Публикаций нет.",
                )
            else:
                raise Rejected("Неизвестная команда или неверные аргументы. /start — помощь.")
            return
        content = {"media": "text", "text": text, "file_id": ""}
        if message.get("media_group_id"):
            raise Rejected("Альбомы пока не поддержаны. Отправьте одно фото или видео.")
        if message.get("photo"):
            content = {
                "media": "photo",
                "text": message.get("caption", ""),
                "file_id": message["photo"][-1]["file_id"],
            }
        elif message.get("video"):
            if message["video"].get("file_size", 0) > 20_000_000:
                raise Rejected("Максимальный размер видео — 20 МБ.")
            content = {
                "media": "video",
                "text": message.get("caption", ""),
                "file_id": message["video"]["file_id"],
            }
        elif not text:
            raise Rejected("Отправьте текст, фото или видео.")
        options = self.service.options(owner, content)
        if not options:
            raise Rejected(
                "Нет назначений для этого формата. Подключите аккаунт и добавьте канал. /start"
            )
        draft_id = self.store.draft(owner, content)
        names = {"post": "Пост", "message": "Сообщение", "story": "История"}
        buttons = [
            [
                {
                    "text": f"{names[o['mode']]} · {o['platform']} · {o['label']}"[:64],
                    "callback_data": f"pub:{draft_id}:{o['id']}:{o['mode']}",
                }
            ]
            for o in options
        ]
        await self.say(
            owner,
            "Черновик готов. Нажатие кнопки подтверждает публикацию в выбранном месте. "
            "Для других мест нажмите соответствующие кнопки. Черновик действует 24 часа.",
            reply_markup={"inline_keyboard": buttons},
        )
