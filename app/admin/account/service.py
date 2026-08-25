"""账号认证服务：注册 / 登录 / 令牌 / 种子管理员"""

from __future__ import annotations

import hashlib
import logging
import secrets
from datetime import datetime, timezone

from app.admin.account.schema import AccountRole, UserAccount
from app.admin.account.store import AccountStore, MySqlAccountStore
from app.infrastructure.settings import settings

logger = logging.getLogger(__name__)

_PBKDF2_ITERATIONS = 100_000


def _hash_password(password: str) -> str:
    """pbkdf2-sha256 加盐哈希，格式 pbkdf2_sha256$iterations$salt$digest"""
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), _PBKDF2_ITERATIONS).hex()
    return f"pbkdf2_sha256${_PBKDF2_ITERATIONS}${salt}${digest}"


def _verify_password(password: str, stored: str) -> bool:
    try:
        _algo, iterations, salt, digest = stored.split("$")
        calc = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), int(iterations)).hex()
        return secrets.compare_digest(calc, digest)
    except (ValueError, TypeError):
        return False


class AuthService:
    """账号读写 + 登录态（store 可注入，测试用 InMemoryAccountStore）"""

    def __init__(self, store: AccountStore | None = None) -> None:
        self._store: AccountStore = store or MySqlAccountStore()

    @property
    def store(self) -> AccountStore:
        return self._store

    def register(
        self,
        username: str,
        password: str,
        display_name: str | None = None,
        role: AccountRole = "user",
    ) -> UserAccount:
        """创建账号；用户名唯一（小写归一），密码长度校验。冲突/非法抛 ValueError"""
        username = (username or "").strip().lower()
        if len(username) < 2:
            raise ValueError("用户名至少 2 个字符")
        if not password or len(password) < 6:
            raise ValueError("密码至少 6 位")
        if self._store.load_by_username(username) is not None:
            raise ValueError("用户名已存在")
        account = UserAccount(
            user_id=secrets.token_hex(16),
            username=username,
            password_hash=_hash_password(password),
            display_name=(display_name or "").strip() or None,
            role=role,
        )
        self._store.save(account)
        return account

    def login(self, username: str, password: str) -> tuple[str, UserAccount]:
        """校验用户名密码，返回 (token, account)；失败/被禁用抛 ValueError"""
        account = self._store.load_by_username((username or "").strip().lower())
        if account is None or not _verify_password(password or "", account.password_hash):
            raise ValueError("用户名或密码错误")
        if account.status == "disabled":
            raise ValueError("账号已被禁用，请联系管理员")
        token = secrets.token_urlsafe(32)
        self._store.save_token(token, account.user_id)
        self._touch(account)
        return token, account

    def authenticate(self, token: str | None) -> UserAccount | None:
        """按令牌取当前用户；令牌无效/过期/账号被禁返回 None"""
        if not token:
            return None
        user_id = self._store.load_token_user(token)
        if not user_id:
            return None
        account = self._store.load_by_id(user_id)
        if account is None or account.status == "disabled":
            return None
        return account

    def logout(self, token: str | None) -> None:
        if token:
            self._store.delete_token(token)

    def touch_active(self, user_id: str) -> None:
        """刷新最近活跃时间（聊天成功后尽力而为）"""
        account = self._store.load_by_id(user_id)
        if account is not None:
            self._touch(account)

    def _touch(self, account: UserAccount) -> None:
        updated = account.model_copy(update={"last_active_at": datetime.now(timezone.utc).isoformat()})
        self._store.save(updated)

    def seed_admin(self) -> None:
        """启动时按 env 配置创建种子管理员（已存在则跳过）"""
        username = (settings.admin_seed_username or "").strip().lower()
        password = settings.admin_seed_password
        if not username or not password:
            return
        if self._store.load_by_username(username) is not None:
            return
        try:
            self.register(username, password, display_name="系统管理员", role="admin")
            logger.info("admin_seed_created, username=%s", username)
        except ValueError as exc:
            logger.warning("admin_seed_failed: %s", exc)


auth_service = AuthService()
