from __future__ import annotations

import csv
import json
import logging
import math
import os
import random
import re
from collections import defaultdict
from datetime import datetime, time as time_cls, timedelta
from pathlib import Path
from typing import Any

from uto_routing.config import RuntimeSettings, get_settings
from uto_routing.models import (
    Dataset,
    Edge,
    Node,
    Priority,
    Shift,
    Task,
    Vehicle,
    Well,
    resolve_start_day,
)
from uto_routing.sample_data import create_sample_dataset

logger = logging.getLogger(__name__)


REQUIRED_BASENAMES = [
    "road_nodes",
    "road_edges",
    "wells",
    "vehicles",
    "tasks",
    "compatibility",
]


SAFE_TABLE_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]*$")


def load_dataset(
    data_dir: str | None = None,
    *,
    settings: RuntimeSettings | None = None,
) -> Dataset:
    runtime = settings or get_settings()
    if data_dir is not None:
        return load_directory_dataset(Path(data_dir))
    if runtime.data_source == "sample":
        return create_sample_dataset()
    if runtime.data_source == "directory":
        if not runtime.data_dir:
            raise ValueError("UTO_DATA_DIR is required when UTO_DATA_SOURCE=directory")
        return load_directory_dataset(Path(runtime.data_dir))
    if runtime.data_source == "postgres":
        return load_postgres_dataset(runtime)
    if runtime.data_source == "hackathon_db":
        return load_hackathon_db_dataset(runtime)
    raise ValueError(f"Unsupported data source: {runtime.data_source}")


def load_directory_dataset(data_dir: Path) -> Dataset:
    if not data_dir.exists():
        raise FileNotFoundError(f"Dataset directory not found: {data_dir}")

    records = {basename: _load_records(data_dir, basename) for basename in REQUIRED_BASENAMES}

    nodes = [
        Node(
            node_id=int(record["node_id"]),
            lon=float(record["lon"]),
            lat=float(record["lat"]),
        )
        for record in records["road_nodes"]
    ]
    edges = [
        Edge(
            source=int(record["source"]),
            target=int(record["target"]),
            weight_m=float(record["weight"] if "weight" in record else record["weight_m"]),
        )
        for record in records["road_edges"]
    ]
    wells = [
        Well(
            uwi=str(record["uwi"]),
            lon=float(record["longitude"] if "longitude" in record else record["lon"]),
            lat=float(record["latitude"] if "latitude" in record else record["lat"]),
            well_name=str(record.get("well_name", record["uwi"])),
            nearest_node_id=(
                int(record["nearest_node_id"]) if record.get("nearest_node_id") not in (None, "") else None
            ),
        )
        for record in records["wells"]
    ]

    compatibility = _parse_compatibility(records["compatibility"])
    vehicles = []
    for record in records["vehicles"]:
        skills = _parse_skills(record.get("skills"))
        vehicle_type = str(record["vehicle_type"])
        if not skills:
            skills = {task_type for task_type, vehicle_types in compatibility.items() if vehicle_type in vehicle_types}
        vehicles.append(
            Vehicle(
                vehicle_id=int(record["vehicle_id"]),
                name=str(record["name"]),
                vehicle_type=vehicle_type,
                current_node=int(record["current_node"]),
                lon=float(record["lon"]),
                lat=float(record["lat"]),
                available_at=_parse_datetime(record["available_at"]),
                avg_speed_kmph=float(record["avg_speed_kmph"]),
                skills=skills,
                registration_plate=record.get("registration_plate"),
            )
        )

    tasks = [
        Task(
            task_id=str(record["task_id"]),
            priority=Priority(str(record["priority"]).lower()),
            planned_start=(planned_start := _parse_datetime(record["planned_start"])),
            planned_duration_hours=float(record["planned_duration_hours"]),
            destination_uwi=str(record["destination_uwi"]),
            task_type=str(record["task_type"]),
            shift=(shift := Shift(str(record["shift"]).lower())),
            start_day=resolve_start_day(planned_start, shift, record.get("start_day")),
        )
        for record in records["tasks"]
    ]

    return Dataset(
        nodes=nodes,
        edges=edges,
        wells=wells,
        vehicles=vehicles,
        tasks=tasks,
        compatibility=compatibility,
        metadata={
            "dataset_mode": "directory",
            "dataset_path": str(data_dir),
        },
    )


