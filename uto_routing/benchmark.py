from __future__ import annotations

import random
import time
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Any

from uto_routing.models import BatchPlan, Dataset, Priority, Shift, Task, Vehicle, resolve_start_day
from uto_routing.planners import plan_batch
from uto_routing.scoring import DEFAULT_SCORING_WEIGHTS, ScoringWeights


COMPARISON_DIRECTIONS = {
    "assignment_rate": "higher_is_better",
    "unassigned_tasks": "lower_is_better",
    "total_distance_km": "lower_is_better",
    "total_travel_minutes": "lower_is_better",
    "weighted_lateness": "lower_is_better",
    "high_priority_on_time_rate": "higher_is_better",
    "runtime_ms": "lower_is_better",
}


def simulate_batch_plan(plan: BatchPlan, dataset: Dataset, reference_time: datetime) -> dict[str, float]:
    task_lookup = dataset.task_lookup()
    total_distance_m = sum(assignment.total_distance_m for assignment in plan.assignments)
    total_travel_minutes = sum(assignment.total_travel_minutes for assignment in plan.assignments)
    total_service_minutes = 0.0
    weighted_lateness = 0.0
    high_priority_total = 0
    high_priority_on_time = 0
    busy_minutes = 0.0
    assigned_task_count = 0

    for assignment in plan.assignments:
        busy_minutes += (assignment.finished_at - assignment.started_at).total_seconds() / 60.0
        for leg in assignment.route_legs:
            task = task_lookup[leg.task_id]
            assigned_task_count += 1
            service_minutes = (leg.service_end - leg.service_start).total_seconds() / 60.0
            total_service_minutes += service_minutes
            lateness = max(0.0, (leg.service_start - task.sla_deadline).total_seconds() / 60.0)
            weighted_lateness += lateness * task.priority_weight
            if task.priority is Priority.HIGH:
                high_priority_total += 1
                if lateness == 0:
                    high_priority_on_time += 1

    horizon_end = max(
        [task.shift_end for task in dataset.tasks] + [reference_time + timedelta(hours=12)]
    )
    horizon_minutes = max(1.0, (horizon_end - reference_time).total_seconds() / 60.0)
    vehicle_utilization = busy_minutes / (max(1, len(dataset.vehicles)) * horizon_minutes)
    high_priority_on_time_rate = (
        high_priority_on_time / high_priority_total if high_priority_total else 1.0
    )

    return {
        "assigned_tasks": float(assigned_task_count),
        "unassigned_tasks": float(len(plan.unassigned_task_ids)),
        "total_distance_km": total_distance_m / 1000.0,
        "total_travel_minutes": total_travel_minutes,
        "total_service_minutes": total_service_minutes,
        "weighted_lateness": weighted_lateness,
        "high_priority_on_time_rate": high_priority_on_time_rate,
        "vehicle_utilization": vehicle_utilization,
        "sequence_count": float(len(plan.assignments)),
    }


def enrich_metrics(metrics: dict[str, float]) -> dict[str, float]:
    total_tasks = metrics["assigned_tasks"] + metrics["unassigned_tasks"]
    assignment_rate = metrics["assigned_tasks"] / total_tasks if total_tasks else 1.0
    enriched = dict(metrics)
    enriched["assignment_rate"] = assignment_rate
    return enriched


def round_metrics(metrics: dict[str, float], digits: int = 4) -> dict[str, float]:
    return {key: round(value, digits) for key, value in metrics.items()}


def compare_metrics(
    current: dict[str, float],
    baseline: dict[str, float],
) -> dict[str, dict[str, float | bool | str | None]]:
    comparison: dict[str, dict[str, float | bool | str | None]] = {}
    for metric, direction in COMPARISON_DIRECTIONS.items():
        current_value = current.get(metric)
        baseline_value = baseline.get(metric)
        if current_value is None or baseline_value is None:
            continue
        delta = current_value - baseline_value
        delta_percent: float | None = None
        if baseline_value != 0:
            delta_percent = (delta / baseline_value) * 100.0
        improved = delta > 0 if direction == "higher_is_better" else delta < 0
        comparison[metric] = {
            "current": round(current_value, 4),
            "baseline": round(baseline_value, 4),
            "delta": round(delta, 4),
            "delta_percent": round(delta_percent, 4) if delta_percent is not None else None,
            "direction": direction,
            "improved": improved,
        }
    return comparison


