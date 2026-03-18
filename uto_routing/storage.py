from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from uto_routing.config import RuntimeSettings


class ApplicationStore:
    def __init__(self, settings: RuntimeSettings) -> None:
        self.settings = settings
        self.backend = self._resolve_backend()
        self.location = self._resolve_location()
        self._initialized = False

    def initialize(self) -> None:
        if self._initialized:
            return
        if self.backend == "sqlite":
            self._initialize_sqlite()
        else:
            self._initialize_postgres()
        self._initialized = True

    def record_audit_event(
        self,
        *,
        event_id: str,
        timestamp: str,
        action: str,
        strategy: str | None,
        summary: str,
        request: dict[str, Any],
        response: dict[str, Any],
    ) -> None:
        self.initialize()
        payload_request = json.dumps(request)
        payload_response = json.dumps(response)
        if self.backend == "sqlite":
            with self._sqlite_connection() as connection:
                connection.execute(
                    """
                    INSERT OR REPLACE INTO audit_events
                    (event_id, timestamp, action, strategy, summary, request_json, response_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (event_id, timestamp, action, strategy, summary, payload_request, payload_response),
                )
                connection.commit()
            return

        with self._postgres_cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO audit_events
                (event_id, timestamp, action, strategy, summary, request_json, response_json)
                VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s::jsonb)
                ON CONFLICT (event_id) DO UPDATE
                SET timestamp = EXCLUDED.timestamp,
                    action = EXCLUDED.action,
                    strategy = EXCLUDED.strategy,
                    summary = EXCLUDED.summary,
                    request_json = EXCLUDED.request_json,
                    response_json = EXCLUDED.response_json
                """,
                (event_id, timestamp, action, strategy, summary, payload_request, payload_response),
            )

    def list_audit_events(self, *, limit: int, action: str | None = None) -> list[dict[str, Any]]:
        self.initialize()
        if self.backend == "sqlite":
            with self._sqlite_connection() as connection:
                cursor = connection.execute(
                    """
                    SELECT event_id, timestamp, action, strategy, summary, request_json, response_json
                    FROM audit_events
                    WHERE (? IS NULL OR action = ?)
                    ORDER BY timestamp DESC
                    LIMIT ?
                    """,
                    (action, action, limit),
                )
                rows = cursor.fetchall()
            return [self._decode_audit_row(row) for row in rows]

        with self._postgres_cursor() as cursor:
            if action is None:
                cursor.execute(
                    """
                    SELECT event_id, timestamp, action, strategy, summary, request_json, response_json
                    FROM audit_events
                    ORDER BY timestamp DESC
                    LIMIT %s
                    """,
                    (limit,),
                )
            else:
                cursor.execute(
                    """
                    SELECT event_id, timestamp, action, strategy, summary, request_json, response_json
                    FROM audit_events
                    WHERE action = %s
                    ORDER BY timestamp DESC
                    LIMIT %s
                    """,
                    (action, limit),
                )
            rows = cursor.fetchall()
        return [self._decode_audit_row(row) for row in rows]

    def clear_audit_events(self) -> None:
        self.initialize()
        if self.backend == "sqlite":
            with self._sqlite_connection() as connection:
                connection.execute("DELETE FROM audit_events")
                connection.commit()
            return
        with self._postgres_cursor() as cursor:
            cursor.execute("DELETE FROM audit_events")

    def save_report(
        self,
        *,
        report_id: str,
        report_type: str,
        created_at: str,
        name: str,
        summary: str,
        payload: dict[str, Any],
    ) -> None:
        self.initialize()
        payload_json = json.dumps(payload)
        if self.backend == "sqlite":
            with self._sqlite_connection() as connection:
                connection.execute(
                    """
                    INSERT OR REPLACE INTO benchmark_reports
                    (report_id, report_type, created_at, name, summary, payload_json)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (report_id, report_type, created_at, name, summary, payload_json),
                )
                connection.commit()
            return
        with self._postgres_cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO benchmark_reports
                (report_id, report_type, created_at, name, summary, payload_json)
                VALUES (%s, %s, %s, %s, %s, %s::jsonb)
                ON CONFLICT (report_id) DO UPDATE
                SET report_type = EXCLUDED.report_type,
                    created_at = EXCLUDED.created_at,
                    name = EXCLUDED.name,
                    summary = EXCLUDED.summary,
                    payload_json = EXCLUDED.payload_json
                """,
                (report_id, report_type, created_at, name, summary, payload_json),
            )

    def list_reports(self, *, report_type: str | None = None, limit: int = 20) -> list[dict[str, Any]]:
        self.initialize()
        if self.backend == "sqlite":
            with self._sqlite_connection() as connection:
                cursor = connection.execute(
                    """
                    SELECT report_id, report_type, created_at, name, summary, payload_json
                    FROM benchmark_reports
                    WHERE (? IS NULL OR report_type = ?)
                    ORDER BY created_at DESC
                    LIMIT ?
                    """,
                    (report_type, report_type, limit),
                )
                rows = cursor.fetchall()
            return [self._decode_report_row(row) for row in rows]
        with self._postgres_cursor() as cursor:
            if report_type is None:
                cursor.execute(
                    """
                    SELECT report_id, report_type, created_at, name, summary, payload_json
                    FROM benchmark_reports
                    ORDER BY created_at DESC
                    LIMIT %s
                    """,
                    (limit,),
                )
            else:
                cursor.execute(
                    """
                    SELECT report_id, report_type, created_at, name, summary, payload_json
                    FROM benchmark_reports
                    WHERE report_type = %s
                    ORDER BY created_at DESC
                    LIMIT %s
                    """,
                    (report_type, limit),
                )
            rows = cursor.fetchall()
        return [self._decode_report_row(row) for row in rows]

    def get_report(
        self,
        report_id: str | None = None,
        *,
        latest: bool = False,
        report_type: str | None = None,
    ) -> dict[str, Any] | None:
        self.initialize()
        if self.backend == "sqlite":
            with self._sqlite_connection() as connection:
                if latest:
                    cursor = connection.execute(
                        """
                        SELECT report_id, report_type, created_at, name, summary, payload_json
                        FROM benchmark_reports
                        WHERE (? IS NULL OR report_type = ?)
                        ORDER BY created_at DESC
                        LIMIT 1
                        """,
                        (report_type, report_type),
                    )
                else:
                    cursor = connection.execute(
                        """
                        SELECT report_id, report_type, created_at, name, summary, payload_json
                        FROM benchmark_reports
                        WHERE report_id = ?
                          AND (? IS NULL OR report_type = ?)
                        LIMIT 1
                        """,
                        (report_id, report_type, report_type),
                    )
                row = cursor.fetchone()
            return self._decode_report_row(row) if row else None

        with self._postgres_cursor() as cursor:
            if latest:
                if report_type is None:
                    cursor.execute(
                        """
                        SELECT report_id, report_type, created_at, name, summary, payload_json
                        FROM benchmark_reports
                        ORDER BY created_at DESC
                        LIMIT 1
                        """
                    )
                else:
                    cursor.execute(
                        """
                        SELECT report_id, report_type, created_at, name, summary, payload_json
                        FROM benchmark_reports
                        WHERE report_type = %s
                        ORDER BY created_at DESC
                        LIMIT 1
                        """,
                        (report_type,),
                    )
            else:
                if report_type is None:
                    cursor.execute(
                        """
                        SELECT report_id, report_type, created_at, name, summary, payload_json
                        FROM benchmark_reports
                        WHERE report_id = %s
                        LIMIT 1
                        """,
                        (report_id,),
                    )
                else:
                    cursor.execute(
                        """
                        SELECT report_id, report_type, created_at, name, summary, payload_json
                        FROM benchmark_reports
                        WHERE report_id = %s
                          AND report_type = %s
                        LIMIT 1
                        """,
                        (report_id, report_type),
                    )
            row = cursor.fetchone()
        return self._decode_report_row(row) if row else None

    def _resolve_backend(self) -> str:
        app_database_url = self.settings.app_database_url
        if app_database_url:
            if app_database_url.startswith("postgresql://") or app_database_url.startswith("postgres://"):
                return "postgres"
            return "sqlite"
        if self.settings.database_url and self.settings.data_source == "postgres":
            return "postgres"
        return "sqlite"

    def _resolve_location(self) -> str:
        if self.backend == "postgres":
            return self.settings.app_database_url or self.settings.database_url or ""
        sqlite_path = self.settings.app_db_path or str(Path(".data") / "uto_app.db")
        return sqlite_path

    def _initialize_sqlite(self) -> None:
        path = Path(self.location)
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._sqlite_connection() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    migration_id TEXT PRIMARY KEY,
                    applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            applied = {
                row[0]
                for row in connection.execute("SELECT migration_id FROM schema_migrations").fetchall()
            }
            migrations_dir = Path(__file__).resolve().parent / "migrations" / "sqlite"
            for migration_path in sorted(migrations_dir.glob("*.sql")):
                if migration_path.name in applied:
                    continue
                connection.executescript(migration_path.read_text())
                connection.execute(
                    "INSERT INTO schema_migrations (migration_id) VALUES (?)",
                    (migration_path.name,),
                )
            connection.commit()

    def _initialize_postgres(self) -> None:
        with self._postgres_cursor() as cursor:
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    migration_id TEXT PRIMARY KEY,
                    applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
                """
            )
            cursor.execute("SELECT migration_id FROM schema_migrations")
            applied = {row[0] for row in cursor.fetchall()}
            migrations_dir = Path(__file__).resolve().parent / "migrations" / "postgres"
            for migration_path in sorted(migrations_dir.glob("*.sql")):
                if migration_path.name in applied:
                    continue
                cursor.execute(migration_path.read_text())
                cursor.execute(
                    "INSERT INTO schema_migrations (migration_id) VALUES (%s)",
                    (migration_path.name,),
                )

    @contextmanager
    def _postgres_cursor(self) -> Iterator[Any]:
        import psycopg
        from psycopg.rows import tuple_row

        with psycopg.connect(self.location, row_factory=tuple_row) as connection:
            with connection.cursor() as cursor:
                yield cursor
            connection.commit()

    @contextmanager
    def _sqlite_connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.location)
        try:
            yield connection
        finally:
            connection.close()

    @staticmethod
    def _decode_audit_row(row: Any) -> dict[str, Any]:
        event_id, timestamp, action, strategy, summary, request_json, response_json = row
        return {
            "event_id": event_id,
            "timestamp": str(timestamp),
            "action": action,
            "strategy": strategy,
            "summary": summary,
            "request": json.loads(request_json) if isinstance(request_json, str) else request_json,
            "response": json.loads(response_json) if isinstance(response_json, str) else response_json,
        }

    @staticmethod
    def _decode_report_row(row: Any) -> dict[str, Any]:
        report_id, report_type, created_at, name, summary, payload_json = row
        return {
            "report_id": report_id,
            "report_type": report_type,
            "created_at": str(created_at),
            "name": name,
            "summary": summary,
            "payload": json.loads(payload_json) if isinstance(payload_json, str) else payload_json,
        }