def load_postgres_dataset(settings: RuntimeSettings) -> Dataset:
    if not settings.database_url:
        raise ValueError("UTO_DATABASE_URL is required when UTO_DATA_SOURCE=postgres")

    try:
        import psycopg
        from psycopg.rows import dict_row
    except ImportError as exc:  # pragma: no cover - runtime dependency guard
        raise RuntimeError("psycopg is not installed. Add it to runtime dependencies.") from exc

    nodes_query = f"SELECT node_id, lon, lat FROM {_validate_table(settings.pg_table_road_nodes)} ORDER BY node_id"
    edges_query = (
        f"SELECT source, target, weight "
        f"FROM {_validate_table(settings.pg_table_road_edges)} ORDER BY source, target"
    )
    wells_query = (
        f"SELECT uwi, longitude, latitude, well_name, nearest_node_id "
        f"FROM {_validate_table(settings.pg_table_wells)} ORDER BY uwi"
    )
    vehicles_query = (
        f"SELECT vehicle_id, name, vehicle_type, current_node, lon, lat, available_at, avg_speed_kmph, "
        f"skills, registration_plate "
        f"FROM {_validate_table(settings.pg_table_vehicles)} ORDER BY vehicle_id"
    )
    tasks_query = (
        f"SELECT task_id, priority, planned_start, start_day, planned_duration_hours, "
        f"destination_uwi, task_type, shift "
        f"FROM {_validate_table(settings.pg_table_tasks)} ORDER BY planned_start, task_id"
    )
    compatibility_query = (
        f"SELECT task_type, vehicle_type FROM {_validate_table(settings.pg_table_compatibility)} "
        f"ORDER BY task_type, vehicle_type"
    )

    with psycopg.connect(settings.database_url, row_factory=dict_row) as connection:
        with connection.cursor() as cursor:
            nodes_records = cursor.execute(nodes_query).fetchall()
            edges_records = cursor.execute(edges_query).fetchall()
            wells_records = cursor.execute(wells_query).fetchall()
            compatibility_records = cursor.execute(compatibility_query).fetchall()
            vehicles_records = cursor.execute(vehicles_query).fetchall()
            tasks_records = cursor.execute(tasks_query).fetchall()

    nodes = [
        Node(
            node_id=int(record["node_id"]),
            lon=float(record["lon"]),
            lat=float(record["lat"]),
        )
        for record in nodes_records
    ]
    node_lookup = {node.node_id: node for node in nodes}
    edges = [
        Edge(
            source=int(record["source"]),
            target=int(record["target"]),
            weight_m=float(record["weight"]),
        )
        for record in edges_records
    ]
    wells = [
        Well(
            uwi=str(record["uwi"]),
            lon=float(record["longitude"]),
            lat=float(record["latitude"]),
            well_name=str(record["well_name"]),
            nearest_node_id=_resolve_nearest_node_id(
                nodes=node_lookup,
                candidate=record.get("nearest_node_id"),
                lon=float(record["longitude"]),
                lat=float(record["latitude"]),
            ),
        )
        for record in wells_records
    ]

    compatibility = _parse_compatibility(compatibility_records)
    vehicles = []
    for record in vehicles_records:
        skills = _parse_skills(record.get("skills"))
        vehicle_type = str(record["vehicle_type"])
        if not skills:
            skills = {task_type for task_type, vehicle_types in compatibility.items() if vehicle_type in vehicle_types}
        lon = float(record["lon"])
        lat = float(record["lat"])
        vehicles.append(
            Vehicle(
                vehicle_id=int(record["vehicle_id"]),
                name=str(record["name"]),
                vehicle_type=vehicle_type,
                current_node=_resolve_nearest_node_id(
                    nodes=node_lookup,
                    candidate=record.get("current_node"),
                    lon=lon,
                    lat=lat,
                ),
                lon=lon,
                lat=lat,
                available_at=_parse_datetime(record["available_at"]),
                avg_speed_kmph=float(record["avg_speed_kmph"]),
                skills=skills,
                registration_plate=record.get("registration_plate"),
            )
        )

    tasks = []
    for record in tasks_records:
        planned_start = _parse_datetime(record["planned_start"])
        shift = Shift(str(record["shift"]).lower())
        tasks.append(
            Task(
                task_id=str(record["task_id"]),
                priority=Priority(str(record["priority"]).lower()),
                planned_start=planned_start,
                planned_duration_hours=float(record["planned_duration_hours"]),
                destination_uwi=str(record["destination_uwi"]),
                task_type=str(record["task_type"]),
                shift=shift,
                start_day=resolve_start_day(planned_start, shift, record.get("start_day")),
            )
        )

    return Dataset(
        nodes=nodes,
        edges=edges,
        wells=wells,
        vehicles=vehicles,
        tasks=tasks,
        compatibility=compatibility,
        metadata={
            "dataset_mode": "postgres",
            "database_url": _redact_database_url(settings.database_url),
        },
    )


