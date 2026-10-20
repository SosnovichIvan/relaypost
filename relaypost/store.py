"""SQLite persistence; all queries scoped by owner, short transactions only."""

import json
import secrets
import sqlite3
import time
from typing import Any, Optional

from cryptography.fernet import Fernet

from relaypost.domain import Rejected
from relaypost.selections import SCHEMA as SELECTION_SCHEMA

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

USER_SCHEMA = """
CREATE TABLE IF NOT EXISTS personal_sessions (
 account_id INTEGER PRIMARY KEY REFERENCES accounts(id) ON DELETE CASCADE,
 secret BLOB NOT NULL CHECK(typeof(secret)='blob')
);
CREATE TRIGGER IF NOT EXISTS personal_session_delete_guard BEFORE DELETE ON accounts
WHEN EXISTS(SELECT 1 FROM personal_sessions WHERE account_id=OLD.id)
BEGIN SELECT RAISE(ABORT,'Revoke the personal session using the new application first'); END;
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
        self.db.executescript("BEGIN;" + SELECTION_SCHEMA + USER_SCHEMA + "COMMIT;")
        with self.db:
            self.db.execute(
                "INSERT OR IGNORE INTO peer_deliveries(draft,peer) "
                "SELECT d.draft_id,(CASE WHEN a.platform IN ('telegram','telegram-user') "
                "THEN 'telegram' ELSE a.platform END)||':'||t.external_id||':'||d.mode "
                "FROM deliveries d JOIN targets t ON t.id=d.target_id "
                "JOIN accounts a ON a.id=t.account_id"
            )

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
        credential = result.pop("secret")
        personal = self.db.execute(
            "SELECT secret FROM personal_sessions WHERE account_id=?", (account_id,)
        ).fetchone()
        if personal is not None:
            result["platform"], credential = "telegram-user", personal[0]
        result["token"] = self.vault.decrypt(credential).decode()
        return result

    def accounts(self, owner: int) -> list[dict[str, Any]]:
        return [
            dict(row)
            for row in self.db.execute(
                "SELECT a.id,CASE WHEN s.account_id IS NOT NULL THEN 'telegram-user' "
                "ELSE a.platform END AS platform,a.label FROM accounts a "
                "LEFT JOIN personal_sessions s ON s.account_id=a.id "
                "WHERE a.owner=? ORDER BY a.id LIMIT 100",
                (owner,),
            )
        ]

    def identity_account(
        self, owner: int, platform: str, external_id: str
    ) -> Optional[dict[str, Any]]:
        row = self.db.execute(
            "SELECT a.id FROM accounts a LEFT JOIN personal_sessions s ON s.account_id=a.id "
            "WHERE a.owner=? AND a.platform=? AND a.external_id=? "
            "AND ((?=1 AND s.account_id IS NOT NULL) OR (?=0 AND s.account_id IS NULL))",
            (
                owner,
                "telegram" if platform == "telegram-user" else platform,
                external_id,
                platform == "telegram-user",
                platform == "telegram-user",
            ),
        ).fetchone()
        return self.account(owner, row[0]) if row is not None else None

    def save_account(self, owner: int, platform: str, profile: dict[str, Any], token: str) -> int:
        legacy_platform = "telegram" if platform == "telegram-user" else platform
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            existing = self.db.execute(
                "SELECT s.account_id FROM accounts a "
                "LEFT JOIN personal_sessions s ON s.account_id=a.id "
                "WHERE a.owner=? AND a.platform=? AND a.external_id=?",
                (owner, legacy_platform, str(profile["id"])),
            ).fetchone()
            if existing is not None and (existing[0] is not None) != (platform == "telegram-user"):
                raise Rejected("Нельзя заменить личную сессию токеном бота или наоборот.")
            self.db.execute(
                "INSERT INTO accounts(owner,platform,external_id,label,secret) VALUES(?,?,?,?,?) "
                "ON CONFLICT(owner,platform,external_id) DO UPDATE SET label=excluded.label,"
                "secret=excluded.secret",
                (
                    owner,
                    legacy_platform,
                    str(profile["id"]),
                    profile["label"],
                    # Legacy code sees an invalid empty Bot API credential, never a user session.
                    self.vault.encrypt(b"" if platform == "telegram-user" else token.encode()),
                ),
            )
            account_id = int(
                self.db.execute(
                    "SELECT id FROM accounts WHERE owner=? AND platform=? AND external_id=?",
                    (owner, legacy_platform, str(profile["id"])),
                ).fetchone()[0]
            )
            if platform == "telegram-user":
                self.db.execute(
                    "INSERT INTO personal_sessions VALUES(?,?) ON CONFLICT(account_id) "
                    "DO UPDATE SET secret=excluded.secret",
                    (account_id, self.vault.encrypt(token.encode())),
                )
        return account_id

    def disconnect(self, owner: int, account_id: int, expected_token: Optional[str] = None) -> None:
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            account = self.account(owner, account_id)
            if expected_token is not None and account["token"] != expected_token:
                raise Rejected("Подключение изменилось во время отзыва. Повторите отключение.")
            self.db.execute("DELETE FROM personal_sessions WHERE account_id=?", (account_id,))
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
                "SELECT t.*, CASE WHEN s.account_id IS NOT NULL THEN 'telegram-user' "
                "ELSE a.platform END AS platform FROM targets t "
                "JOIN accounts a ON a.id=t.account_id "
                "LEFT JOIN personal_sessions s ON s.account_id=a.id "
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
            self.db.execute("BEGIN IMMEDIATE")
            target = self.db.execute(
                "SELECT t.external_id,a.platform FROM targets t "
                "JOIN accounts a ON a.id=t.account_id WHERE t.id=?",
                (target_id,),
            ).fetchone()
            if target is None:
                raise Rejected("Назначение было удалено. Создайте новый черновик.")
            namespace = (
                "telegram"
                if target["platform"] in {"telegram", "telegram-user"}
                else target["platform"]
            )
            peer = f"{namespace}:{target['external_id']}:{mode}"
            if self.db.execute(
                "SELECT 1 FROM peer_deliveries WHERE draft=? AND peer=?", (draft_id, peer)
            ).fetchone():
                return False
            claimed = (
                self.db.execute(
                    "INSERT OR IGNORE INTO deliveries(draft_id,target_id,mode,state) "
                    "VALUES(?,?,?,'sending')",
                    (draft_id, target_id, mode),
                ).rowcount
                == 1
            )
            if claimed:
                self.db.execute("INSERT INTO peer_deliveries VALUES(?,?)", (draft_id, peer))
            return claimed

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
