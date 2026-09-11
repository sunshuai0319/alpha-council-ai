from types import SimpleNamespace

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.api.dependencies import get_cycle_service
from app.auth.dependencies import get_current_user
from app.config import Settings
from app.db.models import Base, TradingAccount, User
from app.main import app
from app.services.cycle import TradingCycleService


def _account_client(tmp_path, name: str = "limits.db"):
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / name}")
    Base.metadata.create_all(engine)
    db = Session(engine)
    db.add(User(id="user-account", clerk_user_id="clerk-account"))
    db.commit()
    service = TradingCycleService(db=db, settings=Settings(max_daily_loss_pct=0.05))
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id="user-account")
    app.dependency_overrides[get_cycle_service] = lambda: service
    return TestClient(app), db


def _create(client) -> str:
    response = client.post(
        "/api/accounts",
        json={
            "api_key_ref": "k",
            "api_secret_ref": "s",
            "passphrase_ref": "p",
            "environment": "virtual",
        },
    )
    assert response.status_code == 201
    return response.json()["id"]


def test_account_risk_limits_are_stored_and_echoed(tmp_path) -> None:
    client, _ = _account_client(tmp_path)
    try:
        account_id = _create(client)

        response = client.patch(
            f"/api/accounts/{account_id}",
            json={
                "enabled": True,
                "risk_limits": {
                    "max_position_notional_pct": 0.05,
                    "max_daily_loss_pct": 0.02,
                },
            },
        )

        assert response.status_code == 200
        body = response.json()
        assert body["risk_limits"] == {
            "max_position_notional_pct": 0.05,
            "max_daily_loss_pct": 0.02,
        }
        # UI 需要平台上限来约束输入范围
        assert body["platform_limits"]["max_daily_loss_pct"] == 0.05
    finally:
        app.dependency_overrides.clear()


def test_account_risk_limits_reject_loosening_beyond_the_platform(tmp_path) -> None:
    """调松必须明确报错并说清上限，而不是静默夹回。"""
    client, _ = _account_client(tmp_path)
    try:
        account_id = _create(client)

        response = client.patch(
            f"/api/accounts/{account_id}",
            json={"enabled": True, "risk_limits": {"max_daily_loss_pct": 0.5}},
        )

        assert response.status_code == 422
        assert "max_daily_loss_pct" in response.text
    finally:
        app.dependency_overrides.clear()


def test_account_risk_limits_survive_an_enable_toggle(tmp_path) -> None:
    """切换启用状态不能把已设的风险偏好清掉。"""
    client, _ = _account_client(tmp_path)
    try:
        account_id = _create(client)
        client.patch(
            f"/api/accounts/{account_id}",
            json={"enabled": True, "risk_limits": {"max_position_notional_pct": 0.05}},
        )

        response = client.patch(f"/api/accounts/{account_id}", json={"enabled": False})

        assert response.status_code == 200
        assert response.json()["risk_limits"] == {"max_position_notional_pct": 0.05}
    finally:
        app.dependency_overrides.clear()


def test_account_api_stores_ui_credentials_for_virtual_account(tmp_path) -> None:
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'accounts.db'}")
    Base.metadata.create_all(engine)
    db = Session(engine)
    db.add(User(id="user-account", clerk_user_id="clerk-account"))
    db.commit()
    service = TradingCycleService(db=db)
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id="user-account")
    app.dependency_overrides[get_cycle_service] = lambda: service
    try:
        client = TestClient(app)
        response = client.post(
            "/api/accounts",
            json={
                "api_key_ref": "ui-account-key",
                "api_secret_ref": "ui-account-secret",
                "passphrase_ref": "ui-account-passphrase",
                "environment": "virtual",
            },
        )
        assert response.status_code == 201
        account_id = response.json()["id"]
        assert response.json()["enabled"] is False
        assert client.patch(f"/api/accounts/{account_id}", json={"enabled": True}).status_code == 200
        assert db.scalar(select(TradingAccount).where(TradingAccount.id == account_id)).enabled is True

        live = client.post(
            "/api/accounts",
            json={
                "api_key_ref": "ui-account-key",
                "api_secret_ref": "ui-account-secret",
                "passphrase_ref": "ui-account-passphrase",
                "environment": "live",
            },
        )
        assert live.status_code == 422

        empty = client.post(
            "/api/accounts",
            json={
                "api_key_ref": "",
                "api_secret_ref": "ui-account-secret",
                "passphrase_ref": "ui-account-passphrase",
                "environment": "virtual",
            },
        )
        assert empty.status_code == 422

        env_reference = client.post(
            "/api/accounts",
            json={
                "api_key_ref": "env:WEEX_USER_KEY",
                "api_secret_ref": "ui-account-secret",
                "passphrase_ref": "ui-account-passphrase",
                "environment": "virtual",
            },
        )
        assert env_reference.status_code == 422
    finally:
        app.dependency_overrides.clear()
        db.close()
        engine.dispose()


def test_account_create_strips_pasted_weex_env_var_prefixes(tmp_path) -> None:
    """用户从 .env 复制凭证行时会把 WEEX_API_SECRET= 之类前缀一起粘进来。

    这正是线上 401 -1047 的根因：HMAC 用了带前缀的 secret，签名永远不匹配。
    """
    client, db = _account_client(tmp_path)
    try:
        response = client.post(
            "/api/accounts",
            json={
                "api_key_ref": "WEEX_API_KEY=weex_key_123",
                "api_secret_ref": "WEEX_API_SECRET=test-secret-redacted\n",
                "passphrase_ref": "WEEX_PASSPHRASE=test-passphrase",
                "environment": "virtual",
            },
        )
        assert response.status_code == 201
        account = db.scalar(select(TradingAccount).order_by(TradingAccount.created_at.desc()))
        assert account.api_key_ref == "weex_key_123"
        assert account.api_secret_ref == "test-secret-redacted"
        assert account.passphrase_ref == "test-passphrase"
    finally:
        app.dependency_overrides.clear()


def test_pause_is_persisted_per_user(tmp_path) -> None:
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'control.db'}")
    Base.metadata.create_all(engine)
    db = Session(engine)
    db.add(User(id="user-control", clerk_user_id="clerk-control"))
    db.commit()
    service = TradingCycleService(db=db)

    assert service.pause("user-control") == {"status": "PAUSED"}
    assert TradingCycleService(db=db).control_status("user-control") == "PAUSED"
    assert TradingCycleService(db=db).control_status("other-user") == "RUNNING"


def test_cycle_uses_enabled_ui_account_credentials(tmp_path) -> None:
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'cycle-account.db'}")
    Base.metadata.create_all(engine)
    db = Session(engine)
    db.add(User(id="user-cycle-account", clerk_user_id="clerk-cycle-account"))
    db.add(
        TradingAccount(
            id="account-cycle",
            user_id="user-cycle-account",
            provider="weex",
            environment="virtual",
            api_key_ref="ui-key",
            api_secret_ref="ui-secret",
            passphrase_ref="ui-passphrase",
            enabled=True,
        )
    )
    db.commit()
    service = TradingCycleService(db=db)

    exchange = service._exchange_for_user("user-cycle-account")

    assert exchange.credentials.api_key == "ui-key"
    assert exchange.credentials.api_secret == "ui-secret"
    assert exchange.credentials.passphrase == "ui-passphrase"
    exchange.close()
    db.close()
    engine.dispose()
