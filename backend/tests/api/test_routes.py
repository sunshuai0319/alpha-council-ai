from types import SimpleNamespace

from fastapi.testclient import TestClient

from app.api.dependencies import get_cycle_service
from app.auth.dependencies import get_current_user
from app.main import app
from app.services.cycle import TradingCycleService


def test_business_route_rejects_missing_clerk_user() -> None:
    response = TestClient(app).get("/api/portfolio")
    assert response.status_code == 401


def test_pause_endpoint_changes_control_state() -> None:
    service = TradingCycleService()
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id="user-a")
    app.dependency_overrides[get_cycle_service] = lambda: service
    try:
        response = TestClient(app).post("/api/control/pause")
        assert response.status_code == 200
        assert response.json()["status"] == "PAUSED"

        response = TestClient(app).post("/api/control/resume")
        assert response.status_code == 200
        assert response.json()["status"] == "RUNNING"
    finally:
        app.dependency_overrides.clear()


def test_dashboard_routes_use_authenticated_user_scope() -> None:
    service = TradingCycleService()
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id="user-a")
    app.dependency_overrides[get_cycle_service] = lambda: service
    try:
        assert TestClient(app).get("/api/decisions").json() == {"items": []}
        assert TestClient(app).get("/api/portfolio").json() == {"items": []}
    finally:
        app.dependency_overrides.clear()
