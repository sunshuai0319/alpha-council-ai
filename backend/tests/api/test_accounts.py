from types import SimpleNamespace

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.api.dependencies import get_cycle_service
from app.auth.dependencies import get_current_user
from app.db.models import Base, TradingAccount, User
from app.main import app
from app.services.cycle import TradingCycleService


def test_account_api_only_accepts_virtual_env_reference(tmp_path) -> None:
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
                "api_key_ref": "env:WEEX_USER_KEY",
                "api_secret_ref": "env:WEEX_USER_SECRET",
                "passphrase_ref": "env:WEEX_USER_PASSPHRASE",
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
                "api_key_ref": "env:WEEX_USER_KEY",
                "api_secret_ref": "env:WEEX_USER_SECRET",
                "passphrase_ref": "env:WEEX_USER_PASSPHRASE",
                "environment": "live",
            },
        )
        assert live.status_code == 422
    finally:
        app.dependency_overrides.clear()
        db.close()
        engine.dispose()


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
