from fastapi import FastAPI

from app.api.routes.auth import router as auth_router
from app.config import get_settings
from app.logging import configure_logging

settings = get_settings()
configure_logging(settings.log_level)

app = FastAPI(title="Alpha Council AI API", version="0.1.0")
app.include_router(auth_router)


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok", "environment": settings.app_env, "weex_mode": "virtual"}
