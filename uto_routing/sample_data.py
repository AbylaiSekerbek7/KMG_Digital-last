from __future__ import annotations

import math
import random
from datetime import datetime, timedelta

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


TASK_TYPES = [
    "acidizing",
    "cementing",
    "inspection",
    "transport",
]

VEHICLE_TYPES = {
    "acidizing": ["ACN-12", "CA-320"],
    "cementing": ["CA-320", "Crane"],
    "inspection": ["Pickup", "Crane", "Tractor"],
    "transport": ["Tractor", "Pickup"],
}


def create_sample_dataset(seed: int = 7) -> Dataset:
    rng = random.Random(seed)
    nodes = _build_grid_nodes(size=6, lon0=68.05, lat0=51.62, step=0.008)
    edges = _build_grid_edges(nodes, size=6)

    well_node_ids = [2, 5, 8, 12, 17, 20, 23, 26, 29, 31, 34]
    wells = []
    for index, node_id in enumerate(well_node_ids, start=1):
        node = next(node for node in nodes if node.node_id == node_id)
        wells.append(
            Well(
                uwi=f"05-1200-{500 + index}",
                lon=node.lon,
                lat=node.lat,
                well_name=f"Well-{index:02d}",
                nearest_node_id=node_id,
            )
        )

    base_time = datetime(2026, 3, 17, 8, 0, 0)
    vehicles = [
        Vehicle(
            vehicle_id=10_200 + idx,
            name=name,
            vehicle_type=vehicle_type,
            current_node=node_id,
            lon=_node_lookup(nodes)[node_id].lon,
            lat=_node_lookup(nodes)[node_id].lat,
            available_at=base_time + timedelta(minutes=busy_for),
            avg_speed_kmph=speed,
            skills={task_type for task_type, types in VEHICLE_TYPES.items() if vehicle_type in types},
            registration_plate=f"X{idx:03d}YY",
        )
        for idx, (name, vehicle_type, node_id, busy_for, speed) in enumerate(
            [
                ("ACN-12 A045KM", "ACN-12", 1, 0, 24.0),
                ("CA-320 B112OR", "CA-320", 9, 25, 23.0),
                ("TRACTOR C330MN", "Tractor", 15, 0, 20.0),
                ("CRANE D704QP", "Crane", 21, 55, 18.0),
                ("PICKUP E910AA", "Pickup", 27, 0, 28.0),
                ("ACN-12 F221BB", "ACN-12", 30, 90, 22.0),
            ],
            start=1,
        )
    ]

    tasks = []
    for index, well in enumerate(wells[:9], start=1):
        task_type = TASK_TYPES[index % len(TASK_TYPES)]
        priority = [Priority.HIGH, Priority.MEDIUM, Priority.LOW][index % 3]
        shift = Shift.DAY if index <= 6 else Shift.NIGHT
        planned_start = base_time + timedelta(minutes=45 * index)
        if shift is Shift.NIGHT:
            planned_start = datetime(2026, 3, 17, 20, 0, 0) + timedelta(minutes=35 * index)
        tasks.append(
            Task(
                task_id=f"T-2026-{index:04d}",
                priority=priority,
                planned_start=planned_start,
                planned_duration_hours=round(rng.uniform(2.0, 5.5), 1),
                destination_uwi=well.uwi,
                task_type=task_type,
                shift=shift,
                start_day=resolve_start_day(planned_start, shift),
            )
        )

    compatibility = {
        task_type: set(vehicle_types) for task_type, vehicle_types in VEHICLE_TYPES.items()
    }
    return Dataset(
        nodes=nodes,
        edges=edges,
        wells=wells,
        vehicles=vehicles,
        tasks=tasks,
        compatibility=compatibility,
        metadata={
            "dataset_mode": "sample",
            "description": "Deterministic synthetic oilfield routing scenario.",
        },
    )


def _build_grid_nodes(size: int, lon0: float, lat0: float, step: float) -> list[Node]:
    nodes = []
    node_id = 1
    for row in range(size):
        for col in range(size):
            nodes.append(
                Node(
                    node_id=node_id,
                    lon=lon0 + step * col,
                    lat=lat0 + step * row,
                )
            )
            node_id += 1
    return nodes


def _build_grid_edges(nodes: list[Node], size: int) -> list[Edge]:
    node_map = _node_lookup(nodes)
    edges: list[Edge] = []
    for row in range(size):
        for col in range(size):
            current = row * size + col + 1
            neighbors = []
            if col + 1 < size:
                neighbors.append(current + 1)
            if row + 1 < size:
                neighbors.append(current + size)
            for neighbor in neighbors:
                weight_m = _distance_between(node_map[current], node_map[neighbor])
                edges.append(Edge(source=current, target=neighbor, weight_m=weight_m))
                edges.append(Edge(source=neighbor, target=current, weight_m=weight_m))
    return edges


def _distance_between(start: Node, end: Node) -> float:
    return math.dist((start.lon, start.lat), (end.lon, end.lat)) * 111_000


def _node_lookup(nodes: list[Node]) -> dict[int, Node]:
    return {node.node_id: node for node in nodes}