# ---------------------------------------------------------------------------
# Hackathon DB loader — KMG mock_uto database (real organizer data)
# ---------------------------------------------------------------------------

# Priority list_values IDs in the organizer DB
_HACKATHON_PRIORITY_MAP: dict[int, str] = {11: "high", 12: "medium", 13: "low"}

# Shift list_values IDs in the organizer DB
_HACKATHON_SHIFT_MAP: dict[int, str] = {16: "day", 17: "night"}

# Typical average speeds (km/h) for oilfield vehicle types
_VEHICLE_SPEED_MAP: dict[str, float] = {
    "ЦА-320": 22.0, "АНЦ-32/50": 21.0, "АСЦ-320": 20.0,
    "K-700": 15.0, "Автокран 50т.": 18.0, "Автокран КС45717К2": 18.0,
    "Бульдозер": 10.0, "JAC": 25.0, "Пикап": 30.0,
    "БАРС-80": 20.0, "БМ-70К": 18.0, "АКС": 20.0,
    "XJ350/XJ900": 15.0, "XJ450": 15.0, "АЦН-10С": 22.0,
    "Автосварка": 20.0, "СД-9/101": 18.0, "2СМ-20": 18.0,
    "Автобус": 35.0,
}

# Patterns to strip test garbage from vehicle type names
_VEHTYPE_NOISE_RE = re.compile(r"\s*(TEEEE+St?|Test)\s*$", re.IGNORECASE)


