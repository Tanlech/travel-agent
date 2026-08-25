"""账号存取：MySQL 实现（SQLAlchemy Core + pymysql）+ 测试用内存实现"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol

from sqlalchemy import text

from app.admin.account.schema import UserAccount
from app.infrastructure.mysql_client import get_engine
from app.infrastructure.settings import settings

logger = logging.getLogger(__name__)


class AccountStore(Protocol):
    """账号读写接口（可注入替换，测试用 InMemoryAccountStore）"""

    def save(self, account: UserAccount) -> None: ...
    def load_by_id(self, user_id: str) -> UserAccount | None: ...
    def load_by_username(self, username: str) -> UserAccount | None: ...
    def delete(self, user_id: str) -> None: ...
    def list_accounts(self) -> list[UserAccount]: ...
    def save_token(self, token: str, user_id: str) -> None: ...
    def load_token_user(self, token: str) -> str | None: ...
    def delete_token(self, token: str) -> None: ...
    def delete_tokens_for_user(self, user_id: str) -> None: ...


def _to_naive_utc(iso: str | None) -> datetime | None:
    """ISO 字符串 → naive UTC datetime（MySQL DATETIME 无时区，统一按 UTC 存取）"""
    if not iso:
        return None
    dt = datetime.fromisoformat(iso)
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def _row_to_account(row: Any) -> UserAccount | None:
    if row is None:
        return None
    return UserAccount(
        user_id=row.user_id,
        username=row.username,
        password_hash=row.password_hash,
        display_name=row.display_name,
        role=row.role,
        status=row.status,
        created_at=row.created_at.isoformat() if row.created_at else None,
        last_active_at=row.last_active_at.isoformat() if row.last_active_at else None,
    )


class MySqlAccountStore:
    """账号 + 令牌的 MySQL 持久化实现（engine 可注入，测试可传 SQLite/内存库）"""

    def __init__(self, engine: Any | None = None) -> None:
        self._engine = engine

    @property
    def engine(self):
        if self._engine is None:
            self._engine = get_engine()
        return self._engine

    def save(self, account: UserAccount) -> None:
        """插入或整体覆盖（按 user_id 幂等；角色/状态等字段随最新值更新）"""
        with self.engine.begin() as conn:
            conn.execute(
                text(
                    """
                    INSERT INTO users
                        (user_id, username, password_hash, display_name, role, status, created_at, last_active_at)
                    VALUES (:user_id, :username, :password_hash, :display_name, :role, :status, :created_at, :last_active_at)
                    ON DUPLICATE KEY UPDATE
                        username = VALUES(username),
                        password_hash = VALUES(password_hash),
                        display_name = VALUES(display_name),
                        role = VALUES(role),
                        status = VALUES(status),
                        last_active_at = VALUES(last_active_at)
                    """
                ),
                {
                    "user_id": account.user_id,
                    "username": account.username,
                    "password_hash": account.password_hash,
                    "display_name": account.display_name,
                    "role": account.role,
                    "status": account.status,
                    "created_at": _to_naive_utc(account.created_at),
                    "last_active_at": _to_naive_utc(account.last_active_at),
                },
            )

    def load_by_id(self, user_id: str) -> UserAccount | None:
        with self.engine.connect() as conn:
            row = conn.execute(text("SELECT * FROM users WHERE user_id = :uid"), {"uid": user_id}).mappings().first()
        return _row_to_account(row)

    def load_by_username(self, username: str) -> UserAccount | None:
        with self.engine.connect() as conn:
            row = conn.execute(text("SELECT * FROM users WHERE username = :uname"), {"uname": username}).mappings().first()
        return _row_to_account(row)

    def delete(self, user_id: str) -> None:
        """删除账号及其全部令牌"""
        with self.engine.begin() as conn:
            conn.execute(text("DELETE FROM user_tokens WHERE user_id = :uid"), {"uid": user_id})
            conn.execute(text("DELETE FROM users WHERE user_id = :uid"), {"uid": user_id})

    def list_accounts(self) -> list[UserAccount]:
        with self.engine.connect() as conn:
            rows = conn.execute(text("SELECT * FROM users ORDER BY created_at DESC")).mappings().all()
        return [a for a in (_row_to_account(r) for r in rows) if a is not None]

    def save_token(self, token: str, user_id: str) -> None:
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        expires = now + timedelta(seconds=settings.auth_token_ttl_seconds)
        with self.engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO user_tokens (token, user_id, expires_at, created_at) "
                    "VALUES (:token, :user_id, :expires, :now)"
                ),
                {"token": token, "user_id": user_id, "expires": expires, "now": now},
            )

    def load_token_user(self, token: str) -> str | None:
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        with self.engine.connect() as conn:
            row = conn.execute(
                text("SELECT user_id FROM user_tokens WHERE token = :token AND expires_at > :now"),
                {"token": token, "now": now},
            ).mappings().first()
        return row.user_id if row else None

    def delete_token(self, token: str) -> None:
        with self.engine.begin() as conn:
            conn.execute(text("DELETE FROM user_tokens WHERE token = :token"), {"token": token})

    def delete_tokens_for_user(self, user_id: str) -> None:
        with self.engine.begin() as conn:
            conn.execute(text("DELETE FROM user_tokens WHERE user_id = :uid"), {"uid": user_id})


class InMemoryAccountStore:
    """进程内实现（测试用，行为与 MySqlAccountStore 一致）"""

    def __init__(self) -> None:
        self._users: dict[str, UserAccount] = {}
        self._username_index: dict[str, str] = {}
        self._tokens: dict[str, str] = {}

    def save(self, account: UserAccount) -> None:
        self._users[account.user_id] = account
        self._username_index[account.username] = account.user_id

    def load_by_id(self, user_id: str) -> UserAccount | None:
        return self._users.get(user_id)

    def load_by_username(self, username: str) -> UserAccount | None:
        uid = self._username_index.get(username)
        return self._users.get(uid) if uid else None

    def delete(self, user_id: str) -> None:
        account = self._users.pop(user_id, None)
        if account:
            self._username_index.pop(account.username, None)
        self._tokens = {t: u for t, u in self._tokens.items() if u != user_id}

    def list_accounts(self) -> list[UserAccount]:
        return sorted(self._users.values(), key=lambda a: a.created_at, reverse=True)

    def save_token(self, token: str, user_id: str) -> None:
        self._tokens[token] = user_id

    def load_token_user(self, token: str) -> str | None:
        return self._tokens.get(token)

    def delete_token(self, token: str) -> None:
        self._tokens.pop(token, None)

    def delete_tokens_for_user(self, user_id: str) -> None:
        self._tokens = {t: u for t, u in self._tokens.items() if u != user_id}


account_store = MySqlAccountStore()
