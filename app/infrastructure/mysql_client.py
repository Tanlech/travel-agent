"""MySQL 连接与建表初始化（用户账号体系，SQLAlchemy + pymysql）"""

from __future__ import annotations

import sys
from pathlib import Path

# 沙箱环境无法写入 conda site-packages 时，pymysql 装入项目 vendor/ 目录；
# 在 create_engine 前把 vendor 加入 sys.path，保证 SQLAlchemy 能加载 pymysql 方言
_VENDOR_DIR = Path(__file__).resolve().parents[2] / "vendor"
if _VENDOR_DIR.is_dir() and str(_VENDOR_DIR) not in sys.path:
    sys.path.insert(0, str(_VENDOR_DIR))

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from app.infrastructure.settings import settings

_engine: Engine | None = None


def mysql_url() -> str:
    """按 settings 组装 MySQL 连接串（utf8mb4 支持中文/emoji）"""
    return (
        f"mysql+pymysql://{settings.mysql_user}:{settings.mysql_password}"
        f"@{settings.mysql_host}:{settings.mysql_port}/{settings.mysql_database}"
        f"?charset=utf8mb4"
    )


def get_engine() -> Engine:
    """全局 SQLAlchemy 引擎单例（连接池 + 断线自愈 pre_ping）"""
    global _engine
    if _engine is None:
        _engine = create_engine(
            mysql_url(),
            pool_size=settings.mysql_pool_size,
            max_overflow=5,
            pool_pre_ping=True,
            pool_recycle=3600,
        )
    return _engine


# 建表 SQL（幂等：IF NOT EXISTS）。用户账号 + 登录令牌两张表
_CREATE_TABLES_SQL = """
CREATE TABLE IF NOT EXISTS users (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    user_id VARCHAR(64) NOT NULL,
    username VARCHAR(64) NOT NULL,
    password_hash VARCHAR(255) NOT NULL,
    display_name VARCHAR(64) DEFAULT NULL,
    role VARCHAR(16) NOT NULL DEFAULT 'user',
    status VARCHAR(16) NOT NULL DEFAULT 'active',
    created_at DATETIME(3) NOT NULL,
    last_active_at DATETIME(3) DEFAULT NULL,
    PRIMARY KEY (id),
    UNIQUE KEY uk_user_id (user_id),
    UNIQUE KEY uk_username (username)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS user_tokens (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    token VARCHAR(64) NOT NULL,
    user_id VARCHAR(64) NOT NULL,
    expires_at DATETIME(3) NOT NULL,
    created_at DATETIME(3) NOT NULL,
    PRIMARY KEY (id),
    UNIQUE KEY uk_token (token),
    KEY idx_tokens_user (user_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
"""


def init_db() -> None:
    """建表（幂等），启动时调用；失败仅告警不阻断服务（MySQL 未就绪时账号功能不可用）"""
    try:
        engine = get_engine()
        with engine.begin() as conn:
            for stmt in _CREATE_TABLES_SQL.split(";"):
                stmt = stmt.strip()
                if stmt:
                    conn.execute(text(stmt))
    except Exception as exc:  # MySQL 未就绪/不可达时降级，账号接口会返回 503
        import logging

        logging.getLogger(__name__).warning("init_db 失败，账号功能暂不可用: %s", exc)
