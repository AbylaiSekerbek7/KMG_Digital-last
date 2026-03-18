#!/bin/sh
set -eu

psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" <<'SQL'
TRUNCATE TABLE compatibility, tasks, vehicles, wells, road_edges, road_nodes;
\copy road_nodes(node_id, lon, lat) FROM '/seed-data/road_nodes.csv' WITH (FORMAT csv, HEADER true);
\copy road_edges(source, target, weight) FROM '/seed-data/road_edges.csv' WITH (FORMAT csv, HEADER true);
\copy wells(uwi, longitude, latitude, well_name, nearest_node_id) FROM '/seed-data/wells.csv' WITH (FORMAT csv, HEADER true);
\copy vehicles(vehicle_id, name, vehicle_type, current_node, lon, lat, available_at, avg_speed_kmph, skills, registration_plate) FROM '/seed-data/vehicles.csv' WITH (FORMAT csv, HEADER true);
\copy tasks(task_id, priority, planned_start, start_day, planned_duration_hours, destination_uwi, task_type, shift) FROM '/seed-data/tasks.csv' WITH (FORMAT csv, HEADER true);
\copy compatibility(task_type, vehicle_type) FROM '/seed-data/compatibility.csv' WITH (FORMAT csv, HEADER true);
SQL
