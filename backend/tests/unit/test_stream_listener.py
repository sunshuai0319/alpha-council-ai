"""监听器的账户扫描。

只测纯逻辑那部分：常驻循环本身由 probe_private_ws.py 在真实账户上验证过，
单测再起一遍 socket 没有增量价值。
"""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.db.models import Base, TradingAccount, User
from app.streams.listener import load_targets


@pytest.fixture
def db(tmp_path):
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'listener.db'}")
    Base.metadata.create_all(engine)
    session = Session(engine)
    session.add(User(id="u-1", clerk_user_id="clerk-u1"))
    session.commit()
    try:
        yield session
    finally:
        session.close()


def _account(db: Session, **overrides) -> TradingAccount:
    values = {
        "id": "acct-1",
        "user_id": "u-1",
        "provider": "weex",
        "environment": "virtual",
        "api_key_ref": "key-1",
        "api_secret_ref": "secret-1",
        "passphrase_ref": "pass-1",
        "enabled": True,
    }
    values.update(overrides)
    account = TradingAccount(**values)
    db.add(account)
    db.commit()
    return account


def test_loads_enabled_virtual_accounts_with_credentials(db):
    _account(db)

    targets = load_targets(db)

    assert len(targets) == 1
    assert targets[0].user_id == "u-1"
    assert targets[0].trading_account_id == "acct-1"
    assert targets[0].credentials.api_key == "key-1"


def test_skips_disabled_accounts(db):
    _account(db, enabled=False)

    assert load_targets(db) == []


def test_skips_accounts_without_credentials(db):
    """没配凭证的账户跳过即可 —— 不是错误，但要能被日志看见。"""

    _account(db, passphrase_ref=None)

    assert load_targets(db) == []


def test_skips_non_virtual_environments(db):
    """只跟虚拟盘。正式盘不该被这个采集层碰到。"""

    _account(db, environment="live")

    assert load_targets(db) == []


def test_build_client_subscribes_the_three_channels():
    from app.streams.client import PrivateStreamClient
    from app.streams.listener import build_client

    client = build_client(
        url="wss://example.test/v3/ws/private",
        api_key="k",
        api_secret="s",
        passphrase="p",
        initial_backoff=1.0,
        max_backoff=8.0,
    )

    assert isinstance(client, PrivateStreamClient)
    assert client.channels == ("fill", "orders", "account")
    assert client.max_backoff == 8.0
