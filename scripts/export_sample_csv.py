from __future__ import annotations

import csv
import sys
from pathlib import Path

from uto_routing.sample_data import create_sample_dataset


def main() -> None:
    output_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("sample_dataset_csv")
    output_dir.mkdir(parents=True, exist_ok=True)
    dataset = create_sample_dataset()

    write_csv(
        output_dir / "road_nodes.csv",
        ["node_id", "lon", "lat"],
        (
            {
                "node_id": node.node_id,
                "lon": f"{node.lon:.8f}",
                "lat": f"{node.lat:.8f}",
            }
            for node in dataset.nodes
        ),
    )
    write_csv(
        output_dir / "road_edges.csv",
        ["source", "target", "weight"],
        (
            {
                "source": edge.source,
                "target": edge.target,
                "weight": f"{edge.weight_m:.6f}",
            }
            for edge in dataset.edges
        ),
    )
    write_csv(
        output_dir / "wells.csv",
        ["uwi", "longitude", "latitude", "well_name", "nearest_node_id"],
        (
            {
                "uwi": well.uwi,
                "longitude": f"{well.lon:.8f}",
                "latitude": f"{well.lat:.8f}",
                "well_name": well.well_name,
                "nearest_node_id": well.nearest_node_id,
            }
            for well in dataset.wells
        ),
    )
    write_csv(
        output_dir / "vehicles.csv",
        [
            "vehicle_id",
            "name",
            "vehicle_type",
            "current_node",
            "lon",
            "lat",
            "available_at",
            "avg_speed_kmph",
            "skills",
            "registration_plate",
        ],
        (
            {
                "vehicle_id": vehicle.vehicle_id,
                "name": vehicle.name,
                "vehicle_type": vehicle.vehicle_type,
                "current_node": vehicle.current_node,
                "lon": f"{vehicle.lon:.8f}",
                "lat": f"{vehicle.lat:.8f}",
                "available_at": vehicle.available_at.isoformat(),
                "avg_speed_kmph": f"{vehicle.avg_speed_kmph:.1f}",
                "skills": "|".join(sorted(vehicle.skills)),
                "registration_plate": vehicle.registration_plate or "",
            }
            for vehicle in dataset.vehicles
        ),
    )
    write_csv(
        output_dir / "tasks.csv",
        [
            "task_id",
            "priority",
            "planned_start",
            "start_day",
            "planned_duration_hours",
            "destination_uwi",
            "task_type",
            "shift",
        ],
        (
            {
                "task_id": task.task_id,
                "priority": task.priority.value,
                "planned_start": task.planned_start.isoformat(),
                "start_day": task.start_day.isoformat(),
                "planned_duration_hours": f"{task.planned_duration_hours:.1f}",
                "destination_uwi": task.destination_uwi,
                "task_type": task.task_type,
                "shift": task.shift.value,
            }
            for task in dataset.tasks
        ),
    )
    write_csv(
        output_dir / "compatibility.csv",
        ["task_type", "vehicle_type"],
        (
            {
                "task_type": task_type,
                "vehicle_type": vehicle_type,
            }
            for task_type, vehicle_types in sorted(dataset.compatibility.items())
            for vehicle_type in sorted(vehicle_types)
        ),
    )


def write_csv(path: Path, fieldnames: list[str], rows) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


if __name__ == "__main__":
    main()
