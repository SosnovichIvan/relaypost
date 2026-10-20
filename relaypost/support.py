"""Explicit private support intake and owner replies, without forwarding identities."""

from typing import Any

from relaypost.domain import Rejected, Uncertain
from relaypost.gateways import Gateways
from relaypost.support_store import SupportStore

UNAVAILABLE = "Техподдержка пока не настроена. Попробуйте /support позже."
PROMPT = (
    "Техподдержка RelayPost\n\nОтветьте на это сообщение и опишите проблему "
    "(до 3000 символов). Текст, имя и Telegram ID будут переданы команде поддержки "
    "для обратной связи. Не отправляйте пароли, коды входа и токены. Отмена: /cancel."
)


def support_recipient(value: str) -> int:
    if not value.isascii() or not value.isdecimal() or not 0 <= int(value) < 2**52:
        raise ValueError("SUPPORT_CHAT_ID must be 0 or a positive private Telegram user ID")
    return int(value)


class Support:
    def __init__(self, store: SupportStore, gateway: Gateways, recipient: int):
        self.store, self.gateway, self.recipient = store, gateway, recipient

    async def send(self, recipient: int, text: str, **payload: Any) -> Any:
        return await self.gateway.tg(
            self.gateway.bot_token,
            "sendMessage",
            chat_id=recipient,
            text=text,
            link_preview_options={"is_disabled": True},
            **payload,
        )

    async def deliver(self, identifier: str, recipient: int, text: str) -> None:
        try:
            result = await self.send(recipient, text)
            remote_id = result["message_id"]
            if type(remote_id) is not int or remote_id <= 0:
                raise ValueError("Invalid message ID")
        except (Uncertain, KeyError, TypeError, ValueError):
            self.store.finish(identifier, "unknown")
            raise Rejected(
                "Доставка не подтверждена. Не отправляйте сообщение повторно до проверки "
                f"поддержкой. Номер: {identifier}."
            ) from None
        except Rejected:
            self.store.finish(identifier, "failed")
            raise Rejected(
                f"Сообщение не доставлено. Попробуйте позже. Номер: {identifier}."
            ) from None
        self.store.finish(identifier, "sent", remote_id)

    async def handle(self, owner: int, message: dict[str, Any]) -> bool:
        text = message.get("text", "")
        parts = text.split(maxsplit=1)
        command, body = (parts + ["", ""])[:2]
        command = command.split("@")[0]
        if command in {"/cancel", "/start", "/help"}:
            self.store.cancel(owner)
        requested = command == "/support" or text == "/start support"
        parent = message.get("reply_to_message", {})
        valid_parent = parent.get("from", {}).get("id") == int(
            self.gateway.bot_token.split(":")[0]
        ) and not parent.get("forward_origin")
        parent_id = parent.get("message_id", 0) if valid_parent else 0
        card = (
            self.store.card(owner, parent_id) if owner == self.recipient and not requested else None
        )
        prompt_reply = self.store.is_prompt(owner, parent_id)
        if not requested and (text.startswith("/") or not (card or prompt_reply)):
            return False
        if not self.recipient:
            await self.send(owner, UNAVAILABLE)
            return True
        if requested and (command != "/support" or not body.strip()):
            result = await self.send(owner, PROMPT, reply_markup={"force_reply": True})
            self.store.prompt(owner, result["message_id"])
            return True
        body = body if requested else text
        if message.get("media_group_id") or not isinstance(body, str):
            raise Rejected("Поддержка принимает только текст, до 3000 символов.")
        if card:
            identifier = self.store.claim(
                owner, message["message_id"], card["owner"], body, card["id"]
            )
            await self.deliver(
                identifier,
                card["owner"],
                f"Ответ техподдержки RelayPost\nОбращение №{card['id']}\n\n{body}",
            )
            await self.send(owner, "Ответ доставлен пользователю.")
        else:
            identifier = self.store.claim(owner, message["message_id"], self.recipient, body)
            name = str(message.get("from", {}).get("first_name", "Пользователь"))[:100]
            await self.deliver(
                identifier,
                self.recipient,
                f"Техподдержка RelayPost\nОбращение №{identifier}\n"
                f"Пользователь: {name}\nID: {owner}\n\n{body}\n\n"
                "Ответьте на эту карточку текстом — бот доставит ответ пользователю.",
            )
            self.store.cancel(owner)
            await self.send(owner, f"Обращение №{identifier} доставлено в поддержку.")
        return True
