from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.routes.accounts import router as accounts_router
from app.api.routes.auth import router as auth_router
from app.api.routes.control import router as control_router
from app.api.routes.dashboard import router as dashboard_router
from app.api.routes.positions import router as positions_router
from app.api.routes.test_support import router as test_support_router
from app.api.routes.wallet import router as wallet_router
from app.api.routes.webhooks import router as webhooks_router
from app.config import get_settings
from app.logging import configure_logging

settings = get_settings()
configure_logging(settings.log_level)

app = FastAPI(title="Alpha Council AI API", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(auth_router)
app.include_router(accounts_router)
app.include_router(webhooks_router)
app.include_router(wallet_router)
app.include_router(dashboard_router)
app.include_router(control_router)
app.include_router(positions_router)
app.include_router(test_support_router)


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok", "environment": settings.app_env, "weex_mode": "virtual"}
