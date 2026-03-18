CREATE TABLE IF NOT EXISTS schema_migrations (
    migration_id TEXT PRIMARY KEY,
    applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS audit_events (
    event_id TEXT PRIMARY KEY,
    timestamp TEXT NOT NULL,
    action TEXT NOT NULL,
    strategy TEXT NULL,
    summary TEXT NOT NULL,
    request_json TEXT NOT NULL,
    response_json TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_audit_events_action_timestamp
    ON audit_events (action, timestamp DESC);

CREATE TABLE IF NOT EXISTS benchmark_reports (
    report_id TEXT PRIMARY KEY,
    report_type TEXT NOT NULL,
    created_at TEXT NOT NULL,
    name TEXT NOT NULL,
    summary TEXT NOT NULL,
    payload_json TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_benchmark_reports_type_created
    ON benchmark_reports (report_type, created_at DESC);