def run_benchmark(
    base_dataset: Dataset,
    graph,
    *,
    scenarios: int = 250,
    min_tasks: int = 6,
    max_tasks: int = 12,
    min_vehicles: int = 4,
    max_vehicles: int = 7,
    seed: int = 42,
    strategies: tuple[str, ...] = ("baseline", "priority_greedy", "multistop_heuristic", "ortools_solver"),
    ortools_time_limit_seconds: int = 2,
    scoring_weights: ScoringWeights = DEFAULT_SCORING_WEIGHTS,
) -> dict[str, object]:
    rng = random.Random(seed)
    aggregates: dict[str, dict[str, float]] = {
        strategy: defaultdict(float) for strategy in strategies
    }
    distance_wins: dict[str, int] = {strategy: 0 for strategy in strategies if strategy != "baseline"}
    lateness_wins: dict[str, int] = {strategy: 0 for strategy in strategies if strategy != "baseline"}

    for scenario_index in range(scenarios):
        scenario_dataset, reference_time = generate_synthetic_dataset(
            base_dataset=base_dataset,
            seed=rng.randint(0, 10_000_000),
            task_count=rng.randint(min_tasks, max_tasks),
            vehicle_count=rng.randint(min_vehicles, max_vehicles),
        )
        scenario_metrics: dict[str, dict[str, float]] = {}
        for strategy in strategies:
            started = time.perf_counter()
            plan = plan_batch(
                graph=graph,
                dataset=scenario_dataset,
                tasks=scenario_dataset.tasks,
                reference_time=reference_time,
                strategy=strategy,
                ortools_time_limit_seconds=ortools_time_limit_seconds,
                scoring_weights=scoring_weights,
            )
            elapsed_ms = (time.perf_counter() - started) * 1000.0
            metrics = enrich_metrics(simulate_batch_plan(plan, scenario_dataset, reference_time))
            metrics["runtime_ms"] = elapsed_ms
            scenario_metrics[strategy] = metrics
            for key, value in metrics.items():
                aggregates[strategy][key] += value

        baseline = scenario_metrics["baseline"]
        for strategy in strategies:
            if strategy == "baseline":
                continue
            if scenario_metrics[strategy]["total_distance_km"] < baseline["total_distance_km"]:
                distance_wins[strategy] += 1
            if scenario_metrics[strategy]["weighted_lateness"] <= baseline["weighted_lateness"]:
                lateness_wins[strategy] += 1

    strategy_report: dict[str, dict[str, Any]] = {}
    for strategy in strategies:
        report = round_metrics({key: value / scenarios for key, value in aggregates[strategy].items()})
        if strategy != "baseline":
            report["distance_win_rate_vs_baseline"] = round(distance_wins[strategy] / scenarios, 4)
            report["lateness_win_rate_vs_baseline"] = round(lateness_wins[strategy] / scenarios, 4)
        strategy_report[strategy] = report

    baseline_report = strategy_report.get("baseline")
    if baseline_report is not None:
        for strategy, report in strategy_report.items():
            if strategy == "baseline":
                continue
            report["comparison_vs_baseline"] = compare_metrics(report, baseline_report)

    best_distance = min(strategy_report.items(), key=lambda item: item[1]["total_distance_km"])[0]
    best_lateness = min(strategy_report.items(), key=lambda item: item[1]["weighted_lateness"])[0]
    fastest = min(strategy_report.items(), key=lambda item: item[1]["runtime_ms"])[0]

    return {
        "scenarios": scenarios,
        "parameters": {
            "min_tasks": min_tasks,
            "max_tasks": max_tasks,
            "min_vehicles": min_vehicles,
            "max_vehicles": max_vehicles,
            "seed": seed,
            "ortools_time_limit_seconds": ortools_time_limit_seconds,
            "scoring_weights": scoring_weights.as_dict(),
        },
        "strategies": strategy_report,
        "best_by_metric": {
            "distance": best_distance,
            "weighted_lateness": best_lateness,
            "runtime": fastest,
        },
    }


def generate_synthetic_dataset(
    base_dataset: Dataset,
    *,
    seed: int,
    task_count: int,
    vehicle_count: int,
) -> tuple[Dataset, datetime]:
    rng = random.Random(seed)
    wells = list(base_dataset.wells)
    nodes = base_dataset.node_lookup()
    compatibility = base_dataset.compatibility
    reference_time = datetime(2026, 3, 17, 8, 0, 0)

    vehicle_types = sorted({vehicle.vehicle_type for vehicle in base_dataset.vehicles})
    vehicles: list[Vehicle] = []
    for index in range(vehicle_count):
        node_id = rng.choice(base_dataset.nodes).node_id
        node = nodes[node_id]
        vehicle_type = rng.choice(vehicle_types)
        skills = {
            task_type
            for task_type, compatible_types in compatibility.items()
            if vehicle_type in compatible_types
        }
        vehicles.append(
            Vehicle(
                vehicle_id=30_000 + index,
                name=f"{vehicle_type}-{index + 1}",
                vehicle_type=vehicle_type,
                current_node=node_id,
                lon=node.lon,
                lat=node.lat,
                available_at=reference_time + timedelta(minutes=rng.randint(0, 120)),
                avg_speed_kmph=round(rng.uniform(18.0, 28.0), 1),
                skills=skills,
                registration_plate=f"SIM-{index:03d}",
            )
        )

    task_types = sorted(compatibility.keys())
    tasks: list[Task] = []
    for index in range(task_count):
        well = rng.choice(wells)
        priority = rng.choices(
            [Priority.HIGH, Priority.MEDIUM, Priority.LOW],
            weights=[0.25, 0.45, 0.30],
            k=1,
        )[0]
        minute_offset = rng.randint(0, 23 * 60)
        planned_start = reference_time + timedelta(minutes=minute_offset)
        shift = Shift.DAY if 8 <= planned_start.hour < 20 else Shift.NIGHT
        tasks.append(
            Task(
                task_id=f"S-{seed}-{index + 1}",
                priority=priority,
                planned_start=planned_start,
                planned_duration_hours=round(rng.uniform(1.5, 5.5), 1),
                destination_uwi=well.uwi,
                task_type=rng.choice(task_types),
                shift=shift,
                start_day=resolve_start_day(planned_start, shift),
            )
        )

    dataset = Dataset(
        nodes=base_dataset.nodes,
        edges=base_dataset.edges,
        wells=base_dataset.wells,
        vehicles=vehicles,
        tasks=tasks,
        compatibility=compatibility,
        metadata={
            "dataset_mode": "synthetic",
            "seed": str(seed),
        },
    )
    return dataset, reference_time

