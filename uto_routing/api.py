from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from contextlib import asynccontextmanager
from contextvars import ContextVar
from functools import lru_cache
from pathlib import Path
from uuid import uuid4
from typing import Any

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, model_validator

from uto_routing.config import get_settings
from uto_routing.logging_utils import configure_logging
from uto_routing.realtime import PlaybackStreamConfig, RealtimeHub
from uto_routing.reviewer_auth import ReviewerAuthManager
from uto_routing.service import RoutingPlatform

settings = get_settings()
configure_logging(settings)
logger = logging.getLogger("uto_routing.api")
request_id_context: ContextVar[str | None] = ContextVar("request_id", default=None)
realtime_hub = RealtimeHub(
    playback_config=PlaybackStreamConfig(frame_delay_ms=settings.replay_stream_frame_delay_ms)
)
auth_manager = ReviewerAuthManager(settings)
auth_manager.validate_configuration()

@lru_cache(maxsize=1)
def get_platform() -> RoutingPlatform:
    return RoutingPlatform(settings=settings)


class RecommendationRequest(BaseModel):
    task_id: str | None = None
    priority: str | None = None
    destination_uwi: str | None = None
    planned_start: str | None = None
    duration_hours: float | None = None
    task_type: str | None = None
    shift: str | None = None
    start_day: str | None = None
    strategy: str = "priority_greedy"
    top_k: int = Field(default=3, ge=1, le=10)

    @model_validator(mode="after")
    def validate_request(self) -> "RecommendationRequest":
        custom_fields = [
            "priority",
            "destination_uwi",
            "planned_start",
            "duration_hours",
            "task_type",
            "shift",
            "start_day",
        ]
        is_custom_request = self.task_id is None or any(getattr(self, field) is not None for field in custom_fields)
        if is_custom_request:
            required_fields = ["priority", "destination_uwi", "planned_start", "duration_hours"]
            missing = [field for field in required_fields if getattr(self, field) is None]
            if missing:
                raise ValueError(f"Missing fields for custom task request: {', '.join(missing)}")
        return self


class RouteFrom(BaseModel):
    wialon_id: int | None = None
    lon: float | None = None
    lat: float | None = None

    @model_validator(mode="after")
    def validate_payload(self) -> "RouteFrom":
        if self.wialon_id is not None:
            return self
        if self.lon is None or self.lat is None:
            raise ValueError("Provide wialon_id or both lon/lat.")
        return self


class RouteTo(BaseModel):
    uwi: str | None = None
    lon: float | None = None
    lat: float | None = None

    @model_validator(mode="after")
    def validate_payload(self) -> "RouteTo":
        if self.uwi is not None:
            return self
        if self.lon is None or self.lat is None:
            raise ValueError("Provide uwi or both lon/lat.")
        return self


class RouteRequest(BaseModel):
    from_: RouteFrom = Field(alias="from")
    to: RouteTo
    speed_kmph: float | None = Field(default=None, gt=0)


class MultitaskConstraints(BaseModel):
    max_total_time_minutes: float = Field(default=480.0, gt=0)
    max_detour_ratio: float = Field(default=1.3, ge=1.0)


class MultitaskRequest(BaseModel):
    task_ids: list[str]
    constraints: MultitaskConstraints = Field(default_factory=MultitaskConstraints)


class PlanRequest(BaseModel):
    task_ids: list[str] | None = None
    strategy: str = "priority_greedy"


class BenchmarkRequest(BaseModel):
    scenarios: int = Field(default=250, ge=1, le=5000)
    min_tasks: int = Field(default=6, ge=1, le=100)
    max_tasks: int = Field(default=12, ge=1, le=100)
    min_vehicles: int = Field(default=4, ge=1, le=50)
    max_vehicles: int = Field(default=7, ge=1, le=50)
    seed: int = 42

    @model_validator(mode="after")
    def validate_ranges(self) -> "BenchmarkRequest":
        if self.min_tasks > self.max_tasks:
            raise ValueError("min_tasks cannot exceed max_tasks")
        if self.min_vehicles > self.max_vehicles:
            raise ValueError("min_vehicles cannot exceed max_vehicles")
        return self


