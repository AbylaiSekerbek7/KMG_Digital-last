from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from uto_routing.graph import RoadGraph
from uto_routing.models import Task, Vehicle, VehicleEvaluation


@dataclass(frozen=True)
class ScoringWeights:
    distance_weight: float = 2.2
    travel_weight: float = 0.3
    wait_weight: float = 0.8
    lateness_base_penalty: float = 10.0
    lateness_priority_multiplier: float = 30.0
    shift_penalty: float = 50.0
    incompatibility_penalty: float = 10_000.0
    score_scale: float = 25.0

    def as_dict(self) -> dict[str, float]:
        return {
            "distance_weight": self.distance_weight,
            "travel_weight": self.travel_weight,
            "wait_weight": self.wait_weight,
            "lateness_base_penalty": self.lateness_base_penalty,
            "lateness_priority_multiplier": self.lateness_priority_multiplier,
            "shift_penalty": self.shift_penalty,
            "incompatibility_penalty": self.incompatibility_penalty,
            "score_scale": self.score_scale,
        }


DEFAULT_SCORING_WEIGHTS = ScoringWeights()


def evaluate_vehicle_for_task(
    graph: RoadGraph,
    vehicle: Vehicle,
    task: Task,
    destination_node: int,
    compatibility: dict[str, set[str]],
    reference_time: datetime,
    weights: ScoringWeights = DEFAULT_SCORING_WEIGHTS,
) -> VehicleEvaluation:
    compatible_types = compatibility.get(task.task_type, set())
    compatible = vehicle.can_handle(task.task_type) or vehicle.vehicle_type in compatible_types

    route = graph.shortest_path(vehicle.current_node, destination_node)
    travel_minutes = graph.travel_minutes(route.distance_m, vehicle.avg_speed_kmph)
    depart_at = max(vehicle.available_at, reference_time)
    arrival_at = depart_at + timedelta(minutes=travel_minutes)
    service_start = max(arrival_at, task.earliest_start)
    wait_minutes = max(0.0, (vehicle.available_at - reference_time).total_seconds() / 60.0)
    late_minutes = max(0.0, (service_start - task.sla_deadline).total_seconds() / 60.0)
    shift_violation_minutes = max(0.0, (service_start - task.shift_end).total_seconds() / 60.0)

    incompatibility_penalty = weights.incompatibility_penalty if not compatible else 0.0
    distance_km = route.distance_m / 1000.0
    distance_component = distance_km * weights.distance_weight
    travel_component = travel_minutes * weights.travel_weight
    wait_component = wait_minutes * weights.wait_weight
    lateness_penalty = late_minutes * (
        weights.lateness_base_penalty + weights.lateness_priority_multiplier * task.priority_weight
    )
    shift_penalty = shift_violation_minutes * weights.shift_penalty
    cost = (
        distance_component
        + travel_component
        + wait_component
        + lateness_penalty
        + shift_penalty
        + incompatibility_penalty
    )
    score = 1.0 / (1.0 + cost / weights.score_scale)
    score_breakdown = {
        "distance_component": round(distance_component, 4),
        "travel_component": round(travel_component, 4),
        "wait_component": round(wait_component, 4),
        "lateness_penalty": round(lateness_penalty, 4),
        "shift_penalty": round(shift_penalty, 4),
        "incompatibility_penalty": round(incompatibility_penalty, 4),
        "total_cost": round(cost, 4),
        "priority_weight": round(task.priority_weight, 4),
        "score_scale": round(weights.score_scale, 4),
    }
    reason = build_reason(
        compatible=compatible,
        wait_minutes=wait_minutes,
        distance_km=distance_km,
        late_minutes=late_minutes,
        shift_violation_minutes=shift_violation_minutes,
    )

    return VehicleEvaluation(
        vehicle_id=vehicle.vehicle_id,
        name=vehicle.name,
        vehicle_type=vehicle.vehicle_type,
        compatible=compatible,
        distance_m=route.distance_m,
        travel_minutes=travel_minutes,
        wait_minutes=wait_minutes,
        arrival_at=arrival_at,
        service_start=service_start,
        late_minutes=late_minutes,
        shift_violation_minutes=shift_violation_minutes,
        score=score,
        cost=cost,
        score_breakdown=score_breakdown,
        reason=reason,
    )


def build_reason(
    *,
    compatible: bool,
    wait_minutes: float,
    distance_km: float,
    late_minutes: float,
    shift_violation_minutes: float,
) -> str:
    reasons: list[str] = []
    if compatible:
        reasons.append("совместима по типу работ")
    else:
        reasons.append("несовместима по типу работ")

    if wait_minutes <= 5:
        reasons.append("свободна сейчас или почти сейчас")
    else:
        reasons.append(f"освободится примерно через {round(wait_minutes)} мин")

    if distance_km <= 8:
        reasons.append("находится близко к точке работ")
    elif distance_km <= 18:
        reasons.append("маршрут умеренной длины")
    else:
        reasons.append("маршрут длиннее среднего")

    if late_minutes > 0:
        reasons.append(f"риск опоздания около {round(late_minutes)} мин")
    else:
        reasons.append("укладывается в SLA")

    if shift_violation_minutes > 0:
        reasons.append("старт выходит за окно смены")

    return ", ".join(reasons)

