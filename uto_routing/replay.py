from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from uto_routing.benchmark import enrich_metrics, round_metrics, simulate_batch_plan
from uto_routing.models import BatchPlan, Dataset
from uto_routing.planners import plan_batch
from uto_routing.scoring import DEFAULT_SCORING_WEIGHTS, ScoringWeights


def run_historical_replay(
    *,
    graph,
    dataset: Dataset,
    reference_time: datetime,
    strategy: str,
    scoring_weights: ScoringWeights = DEFAULT_SCORING_WEIGHTS,
    ortools_time_limit_seconds: int = 2,
    frame_interval_minutes: int = 15,
) -> dict[str, Any]:
    plan = plan_batch(
        graph=graph,
        dataset=dataset,
        tasks=dataset.tasks,
        reference_time=reference_time,
        strategy=strategy,
        ortools_time_limit_seconds=ortools_time_limit_seconds,
        scoring_weights=scoring_weights,
    )
    metrics = round_metrics(enrich_metrics(simulate_batch_plan(plan, dataset, reference_time)))
    playback = build_playback(plan, dataset, reference_time, frame_interval_minutes=frame_interval_minutes)
    return {
        "strategy": strategy,
        "reference_time": reference_time.isoformat(),
        "metrics": metrics,
        "playback": playback,
    }


MAX_PLAYBACK_FRAMES = 600


def build_playback(
    plan: BatchPlan,
    dataset: Dataset,
    reference_time: datetime,
    *,
    frame_interval_minutes: int = 15,
) -> dict[str, Any]:
    vehicle_lookup = dataset.vehicle_lookup()
    active_assignments = {assignment.vehicle_id: assignment for assignment in plan.assignments}
    end_time = max(
        [assignment.finished_at for assignment in plan.assignments] + [reference_time + timedelta(minutes=1)]
    )

    # Auto-adjust frame interval so we never exceed MAX_PLAYBACK_FRAMES
    total_span_minutes = (end_time - reference_time).total_seconds() / 60.0
    if total_span_minutes / frame_interval_minutes > MAX_PLAYBACK_FRAMES:
        frame_interval_minutes = max(1, int(total_span_minutes / MAX_PLAYBACK_FRAMES))

    current_time = reference_time
    frames: list[dict[str, Any]] = []

    while current_time <= end_time:
        frame_positions = []
        for vehicle in dataset.vehicles:
            assignment = active_assignments.get(vehicle.vehicle_id)
            position = _vehicle_position_at_time(vehicle, assignment, current_time)
            frame_positions.append(position)
        frames.append(
            {
                "timestamp": current_time.isoformat(),
                "vehicles": frame_positions,
            }
        )
        current_time += timedelta(minutes=frame_interval_minutes)

    return {
        "frame_interval_minutes": frame_interval_minutes,
        "start_time": reference_time.isoformat(),
        "end_time": end_time.isoformat(),
        "frames": frames,
    }


def _vehicle_position_at_time(vehicle, assignment, timestamp: datetime) -> dict[str, Any]:
    if assignment is None or not assignment.route_legs:
        return {
            "vehicle_id": vehicle.vehicle_id,
            "name": vehicle.name,
            "lat": vehicle.lat,
            "lon": vehicle.lon,
            "status": "idle",
            "task_id": None,
        }

    first_leg = assignment.route_legs[0]
    if timestamp <= assignment.started_at:
        return {
            "vehicle_id": vehicle.vehicle_id,
            "name": vehicle.name,
            "lat": vehicle.lat,
            "lon": vehicle.lon,
            "status": "waiting",
            "task_id": None,
        }

    previous_coord = assignment.route_legs[0].route.coords[0]
    for leg in assignment.route_legs:
        if timestamp <= leg.arrival_at:
            lon, lat = _interpolate_route_position(
                leg.route.coords,
                assignment.started_at if leg == first_leg else previous_service_end(assignment, leg),
                leg.arrival_at,
                timestamp,
                fallback=previous_coord,
            )
            return {
                "vehicle_id": vehicle.vehicle_id,
                "name": vehicle.name,
                "lat": lat,
                "lon": lon,
                "status": "driving",
                "task_id": leg.task_id,
            }
        if leg.arrival_at < timestamp <= leg.service_end:
            lon, lat = leg.route.coords[-1]
            status = "waiting_at_site" if timestamp < leg.service_start else "servicing"
            return {
                "vehicle_id": vehicle.vehicle_id,
                "name": vehicle.name,
                "lat": lat,
                "lon": lon,
                "status": status,
                "task_id": leg.task_id,
            }
        previous_coord = leg.route.coords[-1]

    lon, lat = assignment.route_legs[-1].route.coords[-1]
    return {
        "vehicle_id": vehicle.vehicle_id,
        "name": vehicle.name,
        "lat": lat,
        "lon": lon,
        "status": "completed",
        "task_id": assignment.route_legs[-1].task_id,
    }


def previous_service_end(assignment, leg) -> datetime:
    index = assignment.route_legs.index(leg)
    if index == 0:
        return assignment.started_at
    return assignment.route_legs[index - 1].service_end


def _interpolate_route_position(
    coords: list[list[float]] | list[tuple[float, float]],
    start_time: datetime,
    end_time: datetime,
    timestamp: datetime,
    *,
    fallback: tuple[float, float] | list[float],
) -> tuple[float, float]:
    if not coords:
        return float(fallback[0]), float(fallback[1])
    if len(coords) == 1 or end_time <= start_time:
        lon, lat = coords[-1]
        return lon, lat
    ratio = (timestamp - start_time).total_seconds() / (end_time - start_time).total_seconds()
    ratio = max(0.0, min(1.0, ratio))
    segment_count = len(coords) - 1
    exact_position = ratio * segment_count
    segment_index = min(segment_count - 1, int(exact_position))
    local_ratio = exact_position - segment_index
    start_lon, start_lat = coords[segment_index]
    end_lon, end_lat = coords[segment_index + 1]
    lon = start_lon + (end_lon - start_lon) * local_ratio
    lat = start_lat + (end_lat - start_lat) * local_ratio
    return lon, lat

