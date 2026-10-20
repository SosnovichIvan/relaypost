"""Private Telegram command dispatcher; tokens are deleted before validation."""

import asyncio
from typing import Any, Optional

from relaypost.domain import Rejected
from relaypost.selections import Selections, peer_key
from relaypost.service import Service
from relaypost.user_login import UserLogin

HELP = """RelayPost — публикации в Telegram, MAX и ВК.

1. /connect telegram| max| vk ТОКЕН — подключить отдельного бота/аккаунт.
Сообщение с токеном удаляется перед проверкой. Используйте выделенные токены:
сообщение проходит через Telegram. Не отправляйте пароли и коды входа.
2. /accounts — список подключений.
3. /add ID_АККАУНТА ID_КАНАЛА — проверить и добавить назначение.
4. Отправьте текст, фото или видео и выберите кнопку публикации.

/targets — назначения; /discover ID — найти площадки (Telegram: до 100 диалогов, MAX/ВК: до 20);
/status — результаты;
/disconnect ID — отозвать сессию/удалить подключение; /cancel — помощь.
/login — личный Telegram через HTTPS: QR или номер/код, затем 2FA при необходимости.
/addstory ID_АККАУНТА me — личные истории Telegram; вместо me можно указать канал.

Истории Telegram: подключите RelayPost в настройках бизнес-аккаунта,
разрешив управление историями. Подключение появится автоматически.
MAX и ВК: тексты, фото, видео. /addstory ID_АККАУНТА ID_ВК — истории ВК.
Личный Telegram подключается через /login; личные MAX-сессии пока не поддержаны."""