class ReplayRequest(BaseModel):
    strategy: str = "priority_greedy"
    frame_interval_minutes: int = Field(default=15, ge=1, le=120)


class TuningRequest(BaseModel):
    candidate_limit: int = Field(default=12, ge=1, le=100)


class LoginRequest(BaseModel):
    username: str
    password: str


@asynccontextmanager
async def lifespan(_app: FastAPI):
    realtime_hub.bind_loop(asyncio.get_running_loop())
    yield


app = FastAPI(
    title="UTO Routing Platform",
    version="0.1.0",
    description="Intelligent dispatching and routing backend for special oilfield vehicles.",
    lifespan=lifespan,
)
STATIC_DIR = Path(__file__).resolve().parent / "static"
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

OPEN_PATH_PREFIXES = ("/static",)
OPEN_PATHS = {
    "/health",
    "/app-config",
    "/login",
    "/auth/login",
    "/favicon.ico",
}


@app.middleware("http")
async def request_middleware(request: Request, call_next):
    request_id = request.headers.get("x-request-id", str(uuid4()))
    request_id_context.set(request_id)
    started = time.perf_counter()

    identity = auth_manager.resolve_identity(request)
    request.state.reviewer = identity

    if auth_manager.enabled and not _is_open_path(request.url.path):
        if identity is None:
            if request.url.path.startswith("/api/") or request.url.path.startswith("/auth/"):
                return JSONResponse({"detail": "Authentication required."}, status_code=401)
            next_param = request.url.path
            if request.url.query:
                next_param = f"{next_param}?{request.url.query}"
            return RedirectResponse(url=f"/login?next={next_param}", status_code=303)

    if settings.api_key and request.url.path.startswith("/api/") and identity is None:
        provided_key = request.headers.get("x-api-key")
        authorization = request.headers.get("authorization", "")
        if not provided_key and authorization.lower().startswith("bearer "):
            provided_key = authorization.split(" ", 1)[1]
        if provided_key != settings.api_key:
            return JSONResponse({"detail": "Invalid or missing API key."}, status_code=401)

    response = await call_next(request)
    response.headers["x-request-id"] = request_id
    duration_ms = round((time.perf_counter() - started) * 1000.0, 2)
    logger.info(
        "request completed",
        extra={
            "request_id": request_id,
            "method": request.method,
            "path": request.url.path,
            "status_code": response.status_code,
            "duration_ms": duration_ms,
        },
    )
    return response


@app.get("/")
def dashboard() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/login")
def login_page() -> FileResponse:
    return FileResponse(STATIC_DIR / "login.html")


@app.websocket("/ws/live")
async def live_socket(websocket: WebSocket) -> None:
    api_key = websocket.query_params.get("api_key")
    if auth_manager.enabled:
        identity = auth_manager.resolve_identity_from_token(websocket.cookies.get(auth_manager.cookie_name))
        if identity is None:
            await websocket.close(code=4401)
            return

    if not auth_manager.enabled and settings.websocket_token and api_key != settings.websocket_token:
        await websocket.close(code=4401)
        return
    if not auth_manager.enabled and not settings.websocket_token and settings.api_key and api_key != settings.api_key:
        await websocket.close(code=4401)
        return

    await realtime_hub.connect(websocket)
    platform = get_platform()
    await realtime_hub.send(websocket, "snapshot", platform.live_state())
    await realtime_hub.send(websocket, "audit_trail", platform.audit_events(limit=20))

    try:
        while True:
            message = await websocket.receive_text()
            try:
                payload = json.loads(message)
            except json.JSONDecodeError:
                await realtime_hub.send(
                    websocket,
                    "error",
                    {"detail": "Invalid JSON payload."},
                )
                continue

            message_type = payload.get("type")
            if message_type == "ping":
                await realtime_hub.send(websocket, "pong", {"status": "ok"})
            elif message_type == "request_snapshot":
                await realtime_hub.send(websocket, "snapshot", platform.live_state())
            elif message_type == "request_audit":
                limit = int(payload.get("limit", 20))
                await realtime_hub.send(websocket, "audit_trail", platform.audit_events(limit=limit))
            else:
                await realtime_hub.send(
                    websocket,
                    "error",
                    {"detail": f"Unsupported message type: {message_type}"},
                )
    except WebSocketDisconnect:
        await realtime_hub.disconnect(websocket)