def load_hackathon_db_dataset(settings: RuntimeSettings) -> Dataset:
    """Load from the KMG hackathon database (mock_uto).

    The organizer database stores data in a normalized EAV schema:
    - references.road_nodes / road_edges — road graph
    - references.wells — destination points
    - references.wialon_units_snapshot_1/2/3 — vehicle telemetry snapshots
    - dcm.records + dcm.record_indicator_values — orders (tasks)
    - dct.elements — dictionaries for vehicle kinds, work types, wells

    This function extracts, transforms and loads all data into our Dataset model.
    """
    if not settings.database_url:
        raise ValueError("UTO_DATABASE_URL is required when UTO_DATA_SOURCE=hackathon_db")

    try:
        import psycopg
        from psycopg.rows import dict_row
    except ImportError as exc:
        raise RuntimeError("psycopg is not installed.") from exc

    with psycopg.connect(settings.database_url, row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            # ---- 1. Road graph ----
            nodes_rows = cur.execute(
                'SELECT node_id, lon, lat FROM "references".road_nodes ORDER BY node_id'
            ).fetchall()
            edges_rows = cur.execute(
                'SELECT source, target, weight FROM "references".road_edges ORDER BY source, target'
            ).fetchall()

            # ---- 2. Wells ----
            wells_rows = cur.execute(
                'SELECT uwi, longitude, latitude, well_name FROM "references".wells '
                "WHERE latitude IS NOT NULL AND longitude IS NOT NULL ORDER BY uwi"
            ).fetchall()

            # ---- 3. Wialon snapshots (vehicles) ----
            snap3_rows = cur.execute(
                'SELECT wialon_id, nm, pos_t, pos_y, pos_x, registration_plate '
                'FROM "references".wialon_units_snapshot_3 '
                "WHERE pos_x IS NOT NULL AND pos_y IS NOT NULL ORDER BY wialon_id"
            ).fetchall()
            snap1_rows = cur.execute(
                'SELECT wialon_id, pos_t, pos_y, pos_x '
                'FROM "references".wialon_units_snapshot_1 '
                "WHERE pos_x IS NOT NULL AND pos_y IS NOT NULL ORDER BY wialon_id"
            ).fetchall()

            # ---- 4. Orders from EAV (dcm) ----
            order_rows = cur.execute(
                """
                SELECT r.id AS record_id, r.number, r.date,
                       i.code AS indicator_code,
                       riv.value_str, riv.value_int, riv.value_float,
                       riv.value_datetime, riv.value_reference,
                       riv.value_text, riv.value_json, riv.value_bool
                FROM dcm.records r
                JOIN dcm.record_indicator_values riv
                     ON riv.record_id = r.id AND riv.deleted_at IS NULL
                JOIN dcm.indicators i ON i.id = riv.indicator_id
                WHERE r.document_id = 2 AND r.is_deleted = false
                ORDER BY r.id, i.code
                """
            ).fetchall()

    # ---- Process nodes & edges ----
    nodes = [
        Node(node_id=int(r["node_id"]), lon=float(r["lon"]), lat=float(r["lat"]))
        for r in nodes_rows
    ]
    # The organizer DB stores edges as directed, but roads are bidirectional.
    # Add reverse edges to make the graph undirected.
    edge_set: set[tuple[int, int]] = set()
    edges: list[Edge] = []
    for r in edges_rows:
        src, tgt, w = int(r["source"]), int(r["target"]), float(r["weight"])
        if (src, tgt) not in edge_set:
            edges.append(Edge(source=src, target=tgt, weight_m=w))
            edge_set.add((src, tgt))
        if (tgt, src) not in edge_set:
            edges.append(Edge(source=tgt, target=src, weight_m=w))
            edge_set.add((tgt, src))

    # ---- Process wells ----
    wells: list[Well] = []
    for r in wells_rows:
        wells.append(
            Well(
                uwi=str(r["uwi"]),
                lon=float(r["longitude"]),
                lat=float(r["latitude"]),
                well_name=str(r["well_name"] or r["uwi"]),
            )
        )

    # Build a lookup for matching order wells → reference wells
    well_name_index: dict[str, Well] = {}
    for w in wells:
        well_name_index[w.well_name] = w
        # Also index short forms, e.g. "G_4416/28" without suffixes
        base = w.well_name.split(" ")[0]
        if base not in well_name_index:
            well_name_index[base] = w

    # ---- Compute average speed from wialon snapshot deltas ----
    snap1_lookup = {r["wialon_id"]: r for r in snap1_rows}
    wialon_speeds: dict[int, float] = {}
    for s3 in snap3_rows:
        s1 = snap1_lookup.get(s3["wialon_id"])
        if s1 and s3["pos_t"] and s1["pos_t"] and s3["pos_t"] != s1["pos_t"]:
            dist_deg = math.sqrt(
                (float(s3["pos_x"]) - float(s1["pos_x"])) ** 2
                + (float(s3["pos_y"]) - float(s1["pos_y"])) ** 2
            )
            dist_m = dist_deg * 111_000
            dt_hours = abs(int(s3["pos_t"]) - int(s1["pos_t"])) / 3600.0
            if dt_hours > 0:
                wialon_speeds[s3["wialon_id"]] = round(dist_m / 1000.0 / dt_hours, 1)
    median_speed = sorted(wialon_speeds.values())[len(wialon_speeds) // 2] if wialon_speeds else 20.0

    # ---- Extract tasks from EAV orders ----
    order_data: dict[int, dict[str, Any]] = defaultdict(dict)
    for row in order_rows:
        rid = row["record_id"]
        if "_meta" not in order_data[rid]:
            order_data[rid]["_meta"] = {"number": row["number"], "date": row["date"]}
        order_data[rid][row["indicator_code"]] = {
            "str": row["value_str"],
            "int": row["value_int"],
            "float": row["value_float"],
            "dt": row["value_datetime"],
            "ref": row["value_reference"],
            "text": row["value_text"],
            "json": row["value_json"],
            "bool": row["value_bool"],
        }

    compatibility: dict[str, set[str]] = defaultdict(set)
    tasks: list[Task] = []
    seen_task_ids: set[str] = set()

    for rid, data in order_data.items():
        meta = data.get("_meta", {})

        # Priority
        pry_ref = (data.get("TRS_ORDER_PRY") or {}).get("ref")
        priority_str = _HACKATHON_PRIORITY_MAP.get(pry_ref, "medium")

        # Shift
        shift_ref = (data.get("TRS_ORDER_SHIFT") or {}).get("ref")
        shift_str = _HACKATHON_SHIFT_MAP.get(shift_ref, "day")

        # Date
        date_val = (data.get("TRS_ORDER_DATE") or {}).get("dt")
        if not date_val:
            date_val = meta.get("date")
        if not date_val:
            continue

        # Duration (hours)
        hours_val = (data.get("TRS_ORDER_HOURS") or {}).get("int")
        if not hours_val or hours_val <= 0:
            hours_val = 4

        # Well — resolve from 1C JSON description
        well1c_json = (data.get("TRS_ORDER_WELL1C") or {}).get("json")
        well_desc = _extract_1c_description(well1c_json)
        matched_well = _match_well(well_desc, well_name_index) if well_desc else None
        if not matched_well:
            logger.debug("Order %s: could not resolve well '%s', skipping", rid, well_desc)
            continue

        # Work type
        wkind_json = (data.get("TRS_ORDER_WKIND1C") or {}).get("json")
        work_type = _extract_1c_description(wkind_json) or "Прочие виды работ."

        # Vehicle kind
        vehkind_json = (data.get("TRS_ORDER_VEHKIND1C") or {}).get("json")
        vehicle_kind = _clean_vehicle_type(_extract_1c_description(vehkind_json) or "Спецтехника")

        # Build compatibility mapping
        compatibility[work_type].add(vehicle_kind)

        # Build planned_start datetime
        if isinstance(date_val, datetime):
            ps = date_val.replace(tzinfo=None) if date_val.tzinfo else date_val
        else:
            shift_hour = 8 if shift_str == "day" else 20
            ps = datetime.combine(date_val, time_cls(shift_hour, 0))

        task_id = f"ORD-{meta.get('number', rid)}"
        if task_id in seen_task_ids:
            task_id = f"{task_id}-{rid}"
        seen_task_ids.add(task_id)

        shift_enum = Shift(shift_str)
        tasks.append(
            Task(
                task_id=task_id,
                priority=Priority(priority_str),
                planned_start=ps,
                planned_duration_hours=float(hours_val),
                destination_uwi=matched_well.uwi,
                task_type=work_type,
                shift=shift_enum,
                start_day=resolve_start_day(ps, shift_enum),
            )
        )

    # ---- Build vehicle fleet ----
    # The wialon snapshots contain vehicles from a different geographic area,
    # so we place vehicles at well positions within the road graph.
    # Per the TZ: "Если чего-то не хватает — сгенерировать по правдоподобным шаблонам."
    rng = random.Random(42)
    vehicle_types_needed = set()
    for task_type_veh_types in compatibility.values():
        vehicle_types_needed.update(task_type_veh_types)
    if not vehicle_types_needed:
        vehicle_types_needed = {"Спецтехника"}

    well_positions = [(w.lon, w.lat) for w in wells if w.lon and w.lat]
    rng.shuffle(well_positions)
    wialon_ids = [r["wialon_id"] for r in snap3_rows]
    wialon_names = {r["wialon_id"]: r["nm"] for r in snap3_rows}
    wialon_plates = {r["wialon_id"]: r["registration_plate"] for r in snap3_rows}

    # Aim for ~52 vehicles as per TZ (52 units on Жетыбай field)
    target_fleet_size = 52
    vehicles_per_type = max(1, target_fleet_size // max(1, len(vehicle_types_needed)))

    base_time = min((t.planned_start for t in tasks), default=datetime(2025, 8, 1, 8, 0))
    vehicles: list[Vehicle] = []
    vid_counter = 0

    for vtype in sorted(vehicle_types_needed):
        for i in range(vehicles_per_type):
            if vid_counter < len(wialon_ids):
                vid = wialon_ids[vid_counter]
                plate = wialon_plates.get(vid)
                name = f"{vtype} ({plate or vid})"
            else:
                vid = 90_000 + vid_counter
                plate = None
                name = f"{vtype}-{i + 1}"

            pos = well_positions[vid_counter % len(well_positions)]
            speed = _VEHICLE_SPEED_MAP.get(vtype, median_speed) + rng.uniform(-2.0, 2.0)
            available_offset = rng.randint(0, 120)

            vehicles.append(
                Vehicle(
                    vehicle_id=vid,
                    name=name,
                    vehicle_type=vtype,
                    current_node=-1,  # Will be resolved by _normalize_dataset via snap_to_node
                    lon=pos[0],
                    lat=pos[1],
                    available_at=base_time + timedelta(minutes=available_offset),
                    avg_speed_kmph=round(max(5.0, speed), 1),
                    skills={tt for tt, vtypes in compatibility.items() if vtype in vtypes},
                    registration_plate=plate,
                )
            )
            vid_counter += 1

    logger.info(
        "Hackathon DB loaded: %d nodes, %d edges, %d wells, %d tasks (from %d orders), %d vehicles",
        len(nodes), len(edges), len(wells), len(tasks), len(order_data), len(vehicles),
    )

    return Dataset(
        nodes=nodes,
        edges=edges,
        wells=wells,
        vehicles=vehicles,
        tasks=tasks,
        compatibility=dict(compatibility),
        metadata={
            "dataset_mode": "hackathon_db",
            "database_url": _redact_database_url(settings.database_url),
            "source_orders": str(len(order_data)),
            "resolved_tasks": str(len(tasks)),
            "wialon_units": str(len(snap3_rows)),
            "generated_vehicles": str(len(vehicles)),
        },
    )


def _extract_1c_description(json_str: Any) -> str | None:
    """Extract Description field from a 1C-style JSON string."""
    if not json_str:
        return None
    m = re.search(r'Description["\'"]+:\s*["\'"]+([^"\'"]+)', str(json_str))
    return m.group(1).strip() if m else None


def _clean_vehicle_type(raw: str) -> str:
    """Strip test garbage suffixes from vehicle type names."""
    return _VEHTYPE_NOISE_RE.sub("", raw).strip()


def _match_well(desc: str, well_index: dict[str, Well]) -> Well | None:
    """Match an order well description to a references.wells entry."""
    if desc in well_index:
        return well_index[desc]

    # Try base form (before first space, stripping suffixes like "доб." "нагн.")
    base = desc.split(" ")[0]
    if base in well_index:
        return well_index[base]

    # Try with G_ prefix
    if not desc.startswith("G_") and not desc.startswith("K_"):
        with_prefix = f"G_{base}"
        if with_prefix in well_index:
            return well_index[with_prefix]

    # Try stripping G_ or K_ prefix (e.g., "G_526/12" → "526/12")
    for prefix in ("G_", "K_"):
        if base.startswith(prefix):
            stripped = base[len(prefix):]
            if stripped in well_index:
                return well_index[stripped]

    # Fuzzy: check if any indexed name contains the base or stripped form
    candidates = [base]
    for prefix in ("G_", "K_"):
        if base.startswith(prefix):
            candidates.append(base[len(prefix):])
    for candidate in candidates:
        if len(candidate) < 4:
            continue
        for wname, well in well_index.items():
            if wname and candidate in wname:
                return well

    return None


def dataset_summary(dataset: Dataset) -> dict[str, Any]:
    return {
        "mode": dataset.metadata.get("dataset_mode", "unknown"),
        "nodes": len(dataset.nodes),
        "edges": len(dataset.edges),
        "wells": len(dataset.wells),
        "vehicles": len(dataset.vehicles),
        "tasks": len(dataset.tasks),
        "task_types": sorted(dataset.compatibility.keys()),
        "vehicle_types": sorted({vehicle.vehicle_type for vehicle in dataset.vehicles}),
    }


def _load_records(data_dir: Path, basename: str) -> list[dict[str, Any]]:
    for suffix in (".json", ".csv"):
        path = data_dir / f"{basename}{suffix}"
        if not path.exists():
            continue
        if suffix == ".json":
            loaded = json.loads(path.read_text())
            if isinstance(loaded, dict) and "records" in loaded:
                return list(loaded["records"])
            if isinstance(loaded, list):
                return loaded
            raise ValueError(f"Unsupported JSON structure in {path}")
        with path.open(newline="", encoding="utf-8") as handle:
            return list(csv.DictReader(handle))
    raise FileNotFoundError(
        f"Expected {basename}.json or {basename}.csv in {data_dir}"
    )


def _parse_compatibility(records: list[dict[str, Any]]) -> dict[str, set[str]]:
    compatibility: dict[str, set[str]] = {}
    for record in records:
        task_type = str(record["task_type"])
        vehicle_type = str(record["vehicle_type"])
        compatibility.setdefault(task_type, set()).add(vehicle_type)
    return compatibility


def _parse_skills(raw_value: Any) -> set[str]:
    if raw_value in (None, ""):
        return set()
    if isinstance(raw_value, list):
        return {str(item) for item in raw_value}
    return {item.strip() for item in str(raw_value).split("|") if item.strip()}


def _parse_datetime(raw_value: Any) -> datetime:
    if isinstance(raw_value, datetime):
        return raw_value
    return datetime.fromisoformat(str(raw_value))


def _validate_table(table_name: str) -> str:
    if not SAFE_TABLE_PATTERN.match(table_name):
        raise ValueError(f"Unsafe table name: {table_name}")
    return table_name


def _resolve_nearest_node_id(
    *,
    nodes: dict[int, Node],
    candidate: Any,
    lon: float,
    lat: float,
) -> int:
    if candidate not in (None, ""):
        return int(candidate)
    best_node_id = -1
    best_distance = float("inf")
    for node_id, node in nodes.items():
        distance = (node.lon - lon) ** 2 + (node.lat - lat) ** 2
        if distance < best_distance:
            best_distance = distance
            best_node_id = node_id
    if best_node_id == -1:
        raise ValueError("Could not resolve nearest node.")
    return best_node_id


def _redact_database_url(database_url: str) -> str:
    if "@" not in database_url:
        return database_url
    prefix, suffix = database_url.split("@", 1)
    if "://" not in prefix:
        return f"***@{suffix}"
    scheme, _credentials = prefix.split("://", 1)
    return f"{scheme}://***@{suffix}"