class Bot:
    def __init__(
        self,
        service: Service,
        allowed_users: frozenset[int] = frozenset(),
        login: Optional[UserLogin] = None,
    ):
        self.service = service
        self.gateway = service.gateway
        self.store = service.store
        self.allowed_users = allowed_users
        self.login = login
        self.selections = Selections(self.store.db)
        self.batch_lock = asyncio.Lock()

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
                if parts[0] in {"sel", "rev", "go", "page", "acct"}:
                    await self.multiple(owner, parts)
                    return
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
                if command == "/cancel" and self.login is not None:
                    await self.login.cancel(owner)
                await self.say(owner, HELP)
            elif command == "/login" and len(parts) == 1:
                if self.login is None:
                    raise Rejected("Вход в личный Telegram ещё не настроен. Попробуйте позже.")
                url = await self.login.issue(owner)
                await self.say(
                    owner,
                    "Откройте защищённую страницу. Подтвердите вход в Telegram "
                    "и сохранение аккаунта. "
                    "Сессия даёт широкий доступ к аккаунту. Не присылайте коды и пароль 2FA в чат.",
                    link_preview_options={"is_disabled": True},
                    reply_markup={
                        "inline_keyboard": [[{"text": "Подключить личный Telegram", "url": url}]]
                    },
                )
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
                await self.service.disconnect(owner, int(parts[1]))
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
                "duration": message["video"].get("duration", 0),
                "width": message["video"].get("width", 0),
                "height": message["video"].get("height", 0),
            }
        elif not text:
            raise Rejected("Отправьте текст, фото или видео.")
        options = self.service.options(owner, content)
        if not options:
            raise Rejected(
                "Нет назначений для этого формата. Подключите аккаунт и добавьте канал. /start"
            )
        draft_id = self.store.draft(owner, content)
        await self.destinations(owner, draft_id, 0)

    async def destinations(self, owner: int, draft_id: str, page: int) -> None:
        content = self.store.get_draft(owner, draft_id)
        options = self.service.options(owner, content)
        groups: dict[str, list[dict[str, Any]]] = {}
        for option in options:
            groups.setdefault(peer_key(option), []).append(option)
        destinations = list(groups.values())
        if page < 0 or page * 20 >= len(destinations):
            raise Rejected("Страница назначений недоступна.")
        buttons = []
        for group in destinations[page * 20 : (page + 1) * 20]:
            option = group[0]
            if len(group) == 1:
                buttons.append(self.destination_buttons(draft_id, option))
            else:
                buttons.append(
                    [
                        {
                            "text": f"{option['label'][:40]} · выбрать аккаунт ({len(group)})",
                            "callback_data": f"acct:{draft_id}:{option['id']}:{option['mode']}:0",
                        }
                    ]
                )
        if page > 0:
            buttons.append([{"text": "Предыдущие", "callback_data": f"page:{draft_id}:{page - 1}"}])
        if (page + 1) * 20 < len(destinations):
            buttons.append([{"text": "Следующие", "callback_data": f"page:{draft_id}:{page + 1}"}])
        buttons.append(
            [{"text": "Проверить выбранные назначения", "callback_data": f"rev:{draft_id}"}]
        )
        await self.say(
            owner,
            "Черновик готов. Нажатие кнопки подтверждает публикацию в выбранном месте. "
            "Либо выберите несколько мест в пакет и проверьте выбор. "
            "Личные истории: контакты, 24 часа; истории каналов — аудитория канала. "
            "Черновик действует 24 часа.",
            reply_markup={"inline_keyboard": buttons},
        )

    def destination_buttons(self, draft: str, option: dict[str, Any]) -> list[dict[str, str]]:
        names = {"post": "Пост", "message": "Сообщение", "story": "История"}
        return [
            {
                "text": f"{names[option['mode']]} · #{option['account_id']} · {option['label']}"[
                    :64
                ],
                "callback_data": f"pub:{draft}:{option['id']}:{option['mode']}",
            },
            {
                "text": "Выбрать в пакет",
                "callback_data": f"sel:{draft}:{option['id']}:{option['mode']}",
            },
        ]

    async def account_choices(
        self, owner: int, draft: str, target_id: int, mode: str, page: int
    ) -> None:
        options = self.service.options(owner, self.store.get_draft(owner, draft))
        option = next((o for o in options if o["id"] == target_id and o["mode"] == mode), None)
        if option is None:
            raise Rejected("Назначение недоступно.")
        aliases = [o for o in options if peer_key(o) == peer_key(option)]
        if page < 0 or page * 20 >= len(aliases):
            raise Rejected("Страница аккаунтов недоступна.")
        buttons = [
            self.destination_buttons(draft, alias) for alias in aliases[page * 20 : (page + 1) * 20]
        ]
        if page > 0:
            buttons.append(
                [
                    {
                        "text": "Предыдущие аккаунты",
                        "callback_data": f"acct:{draft}:{target_id}:{mode}:{page - 1}",
                    }
                ]
            )
        if (page + 1) * 20 < len(aliases):
            buttons.append(
                [
                    {
                        "text": "Следующие аккаунты",
                        "callback_data": f"acct:{draft}:{target_id}:{mode}:{page + 1}",
                    }
                ]
            )
        buttons.append([{"text": "К назначениям", "callback_data": f"page:{draft}:0"}])
        await self.say(
            owner,
            f"{option['label']}: выберите аккаунт для отправки. "
            "Кнопка «Пост» отправляет сразу, «Выбрать в пакет» — добавляет в общий выбор.",
            reply_markup={"inline_keyboard": buttons},
        )

    async def multiple(self, owner: int, parts: list[str]) -> None:
        draft = parts[1]
        content = self.store.get_draft(owner, draft)
        options = self.service.options(owner, content)
        if parts[0] == "acct" and len(parts) == 5:
            await self.account_choices(owner, draft, int(parts[2]), parts[3], int(parts[4]))
            return
        if parts[0] == "page" and len(parts) == 3:
            await self.destinations(owner, draft, int(parts[2]))
            return
        if parts[0] == "sel" and len(parts) == 4:
            option = next(
                (o for o in options if o["id"] == int(parts[2]) and o["mode"] == parts[3]), None
            )
            if option is None:
                raise Rejected("Назначение недоступно.")
            self.selections.toggle(draft, option, options)
        if parts[0] in {"sel", "rev"}:
            chosen = self.selections.selected(draft)
            if not chosen:
                await self.say(
                    owner, "Сначала выберите хотя бы одно назначение. Текущий выбор пуст."
                )
                return
            fingerprint = self.selections.review(draft)
            labels = [
                f"{o['label'][:60]} · аккаунт #{o['account_id']} · {o['mode']}"
                for r in chosen
                for o in options
                if r["target"] == o["id"] and r["mode"] == o["mode"]
            ]
            await self.say(
                owner,
                "Выбраны:\n"
                + "\n".join(labels)
                + f"\n\nКонтент ({content['media']}): "
                + content["text"][:1000]
                + (
                    "\nЛичные истории: контакты, 24 часа. Каналы — аудитория канала."
                    if any(r["mode"] == "story" for r in chosen)
                    else ""
                )
                + "\nПодтвердите публикацию во всех выбранных местах. "
                "Предыдущие успешные отправки не повторяются.",
                reply_markup={
                    "inline_keyboard": [
                        [
                            {
                                "text": "Подтвердить отправку",
                                "callback_data": f"go:{draft}:{fingerprint}",
                            }
                        ]
                    ]
                },
            )
        elif parts[0] == "go" and len(parts) == 3:
            await self.batch(owner, draft, parts[2])
        else:
            raise Rejected("Неизвестная кнопка пакета.")

    async def batch(self, owner: int, draft: str, fingerprint: str) -> None:
        if self.batch_lock.locked():
            raise Rejected("Другой пакет ещё отправляется. Повторите подтверждение позже.")
        async with self.batch_lock:
            selected = self.selections.freeze(draft, fingerprint)
            results = []
            for row in selected:
                try:
                    result = await self.service.publish(owner, draft, row["target"], row["mode"])
                    results.append(f"#{row['target']}: отправлено, ID {result}")
                except Rejected as exc:
                    results.append(f"#{row['target']}: {exc}")
            await self.say(owner, "\n".join(results) + "\nРезультаты: /status")