@app.get("/health")
def healthcheck() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/app-config")
def app_config(request: Request) -> dict[str, Any]:
    identity = getattr(request.state, "reviewer", None)
    return {
        "auth_enabled": auth_manager.enabled,
        "auth_mode": settings.auth_mode,
        "reviewer": identity.to_dict() if identity is not None else None,
        "websocket_token": settings.websocket_token if not auth_manager.enabled else None,
        "map_defaults": {
            "lat": settings.map_default_lat,
            "lon": settings.map_default_lon,
            "zoom": settings.map_default_zoom,
        },
    }


@app.get("/auth/me")
def auth_me(request: Request) -> dict[str, Any]:
    identity = getattr(request.state, "reviewer", None)
    if auth_manager.enabled and identity is None:
        raise HTTPException(status_code=401, detail="Authentication required.")
    return {
        "authenticated": identity is not None,
        "reviewer": identity.to_dict() if identity is not None else None,
    }


@app.post("/auth/login")
def auth_login(request: Request, payload: LoginRequest) -> JSONResponse:
    if not auth_manager.enabled:
        raise HTTPException(status_code=400, detail="Reviewer auth is disabled.")
    if not auth_manager.authenticate_credentials(payload.username, payload.password):
        raise HTTPException(status_code=401, detail="Неверный логин или пароль.")
    identity = auth_manager.issue_session(payload.username)
    response = JSONResponse(
        {
            "status": "ok",
            "reviewer": identity.to_dict(),
        }
    )
    auth_manager.set_session_cookie(response, identity, request)
    return response


@app.post("/auth/logout")
def auth_logout() -> JSONResponse:
    response = JSONResponse({"status": "ok"})
    auth_manager.clear_session_cookie(response)
    return response


@app.get("/api/catalog")
def dataset_catalog() -> dict[str, Any]:
    return get_platform().catalog()


@app.get("/api/live-state")
def live_state() -> dict[str, Any]:
    return get_platform().live_state()


@app.get("/api/audit/trail")
def audit_trail(limit: int = 50, action: str | None = None) -> dict[str, Any]:
    return get_platform().audit_events(limit=limit, action=action)


@app.delete("/api/audit/trail")
def clear_audit_trail() -> dict[str, str]:
    platform = get_platform()
    response = platform.clear_audit_events()
    _schedule_realtime_audit(platform)
    return response


@app.get("/api/benchmark/reports")
def list_benchmark_reports(limit: int = 20) -> dict[str, Any]:
    return get_platform().benchmark_reports(limit=limit)


@app.get("/api/benchmark/reports/latest")
def latest_benchmark_report() -> dict[str, Any]:
    report = get_platform().benchmark_report(latest=True)
    if report is None:
        raise HTTPException(status_code=404, detail="No benchmark reports available.")
    return report


