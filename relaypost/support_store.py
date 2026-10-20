"""Support data is separate from publication history; effects are claimed before I/O."""

import secrets
import sqlite3
import time
from typing import Any, Optional

from relaypost.domain import Rejected

SCHEMA_V2 = """
CREATE TABLE support_prompts (
 owner INTEGER PRIMARY KEY CHECK(owner>0), message_id INTEGER NOT NULL CHECK(message_id>0),
 created REAL NOT NULL
);
CREATE TABLE support_messages (
 id TEXT PRIMARY KEY, owner INTEGER NOT NULL CHECK(owner>0),
 source_id INTEGER NOT NULL CHECK(source_id>0), recipient INTEGER NOT NULL CHECK(recipient>0),
 ticket TEXT REFERENCES support_messages(id),
 body TEXT NOT NULL CHECK(length(body) BETWEEN 1 AND 3000),
 created REAL NOT NULL, state TEXT NOT NULL CHECK(state IN ('sending','sent','failed','unknown')),
 remote_id INTEGER, UNIQUE(owner,source_id), UNIQUE(recipient,remote_id)
);
CREATE INDEX support_owner_created ON support_messages(owner,created);
PRAGMA user_version=2;
"""


class SupportStore:
    def __init__(self, db: sqlite3.Connection):
        self.db = db

    def prompt(self, owner: int, message_id: int) -> None:
        with self.db:
            self.db.execute(
                "INSERT INTO support_prompts VALUES(?,?,?) ON CONFLICT(owner) "
                "DO UPDATE SET message_id=excluded.message_id,created=excluded.created",
                (owner, message_id, time.time()),
            )

    def cancel(self, owner: int) -> None:
        with self.db:
            self.db.execute("DELETE FROM support_prompts WHERE owner=?", (owner,))

    def is_prompt(self, owner: int, message_id: int) -> bool:
        return (
            self.db.execute(
                "SELECT 1 FROM support_prompts WHERE owner=? AND message_id=? AND created>?",
                (owner, message_id, time.time() - 86400),
            ).fetchone()
            is not None
        )

    def card(self, recipient: int, message_id: int) -> Optional[dict[str, Any]]:
        row = self.db.execute(
            "SELECT * FROM support_messages WHERE recipient=? AND remote_id=? "
            "AND ticket IS NULL AND state='sent'",
            (recipient, message_id),
        ).fetchone()
        return dict(row) if row is not None else None

    def claim(
        self, owner: int, source_id: int, recipient: int, body: str, ticket: Optional[str] = None
    ) -> str:
        if not body.strip() or len(body.encode("utf-16-le")) // 2 > 3000:
            raise Rejected("Нужен текст от 1 до 3000 символов. Фото и файлы пока не принимаются.")
        identifier = secrets.token_hex(8)
        with self.db:
            # BEGIN IMMEDIATE serializes replay and rate-limit checks across connections.
            self.db.execute("BEGIN IMMEDIATE")
            if self.db.execute(
                "SELECT 1 FROM support_messages WHERE owner=? AND source_id=?", (owner, source_id)
            ).fetchone():
                raise Rejected("Это сообщение уже принято. Повторно оно не отправляется.")
            if (
                ticket is None
                and self.db.execute(
                    "SELECT 1 FROM support_messages WHERE owner=? AND created>? LIMIT 1",
                    (owner, time.time() - 60),
                ).fetchone()
            ):
                raise Rejected("Не более одного обращения в минуту. Попробуйте позже.")
            self.db.execute(
                "INSERT INTO support_messages "
                "(id,owner,source_id,recipient,ticket,body,created,state) "
                "VALUES(?,?,?,?,?,?,?,'sending')",
                (identifier, owner, source_id, recipient, ticket, body, time.time()),
            )
        return identifier

    def finish(self, identifier: str, state: str, remote_id: Optional[int] = None) -> None:
        with self.db:
            self.db.execute(
                "UPDATE support_messages SET state=?,remote_id=? WHERE id=? AND state='sending'",
                (state, remote_id, identifier),
            )
