"""SQLite persistence; all queries scoped by owner, short transactions only."""

import json
import secrets
import sqlite3
import time
from typing import Any

from cryptography.fernet import Fernet

from relaypost.domain import Rejected

SCHEMA = """
CREATE TABLE accounts (
 id INTEGER PRIMARY KEY, owner INTEGER NOT NULL, platform TEXT NOT NULL,
 external_id TEXT NOT NULL, label TEXT NOT NULL, secret BLOB NOT NULL,
 UNIQUE(owner, platform, external_id)
);
CREATE TABLE targets (
 id INTEGER PRIMARY KEY, account_id INTEGER NOT NULL REFERENCES accounts ON DELETE CASCADE,
 external_id TEXT NOT NULL, kind TEXT NOT NULL, label TEXT NOT NULL,
 UNIQUE(account_id, external_id, kind)
);
CREATE TABLE drafts (
 id TEXT PRIMARY KEY, owner INTEGER NOT NULL, content TEXT NOT NULL,
 created REAL NOT NULL
);
CREATE TABLE deliveries (
 id INTEGER PRIMARY KEY, draft_id TEXT NOT NULL REFERENCES drafts ON DELETE CASCADE,
 target_id INTEGER NOT NULL REFERENCES targets ON DELETE CASCADE,
 mode TEXT NOT NULL, state TEXT NOT NULL CHECK(state IN ('sending','sent','failed','unknown')),
 result TEXT NOT NULL DEFAULT '', UNIQUE(draft_id, target_id)
);
CREATE TABLE updates (id INTEGER PRIMARY KEY, created REAL NOT NULL);
PRAGMA user_version=1;
"""


class Store:
    def __init__(self, path: str, key: str):
        self.vault = Fernet(key.encode())
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA busy_timeout=5000")
        version = self.db.execute("PRAGMA user_version").fetchone()[0]
        if version == 0:
            self.db.executescript("BEGIN;" + SCHEMA + "COMMIT;")
        elif version != 1:
            self.db.close()
            raise Rejected("Неподдерживаемая версия БД.")
        with self.db:
            self.db.execute("UPDATE deliveries SET state='unknown' WHERE state='sending'")

    def close(self) -> None:
        self.db.close()

    def claim_update(self, update_id: int) -> bool:
        with self.db:
            self.db.execute("DELETE FROM updates WHERE created < ?", (time.time() - 7 * 86400,))
            return (
                self.db.execute(
                    "INSERT OR IGNORE INTO updates VALUES (?, ?)", (update_id, time.time())
                ).rowcount
                == 1
            )

    def account(self, owner: int, account_id: int) -> dict[str, Any]:
        row = self.db.execute(
            "SELECT * FROM accounts WHERE id=? AND owner=?", (account_id, owner)
        ).fetchone()
        if row is None:
            raise Rejected("Аккаунт не найден.")
        result = dict(row)
        result["token"] = self.vault.decrypt(result.pop("secret")).decode()
        return result

    def accounts(self, owner: int) -> list[dict[str, Any]]:
        return [
            dict(row)
            for row in self.db.execute(
                "SELECT id,platform,label FROM accounts WHERE owner=? ORDER BY id LIMIT 100",
                (owner,),
            )
        ]

    def save_account(self, owner: int, platform: str, profile: dict[str, Any], token: str) -> int:
        with self.db:
            self.db.execute(
                "INSERT INTO accounts(owner,platform,external_id,label,secret) VALUES(?,?,?,?,?) "
                "ON CONFLICT(owner,platform,external_id) DO UPDATE SET label=excluded.label,"
                "secret=excluded.secret",
                (
                    owner,
                    platform,
                    str(profile["id"]),
                    profile["label"],
                    self.vault.encrypt(token.encode()),
                ),
            )
        return int(
            self.db.execute(
                "SELECT id FROM accounts WHERE owner=? AND platform=? AND external_id=?",
                (owner, platform, str(profile["id"])),
            ).fetchone()[0]
        )

    def disconnect(self, owner: int, account_id: int) -> None:
        self.account(owner, account_id)
        with self.db:
            self.db.execute("DELETE FROM accounts WHERE owner=? AND id=?", (owner, account_id))

    def add_target(self, owner: int, account_id: int, target: dict[str, Any]) -> None:
        self.account(owner, account_id)
        with self.db:
            self.db.execute(
                "INSERT INTO targets(account_id,external_id,kind,label) VALUES(?,?,?,?) "
                "ON CONFLICT(account_id,external_id,kind) DO UPDATE SET label=excluded.label",
                (account_id, str(target["id"]), target["kind"], target["label"]),
            )

    def targets(self, owner: int) -> list[dict[str, Any]]:
        return [
            dict(row)
            for row in self.db.execute(
                "SELECT t.*, a.platform FROM targets t JOIN accounts a ON a.id=t.account_id "
                "WHERE a.owner=? ORDER BY t.id LIMIT 100",
                (owner,),
            )
        ]

    def draft(self, owner: int, content: dict[str, Any]) -> str:
        draft_id = secrets.token_hex(8)
        with self.db:
            self.db.execute("DELETE FROM drafts WHERE created < ?", (time.time() - 86400,))
            self.db.execute(
                "INSERT INTO drafts VALUES(?,?,?,?)",
                (draft_id, owner, json.dumps(content), time.time()),
            )
        return draft_id

    def get_draft(self, owner: int, draft_id: str) -> dict[str, Any]:
        row = self.db.execute(
            "SELECT content FROM drafts WHERE id=? AND owner=? AND created>=?",
            (draft_id, owner, time.time() - 86400),
        ).fetchone()
        if row is None:
            raise Rejected("Черновик не найден или истёк. Отправьте контент снова.")
        return dict(json.loads(row[0]))

    def claim_delivery(self, draft_id: str, target_id: int, mode: str) -> bool:
        with self.db:
            return (
                self.db.execute(
                    "INSERT OR IGNORE INTO deliveries(draft_id,target_id,mode,state) "
                    "VALUES(?,?,?,'sending')",
                    (draft_id, target_id, mode),
                ).rowcount
                == 1
            )

    def finish(self, draft_id: str, target_id: int, state: str, result: str) -> None:
        with self.db:
            self.db.execute(
                "UPDATE deliveries SET state=?,result=? WHERE draft_id=? AND target_id=?",
                (state, result, draft_id, target_id),
            )

    def status(self, owner: int) -> list[dict[str, Any]]:
        return [
            dict(row)
            for row in self.db.execute(
                "SELECT d.id,d.state,d.result,t.label FROM deliveries d "
                "JOIN drafts f ON f.id=d.draft_id JOIN targets t ON t.id=d.target_id "
                "WHERE f.owner=? ORDER BY d.id DESC LIMIT 20",
                (owner,),
            )
        ]
