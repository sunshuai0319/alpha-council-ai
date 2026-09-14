import os
from logging.config import fileConfig

from sqlalchemy import create_engine, pool

from alembic import context
from app.config import get_settings
from app.db.models import Base

config = context.config
config.set_main_option("sqlalchemy.url", "unused")

if config.config_file_name is not None:
    # 不要让同进程中的迁移关闭应用 logger；否则迁移后 Milvus/RAG 调用会变成静默。
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata

#: DDL 被锁阻塞时最多等待的时间。Postgres 默认 0 = 无限等待，一个空闲的
#: idle-in-transaction 连接就足以让 `alembic upgrade` 永远挂住。
LOCK_TIMEOUT = "10s"


def _database_url() -> str:
    """`ALEMBIC_DATABASE_URL` 优先，便于对测试库或其它环境执行迁移。"""

    return os.environ.get("ALEMBIC_DATABASE_URL") or get_settings().postgres_url


def run_migrations_offline() -> None:
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = create_engine(_database_url(), poolclass=pool.NullPool)
    with connectable.connect() as connection:
        if connection.dialect.name == "postgresql":
            # 宁愿 10 秒后带着明确报错失败，也不要静默挂死。
            connection.exec_driver_sql(f"SET lock_timeout = '{LOCK_TIMEOUT}'")
            # 必须立刻结束这条 SET 开启的事务：否则 alembic 检测到已存在事务，
            # 就不再自己管理提交，迁移跑完但版本号不会落库。
            connection.commit()
        context.configure(
            connection=connection, target_metadata=target_metadata, compare_type=True
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