@app.get("/api/benchmark/reports/latest.csv")
def latest_benchmark_report_csv() -> PlainTextResponse:
    try:
        return PlainTextResponse(
            get_platform().benchmark_report_csv(latest=True),
            media_type="text/csv",
        )
    except Exception as exc:  # pragma: no cover
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/api/replay/run")
def replay(request: ReplayRequest) -> dict[str, Any]:
    platform = get_platform()
    try:
        response = platform.replay(
            strategy=request.strategy,
            frame_interval_minutes=request.frame_interval_minutes,
        )
        _schedule_realtime_snapshot(platform)
        _schedule_realtime_audit(platform)
        realtime_hub.schedule_playback_stream(response)
        return response
    except Exception as exc:  # pragma: no cover
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/tuning/run")
def tuning(request: TuningRequest) -> dict[str, Any]:
    platform = get_platform()
    try:
        response = platform.tune_weights(candidate_limit=request.candidate_limit)
        _schedule_realtime_audit(platform)
        return response
    except Exception as exc:  # pragma: no cover
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/dataset/summary")
def dataset_summary() -> dict[str, Any]:
    return get_platform().summary()


@app.post("/api/dataset/reload")
def reload_dataset() -> dict[str, Any]:
    platform = get_platform()
    platform.refresh()
    response = {"status": "reloaded", "summary": platform.summary()}
    _schedule_realtime_snapshot(platform)
    _schedule_realtime_audit(platform)
    return response


@app.post("/api/recommendations")
def recommendations(request: RecommendationRequest) -> dict[str, Any]:
    payload = request.model_dump(exclude_none=True)
    platform = get_platform()
    try:
        response = platform.recommend(
            task_id=request.task_id,
            payload=payload,
            strategy=request.strategy,
            top_k=request.top_k,
        )
        _schedule_realtime_audit(platform)
        return response
    except Exception as exc:  # pragma: no cover - HTTP mapping wrapper
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/route")
def route(request: RouteRequest) -> dict[str, Any]:
    platform = get_platform()
    try:
        response = platform.route(
            from_vehicle_id=request.from_.wialon_id,
            from_lon=request.from_.lon,
            from_lat=request.from_.lat,
            to_uwi=request.to.uwi,
            to_lon=request.to.lon,
            to_lat=request.to.lat,
            speed_kmph=request.speed_kmph,
        )
        _schedule_realtime_snapshot(platform)
        _schedule_realtime_audit(platform)
        return response
    except Exception as exc:  # pragma: no cover - HTTP mapping wrapper
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/multitask")
def multitask(request: MultitaskRequest) -> dict[str, Any]:
    platform = get_platform()
    try:
        response = platform.multitask(
            task_ids=request.task_ids,
            max_total_time_minutes=request.constraints.max_total_time_minutes,
            max_detour_ratio=request.constraints.max_detour_ratio,
        )
        _schedule_realtime_audit(platform)
        return response
    except Exception as exc:  # pragma: no cover - HTTP mapping wrapper
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/plan")
def plan(request: PlanRequest) -> dict[str, Any]:
    platform = get_platform()
    try:
        response = platform.batch_plan(
            task_ids=request.task_ids,
            strategy=request.strategy,
        )
        _schedule_realtime_snapshot(platform)
        _schedule_realtime_audit(platform)
        return response
    except Exception as exc:  # pragma: no cover - HTTP mapping wrapper
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/benchmark/run")
def benchmark(request: BenchmarkRequest) -> dict[str, Any]:
    platform = get_platform()
    try:
        response = platform.benchmark(
            scenarios=request.scenarios,
            min_tasks=request.min_tasks,
            max_tasks=request.max_tasks,
            min_vehicles=request.min_vehicles,
            max_vehicles=request.max_vehicles,
            seed=request.seed,
        )
        _schedule_realtime_audit(platform)
        return response
    except Exception as exc:  # pragma: no cover - HTTP mapping wrapper
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _schedule_realtime_snapshot(platform: RoutingPlatform) -> None:
    realtime_hub.schedule_snapshot(platform.live_state())


def _schedule_realtime_audit(platform: RoutingPlatform, *, limit: int = 20) -> None:
    realtime_hub.schedule_audit(platform.audit_events(limit=limit))


def _is_open_path(path: str) -> bool:
    if path in OPEN_PATHS:
        return True
    return any(path.startswith(prefix) for prefix in OPEN_PATH_PREFIXES)

