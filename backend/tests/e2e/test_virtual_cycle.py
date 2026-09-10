from types import SimpleNamespace

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.api.dependencies import get_cycle_service
from app.auth.dependencies import get_current_user
from app.db.models import Base, User
from app.main import app
from app.services.cycle import TradingCycleService


def test_fixture_virtual_cycle_persists_authenticated_decision(tmp_path) -> None:
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'e2e.db'}")
    Base.metadata.create_all(engine)
    session = Session(engine)
    session.add(User(id="user-e2e", clerk_user_id="clerk-e2e", email="e2e@example.com"))
    session.commit()
    service = TradingCycleService(db=session)
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id="user-e2e")
    app.dependency_overrides[get_cycle_service] = lambda: service
    try:
        with TestClient(app) as client:
            health = client.get("/api/health")
            assert health.status_code == 200
            assert health.json()["weex_mode"] == "virtual"

            response = client.post("/api/test/run-cycle", headers={"X-Test-Exchange": "fixture"})
            assert response.status_code == 200
            assert response.json()["action"] == "HOLD"

            decisions = client.get("/api/decisions")
            assert decisions.status_code == 200
            assert len(decisions.json()["items"]) == 1
            assert decisions.json()["items"][0]["symbol"] == "BTC-USDT"

            market = client.get("/api/market")
            assert market.status_code == 200
            assert market.json()["items"][0]["symbol"] == "BTC-USDT"
    finally:
        app.dependency_overrides.clear()
        session.close()
        engine.dispose()
