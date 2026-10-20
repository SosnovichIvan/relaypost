"""Owner-scoped destination selection and immutable confirmation of a batch."""

import hashlib
import json
import sqlite3
from typing import Any

from relaypost.domain import Rejected

SCHEMA = """
CREATE TABLE IF NOT EXISTS destination_selections (
 draft TEXT NOT NULL REFERENCES drafts(id) ON DELETE CASCADE,
 target INTEGER NOT NULL REFERENCES targets(id) ON DELETE CASCADE,
 mode TEXT NOT NULL, PRIMARY KEY(draft,target)
);
CREATE TABLE IF NOT EXISTS publication_batches (
 draft TEXT PRIMARY KEY REFERENCES drafts(id) ON DELETE CASCADE,
 fingerprint TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS peer_deliveries (
 draft TEXT NOT NULL REFERENCES drafts(id) ON DELETE CASCADE,
 peer TEXT NOT NULL, PRIMARY KEY(draft,peer)
);
"""


def peer_key(option: dict[str, Any]) -> str:
    platform = option["platform"]
    namespace = "telegram" if platform in {"telegram", "telegram-user"} else platform
    return f"{namespace}:{option['external_id']}:{option['mode']}"


class Selections:
    def __init__(self, db: sqlite3.Connection):
        self.db = db

    def selected(self, draft: str) -> list[dict[str, Any]]:
        return [
            dict(r)
            for r in self.db.execute(
                "SELECT target,mode FROM destination_selections WHERE draft=? ORDER BY target",
                (draft,),
            )
        ]

    def toggle(self, draft: str, option: dict[str, Any], options: list[dict[str, Any]]) -> None:
        with self.db:
            if self.db.execute(
                "SELECT 1 FROM publication_batches WHERE draft=?", (draft,)
            ).fetchone():
                raise Rejected("Пакет уже подтверждён. Создайте новый черновик.")
            chosen = self.selected(draft)
            if any(r["target"] == option["id"] for r in chosen):
                self.db.execute(
                    "DELETE FROM destination_selections WHERE draft=? AND target=?",
                    (draft, option["id"]),
                )
            else:
                # Picking another account for the same peer replaces the previous account.
                aliases = [o["id"] for o in options if peer_key(o) == peer_key(option)]
                for alias in aliases:
                    self.db.execute(
                        "DELETE FROM destination_selections WHERE draft=? AND target=?",
                        (draft, alias),
                    )
                if len(self.selected(draft)) >= 20:
                    raise Rejected("В одном пакете можно выбрать до 20 назначений.")
                self.db.execute(
                    "INSERT INTO destination_selections VALUES(?,?,?)",
                    (draft, option["id"], option["mode"]),
                )

    def review(self, draft: str) -> str:
        selected = self.selected(draft)
        if not selected:
            raise Rejected("Сначала выберите хотя бы одно назначение.")
        return hashlib.sha256(json.dumps(selected, sort_keys=True).encode()).hexdigest()[:16]

    def freeze(self, draft: str, fingerprint: str) -> list[dict[str, Any]]:
        with self.db:
            if fingerprint != self.review(draft):
                raise Rejected("Выбор изменился. Откройте подтверждение заново.")
            if (
                self.db.execute(
                    "INSERT OR IGNORE INTO publication_batches VALUES(?,?)", (draft, fingerprint)
                ).rowcount
                != 1
            ):
                raise Rejected("Пакет уже обработан. Проверьте /status.")
            return self.selected(draft)
