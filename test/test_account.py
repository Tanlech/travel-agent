"""账号服务单元测试（AuthService + InMemoryAccountStore）"""

from __future__ import annotations

import pytest

from app.admin.account.schema import UserAccount
from app.admin.account.service import AuthService
from app.admin.account.store import InMemoryAccountStore


@pytest.fixture
def svc() -> AuthService:
    return AuthService(store=InMemoryAccountStore())


class TestRegister:
    def test_register_ok(self, svc: AuthService) -> None:
        acc = svc.register(" Alice ", "secret123", "小艾")
        assert acc.username == "alice"  # 用户名小写归一
        assert acc.display_name == "小艾"
        assert acc.role == "user"
        assert acc.status == "active"
        assert acc.password_hash.startswith("pbkdf2_sha256$")

    def test_register_duplicate_username(self, svc: AuthService) -> None:
        svc.register("alice", "secret123")
        with pytest.raises(ValueError, match="已存在"):
            svc.register("ALICE", "other123")

    def test_register_short_username(self, svc: AuthService) -> None:
        with pytest.raises(ValueError, match="用户名"):
            svc.register("a", "secret123")

    def test_register_short_password(self, svc: AuthService) -> None:
        with pytest.raises(ValueError, match="密码"):
            svc.register("alice", "123")

    def test_register_admin_role(self, svc: AuthService) -> None:
        acc = svc.register("boss", "secret123", role="admin")
        assert acc.role == "admin"


class TestLogin:
    def test_login_ok(self, svc: AuthService) -> None:
        svc.register("alice", "secret123")
        token, acc = svc.login("alice", "secret123")
        assert token
        assert acc.username == "alice"
        # 登录后令牌可换取账号
        assert svc.authenticate(token) is not None

    def test_login_wrong_password(self, svc: AuthService) -> None:
        svc.register("alice", "secret123")
        with pytest.raises(ValueError, match="用户名或密码错误"):
            svc.login("alice", "wrong")

    def test_login_unknown_user(self, svc: AuthService) -> None:
        with pytest.raises(ValueError, match="用户名或密码错误"):
            svc.login("nobody", "secret123")

    def test_login_disabled(self, svc: AuthService) -> None:
        acc = svc.register("alice", "secret123")
        acc = acc.model_copy(update={"status": "disabled"})
        svc.store.save(acc)
        with pytest.raises(ValueError, match="禁用"):
            svc.login("alice", "secret123")


class TestAuthenticate:
    def test_invalid_token(self, svc: AuthService) -> None:
        assert svc.authenticate("bad-token") is None

    def test_empty_token(self, svc: AuthService) -> None:
        assert svc.authenticate(None) is None

    def test_disabled_user_token_invalid(self, svc: AuthService) -> None:
        acc = svc.register("alice", "secret123")
        token, _ = svc.login("alice", "secret123")
        acc = acc.model_copy(update={"status": "disabled"})
        svc.store.save(acc)
        assert svc.authenticate(token) is None

    def test_logout_invalidates_token(self, svc: AuthService) -> None:
        svc.register("alice", "secret123")
        token, _ = svc.login("alice", "secret123")
        assert svc.authenticate(token) is not None
        svc.logout(token)
        assert svc.authenticate(token) is None


class TestDelete:
    def test_delete_removes_account_and_tokens(self, svc: AuthService) -> None:
        acc = svc.register("alice", "secret123")
        token, _ = svc.login("alice", "secret123")
        svc.store.delete(acc.user_id)
        assert svc.store.load_by_username("alice") is None
        assert svc.authenticate(token) is None

    def test_delete_user_data(self, svc: AuthService) -> None:
        """删除后仍可重新注册同名账号（用户名索引已清理）"""
        acc = svc.register("alice", "secret123")
        svc.store.delete(acc.user_id)
        new_acc = svc.register("alice", "newpass1")
        assert new_acc.username == "alice"


class TestSeedAdmin:
    def test_seed_creates_admin(self, monkeypatch, svc: AuthService) -> None:
        from app.infrastructure.settings import settings

        monkeypatch.setattr(settings, "admin_seed_username", "root")
        monkeypatch.setattr(settings, "admin_seed_password", "rootpass1")
        svc.seed_admin()
        acc = svc.store.load_by_username("root")
        assert acc is not None
        assert acc.role == "admin"

    def test_seed_skips_existing(self, monkeypatch, svc: AuthService) -> None:
        from app.infrastructure.settings import settings

        svc.register("root", "rootpass1", role="admin")
        monkeypatch.setattr(settings, "admin_seed_username", "root")
        monkeypatch.setattr(settings, "admin_seed_password", "rootpass1")
        svc.seed_admin()
        accounts = svc.store.list_accounts()
        assert len(accounts) == 1  # 未重复创建
