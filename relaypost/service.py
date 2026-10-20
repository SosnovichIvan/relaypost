"""Publishing use cases and owner authorization."""

from typing import Any

import httpx

from relaypost.domain import Rejected, Uncertain, formats, validate_content
from relaypost.gateways import Gateways
from relaypost.store import Store


class Service:
    def __init__(self, store: Store, gateway: Gateways):
        self.store = store
        self.gateway = gateway

    async def connect(self, owner: int, platform: str, token: str) -> int:
        profile = await self.gateway.profile(platform, token)
        return self.store.save_account(owner, platform, profile, token)

    async def business(self, connection: dict[str, Any]) -> None:
        owner = connection["user"]["id"]
        profile = {"id": connection["id"], "label": connection["user"]["first_name"]}
        account_id = self.store.save_account(owner, "telegram-business", profile, connection["id"])
        if connection["is_enabled"] and connection.get("rights", {}).get("can_manage_stories"):
            self.store.add_target(
                owner,
                account_id,
                {"id": connection["id"], "kind": "business", "label": profile["label"]},
            )
        else:
            # Revocation deletes capabilities and stored connection, including targets.
            self.store.disconnect(owner, account_id)

    async def add(self, owner: int, account_id: int, external_id: str, story: bool = False) -> None:
        account = self.store.account(owner, account_id)
        target = (
            await self.gateway.target(account, external_id, story=True)
            if story and account["platform"] == "telegram-user"
            else await self.gateway.target(account, external_id)
        )
        if story:
            if account["platform"] == "telegram-user":
                self.store.add_target(owner, account_id, target)
                return
            if account["platform"] != "vk":
                raise Rejected("/addstory используется для историй ВК.")
            group = {"group_id": abs(int(external_id))} if int(external_id) < 0 else {}
            await self.gateway.vk(account["token"], "stories.getPhotoUploadServer", **group)
            target["kind"] = "story"
        self.store.add_target(owner, account_id, target)

    async def disconnect(self, owner: int, account_id: int) -> None:
        account = self.store.account(owner, account_id)
        if account["platform"] == "telegram-user":
            await self.gateway.personal().revoke(account)
        self.store.disconnect(owner, account_id, account["token"])

    async def discover(self, owner: int, account_id: int) -> int:
        account = self.store.account(owner, account_id)
        candidates = await self.gateway.discover(account)
        accepted = 0
        for external_id in candidates:
            try:
                await self.add(owner, account_id, external_id)
            except Rejected:
                continue
            accepted += 1
        return accepted

    def options(self, owner: int, content: dict[str, Any]) -> list[dict[str, Any]]:
        return [
            dict(target, mode=mode)
            for target in self.store.targets(owner)
            for mode in formats(target["platform"], target["kind"], content["media"])
        ]

    async def publish(self, owner: int, draft_id: str, target_id: int, mode: str) -> str:
        content = self.store.get_draft(owner, draft_id)
        options = self.options(owner, content)
        target = next(
            (item for item in options if item["id"] == target_id and item["mode"] == mode), None
        )
        if target is None:
            raise Rejected("Назначение недоступно для этого аккаунта или формата.")
        validate_content(content, target["platform"], mode)
        account = self.store.account(owner, target["account_id"])
        if account["platform"] == "telegram-user":
            await self.gateway.target(account, target["external_id"], story=mode == "story")
        else:
            await self.gateway.target(account, target["external_id"])
        if not self.store.claim_delivery(draft_id, target_id, mode):
            raise Rejected("Публикация уже обработана. Проверьте /status.")
        try:
            result = await self.gateway.publish(
                account, target, content, mode, f"{draft_id}-{target_id}"
            )
        except Uncertain as exc:
            self.store.finish(draft_id, target_id, "unknown", str(exc))
            raise
        except (KeyError, IndexError, TypeError, httpx.TransportError) as exc:
            self.store.finish(
                draft_id, target_id, "unknown", "Неполный ответ площадки; проверьте вручную."
            )
            raise Uncertain("Не удалось подтвердить доставку. Проверьте площадку вручную.") from exc
        except Rejected as exc:
            self.store.finish(draft_id, target_id, "failed", str(exc))
            raise
        self.store.finish(draft_id, target_id, "sent", result)
        return result
