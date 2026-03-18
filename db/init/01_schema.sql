CREATE TABLE IF NOT EXISTS road_nodes (
    node_id INTEGER PRIMARY KEY,
    lon DOUBLE PRECISION NOT NULL,
    lat DOUBLE PRECISION NOT NULL
);

CREATE TABLE IF NOT EXISTS road_edges (
    source INTEGER NOT NULL,
    target INTEGER NOT NULL,
    weight DOUBLE PRECISION NOT NULL
);

CREATE TABLE IF NOT EXISTS wells (
    uwi TEXT PRIMARY KEY,
    longitude DOUBLE PRECISION NOT NULL,
    latitude DOUBLE PRECISION NOT NULL,
    well_name TEXT NOT NULL,
    nearest_node_id INTEGER
);

CREATE TABLE IF NOT EXISTS vehicles (
    vehicle_id BIGINT PRIMARY KEY,
    name TEXT NOT NULL,
    vehicle_type TEXT NOT NULL,
    current_node INTEGER,
    lon DOUBLE PRECISION NOT NULL,
    lat DOUBLE PRECISION NOT NULL,
    available_at TIMESTAMP NOT NULL,
    avg_speed_kmph DOUBLE PRECISION NOT NULL,
    skills TEXT,
    registration_plate TEXT
);

CREATE TABLE IF NOT EXISTS tasks (
    task_id TEXT PRIMARY KEY,
    priority TEXT NOT NULL,
    planned_start TIMESTAMP NOT NULL,
    start_day DATE,
    planned_duration_hours DOUBLE PRECISION NOT NULL,
    destination_uwi TEXT NOT NULL,
    task_type TEXT NOT NULL,
    shift TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS compatibility (
    task_type TEXT NOT NULL,
    vehicle_type TEXT NOT NULL
);
