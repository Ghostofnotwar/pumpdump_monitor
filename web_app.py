"""Authenticated Mini App API and unauthenticated, local-only health probes."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, model_validator

from auth import InitDataError, validate_init_data
from config import Config
from db import Database
from snapshots import snapshot_path

logger = logging.getLogger(__name__)
BASE = Path(__file__).resolve().parent


class SettingsPatch(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, strict=True)
    timeframe: Literal["1", "3", "5", "15", "30", "60", "240", "D"] | None = None
    pump_threshold: float | None = Field(default=None, ge=1, le=50)
    volume_min_usd: float | None = Field(default=None, ge=100_000, le=10_000_000)
    paused: bool | None = None
    theme: Literal["dark", "light"] | None = None
    direction: Literal["both", "pump", "dump"] | None = None
    show_delta: bool | None = None
    show_heatmap: bool | None = None
    show_sma: bool | None = None

    @model_validator(mode="after")
    def not_empty(self):
        if not self.model_fields_set:
            raise ValueError("Provide at least one settings field")
        if any(getattr(self, field) is None for field in self.model_fields_set):
            raise ValueError("Null is not a valid settings value")
        return self


def public_settings(settings: dict) -> dict:
    return {
        key: (bool(value) if key in {"paused", "show_delta", "show_heatmap", "show_sma"} else value)
        for key, value in settings.items()
        if key in SettingsPatch.model_fields
    }


def create_app(config: Config, db: Database, monitor=None, bot_app=None) -> FastAPI:
    app = FastAPI(title="Pump & Dump Monitor", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.monitor = monitor
    app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        if request.method == "POST":
            length = request.headers.get("content-length", "")
            # This small JSON API does not accept streaming/chunked request bodies.
            if not length.isdigit() or int(length) > 8192:
                return JSONResponse({"detail": "A content-length of at most 8192 bytes is required"}, status_code=413)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self' https://telegram.org; style-src 'self'; "
            "img-src 'self' data: blob:; connect-src 'self'; base-uri 'none'; object-src 'none'; "
            "form-action 'none'; frame-ancestors https://web.telegram.org https://*.telegram.org https://t.me https://*.t.me"
        )
        if request.url.path != "/static":
            response.headers["Cache-Control"] = "no-store"
        return response

    async def chat_id(x_telegram_init_data: str = Header(default="", alias="X-Telegram-Init-Data")) -> int:
        try:
            user_id = validate_init_data(x_telegram_init_data, config.bot_token)
        except InitDataError as exc:
            raise HTTPException(status_code=401, detail=str(exc)) from None
        if config.allowed_chat_ids and user_id not in config.allowed_chat_ids:
            raise HTTPException(status_code=403, detail="This chat is not allowed")
        return user_id

    @app.get("/")
    async def index():
        return RedirectResponse("/webapp")

    @app.get("/webapp")
    async def webapp():
        return FileResponse(BASE / "templates" / "index.html", media_type="text/html")

    @app.get("/healthz")
    async def healthz():
        # Liveness must not depend on Bybit/Telegram availability: avoid external-outage restart loops.
        return {"status": "ok"}

    @app.get("/readyz")
    async def readyz():
        database_ok = await db.ping()
        polling_ok = bool(bot_app and bot_app.updater and bot_app.updater.running)
        market_ok = bool(monitor and monitor.ready)
        ready = database_ok and polling_ok and market_ok
        return JSONResponse(
            {
                "status": "ready" if ready else "warming_or_degraded",
                "database": database_ok,
                "polling": polling_ok,
                "market_data": market_ok,
            },
            status_code=200 if ready else 503,
        )

    @app.get("/api/settings")
    async def api_settings(user_id: int = Depends(chat_id)):
        return public_settings(await db.get_settings(user_id))

    @app.post("/api/settings")
    async def api_save_settings(patch: SettingsPatch, user_id: int = Depends(chat_id)):
        settings = await db.save_settings(user_id, patch.model_dump(exclude_unset=True))
        return {"ok": True, "settings": public_settings(settings)}

    @app.get("/api/alerts")
    async def api_alerts(user_id: int = Depends(chat_id), limit: int = 10):
        if not 1 <= limit <= 50:
            raise HTTPException(422, "limit must be 1..50")
        rows = await db.get_alerts(user_id, limit)
        for row in rows:
            image = snapshot_path(config.snapshot_dir, row.pop("screenshot_path"))
            row["chat_id"] = user_id
            row["image_url"] = f"/api/alerts/{row['id']}/image" if image else None
        return {"alerts": rows}

    @app.get("/api/alerts/{alert_id}/image")
    async def api_alert_image(alert_id: int, user_id: int = Depends(chat_id)):
        row = await db.get_alert(user_id, alert_id)
        image = snapshot_path(config.snapshot_dir, row.get("screenshot_path")) if row else None
        if not image:
            raise HTTPException(404, "Image not found or expired")
        return FileResponse(image, media_type="image/png", content_disposition_type="inline")

    @app.get("/api/status")
    async def api_status(user_id: int = Depends(chat_id)):
        status = monitor.status() if monitor else {"ready": False, "running": False}
        status["polling"] = bool(bot_app and bot_app.updater and bot_app.updater.running)
        return status

    return app
