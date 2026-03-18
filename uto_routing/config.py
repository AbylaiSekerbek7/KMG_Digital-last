from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

from pydantic import BaseModel, Field

from uto_routing.scoring import ScoringWeights


class RuntimeSettings(BaseModel):
    auth_mode: str = "none"
    reviewer_username: str | None = None
    reviewer_password: str | None = None
    reviewer_display_name: str = "Reviewer"
    session_secret: str | None = None
    session_cookie_name: str = "uto_reviewer_session"
    session_ttl_hours: int = 24
    force_secure_cookies: bool = False

    data_source: str = Field(default="sample")
    data_dir: str | None = None
    database_url: str | None = None
    app_database_url: str | None = None
    app_db_path: str | None = None
    api_key: str | None = None
    websocket_token: str | None = None
    log_level: str = "INFO"
    log_format: str = "json"

    pg_table_road_nodes: str = "road_nodes"
    pg_table_road_edges: str = "road_edges"
    pg_table_wells: str = "wells"
    pg_table_vehicles: str = "vehicles"
    pg_table_tasks: str = "tasks"
    pg_table_compatibility: str = "compatibility"

    audit_max_entries: int = 200
    ortools_time_limit_seconds: int = 2
    scoring_weights: ScoringWeights = Field(default_factory=ScoringWeights)
    replay_stream_frame_delay_ms: int = 400

    map_default_lat: float = 46.75
    map_default_lon: float = 55.90
    map_default_zoom: int = 10


def load_settings() -> RuntimeSettings:
    data_source = os.getenv("UTO_DATA_SOURCE")
    data_dir = os.getenv("UTO_DATA_DIR")
    database_url = os.getenv("UTO_DATABASE_URL")
    if not data_source:
        hackathon_flag = os.getenv("UTO_HACKATHON_DB", "").lower() in {"1", "true", "yes", "on"}
        if database_url and hackathon_flag:
            data_source = "hackathon_db"
        elif database_url:
            data_source = "postgres"
        elif data_dir:
            data_source = "directory"
        else:
            data_source = "sample"

    return RuntimeSettings(
        auth_mode=os.getenv("UTO_AUTH_MODE", "none"),
        reviewer_username=os.getenv("UTO_REVIEWER_USERNAME"),
        reviewer_password=os.getenv("UTO_REVIEWER_PASSWORD"),
        reviewer_display_name=os.getenv("UTO_REVIEWER_DISPLAY_NAME", "Reviewer"),
        session_secret=os.getenv("UTO_SESSION_SECRET"),
        session_cookie_name=os.getenv("UTO_SESSION_COOKIE_NAME", "uto_reviewer_session"),
        session_ttl_hours=int(os.getenv("UTO_SESSION_TTL_HOURS", "24")),
        force_secure_cookies=os.getenv("UTO_FORCE_SECURE_COOKIES", "false").lower() in {"1", "true", "yes", "on"},
        data_source=data_source,
        data_dir=data_dir,
        database_url=database_url,
        app_database_url=os.getenv("UTO_APP_DATABASE_URL"),
        app_db_path=os.getenv("UTO_APP_DB_PATH"),
        api_key=os.getenv("UTO_API_KEY"),
        websocket_token=os.getenv("UTO_WEBSOCKET_TOKEN"),
        log_level=os.getenv("UTO_LOG_LEVEL", "INFO"),
        log_format=os.getenv("UTO_LOG_FORMAT", "json"),
        pg_table_road_nodes=os.getenv("UTO_PG_TABLE_ROAD_NODES", "road_nodes"),
        pg_table_road_edges=os.getenv("UTO_PG_TABLE_ROAD_EDGES", "road_edges"),
        pg_table_wells=os.getenv("UTO_PG_TABLE_WELLS", "wells"),
        pg_table_vehicles=os.getenv("UTO_PG_TABLE_VEHICLES", "vehicles"),
        pg_table_tasks=os.getenv("UTO_PG_TABLE_TASKS", "tasks"),
        pg_table_compatibility=os.getenv("UTO_PG_TABLE_COMPATIBILITY", "compatibility"),
        audit_max_entries=int(os.getenv("UTO_AUDIT_MAX_ENTRIES", "200")),
        ortools_time_limit_seconds=int(os.getenv("UTO_ORTOOLS_TIME_LIMIT_SECONDS", "2")),
        scoring_weights=ScoringWeights(
            distance_weight=float(os.getenv("UTO_DISTANCE_WEIGHT", "2.2")),
            travel_weight=float(os.getenv("UTO_TRAVEL_WEIGHT", "0.3")),
            wait_weight=float(os.getenv("UTO_WAIT_WEIGHT", "0.8")),
            lateness_base_penalty=float(os.getenv("UTO_LATENESS_BASE_PENALTY", "10.0")),
            lateness_priority_multiplier=float(os.getenv("UTO_LATENESS_PRIORITY_MULTIPLIER", "30.0")),
            shift_penalty=float(os.getenv("UTO_SHIFT_PENALTY", "50.0")),
            incompatibility_penalty=float(os.getenv("UTO_INCOMPATIBILITY_PENALTY", "10000.0")),
            score_scale=float(os.getenv("UTO_SCORE_SCALE", "25.0")),
        ),
        replay_stream_frame_delay_ms=int(os.getenv("UTO_REPLAY_STREAM_FRAME_DELAY_MS", "400")),
        map_default_lat=float(os.getenv("UTO_MAP_DEFAULT_LAT", "46.75")),
        map_default_lon=float(os.getenv("UTO_MAP_DEFAULT_LON", "55.90")),
        map_default_zoom=int(os.getenv("UTO_MAP_DEFAULT_ZOOM", "10")),
    )


@lru_cache(maxsize=1)
def get_settings() -> RuntimeSettings:
    return load_settings()

