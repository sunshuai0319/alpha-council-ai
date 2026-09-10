"""迁移必须能在「已经有数据的库」上跑，而不只是空库。

001 曾经用 create_all(checkfirst=False)，对已存在的表会直接抛 DuplicateTable，
所以线上库根本没法升级 —— 加 risk_limits 这一列时就是这么卡住的。
"""

import os

from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect

from alembic import command

# 升级前的 trading_accounts：没有 risk_limits
LEGACY_TABLE = """
CREATE TABLE trading_accounts (
    id VARCHAR(36) NOT NULL PRIMARY KEY,
    user_id VARCHAR(36) NOT NULL,
    provider VARCHAR(32),
    environment VARCHAR(32),
    api_key_ref VARCHAR(256),
    api_secret_ref VARCHAR(256),
    passphrase_ref VARCHAR(256),
    enabled BOOLEAN,
    created_at TIMESTAMP,
    updated_at TIMESTAMP
)
"""


def _upgrade(url: str, revision: str = "head") -> None:
    config = Config("alembic.ini")
    os.environ["ALEMBIC_DATABASE_URL"] = url
    try:
        command.upgrade(config, revision)
    finally:
        del os.environ["ALEMBIC_DATABASE_URL"]


def _columns(url: str, table: str) -> set[str]:
    engine = create_engine(url)
    try:
        return {column["name"] for column in inspect(engine).get_columns(table)}
    finally:
        engine.dispose()


def test_migrations_upgrade_a_database_that_already_has_tables(tmp_path) -> None:
    url = f"sqlite+pysqlite:///{tmp_path / 'legacy.db'}"
    engine = create_engine(url)
    with engine.begin() as connection:
        connection.exec_driver_sql(LEGACY_TABLE)
    engine.dispose()

    _upgrade(url)

    assert "risk_limits" in _columns(url, "trading_accounts")
    assert "trading_decisions" in inspect(create_engine(url)).get_table_names()


def test_migrations_are_idempotent(tmp_path) -> None:
    """重复执行不能报错：部署脚本会无条件跑 upgrade head。"""
    url = f"sqlite+pysqlite:///{tmp_path / 'twice.db'}"

    _upgrade(url)
    _upgrade(url)

    assert "risk_limits" in _columns(url, "trading_accounts")


def test_new_tables_reach_databases_that_already_migrated(tmp_path) -> None:
    """已迁移的库不会重跑 001，新增的表必须靠后续迁移补上。

    只加模型不加迁移的话，worker 一跑就撞 "relation does not exist"。
    """
    url = f"sqlite+pysqlite:///{tmp_path / 'new_table.db'}"
    _upgrade(url, "002_trading_account_risk_limits")

    engine = create_engine(url)
    with engine.begin() as connection:
        connection.exec_driver_sql("DROP TABLE collector_errors")  # 模拟该表尚未存在
    engine.dispose()

    _upgrade(url)

    assert "collector_errors" in inspect(create_engine(url)).get_table_names()


def test_migrations_record_the_applied_revision(tmp_path) -> None:
    """版本号必须落库：否则每次 upgrade 都会重跑一遍迁移。"""
    url = f"sqlite+pysqlite:///{tmp_path / 'version.db'}"

    _upgrade(url)

    engine = create_engine(url)
    with engine.connect() as connection:
        stamped = connection.exec_driver_sql("select version_num from alembic_version").scalar()
    engine.dispose()

    head = ScriptDirectory.from_config(Config("alembic.ini")).get_current_head()
    assert stamped == head
