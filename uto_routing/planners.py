from __future__ import annotations

import copy
import itertools
from dataclasses import dataclass
from datetime import datetime, timedelta

from uto_routing.graph import RoadGraph
from uto_routing.models import (
    BatchPlan,
    Dataset,
    PlanAssignment,
    RouteLeg,
    Task,
    Vehicle,
    VehicleEvaluation,
)
from uto_routing.ortools_solver import solve_batch_with_ortools
from uto_routing.scoring import DEFAULT_SCORING_WEIGHTS, ScoringWeights, evaluate_vehicle_for_task


DEFAULT_GROUP_MAX_TIME_MINUTES = 480.0
DEFAULT_GROUP_MAX_DETOUR_RATIO = 1.3


@dataclass
class GroupEvaluation:
    vehicle_id: int
    vehicle_name: str
    task_ids: list[str]
    route_legs: list[RouteLeg]
    total_distance_m: float
    total_travel_minutes: float
    total_late_minutes: float
    total_shift_violation_minutes: float
    started_at: datetime
    finished_at: datetime
    cost: float


def recommend_for_task(
    graph: RoadGraph,
    dataset: Dataset,
    task: Task,
    reference_time: datetime,
    strategy: str = "priority_greedy",
    top_k: int = 3,
    scoring_weights: ScoringWeights = DEFAULT_SCORING_WEIGHTS,
) -> list[VehicleEvaluation]:
    destination_node = _destination_node(dataset, task)
    evaluations = [
        evaluate_vehicle_for_task(
            graph=graph,
            vehicle=vehicle,
            task=task,
            destination_node=destination_node,
            compatibility=dataset.compatibility,
            reference_time=reference_time,
            weights=scoring_weights,
        )
        for vehicle in dataset.vehicles
    ]

    if strategy == "baseline":
        free_cutoff = task.earliest_start
        evaluations.sort(
            key=lambda item: (
                not item.compatible,
                item.arrival_at > free_cutoff,
                item.distance_m,
                item.service_start,
            )
        )
    else:
        evaluations.sort(
            key=lambda item: (
                not item.compatible,
                -item.score,
                item.distance_m,
            )
        )
    return evaluations[:top_k]


def plan_batch(
    graph: RoadGraph,
    dataset: Dataset,
    tasks: list[Task],
    reference_time: datetime,
    strategy: str = "priority_greedy",
    *,
    ortools_time_limit_seconds: int = 2,
    scoring_weights: ScoringWeights = DEFAULT_SCORING_WEIGHTS,
) -> BatchPlan:
    if strategy == "ortools_solver":
        return solve_batch_with_ortools(
            graph,
            dataset,
            tasks,
            reference_time,
            time_limit_seconds=ortools_time_limit_seconds,
        )
    if strategy == "multistop_heuristic":
        return _plan_batch_multistop(
            graph,
            dataset,
            tasks,
            reference_time,
            scoring_weights=scoring_weights,
        )
    return _plan_batch_single_task(
        graph,
        dataset,
        tasks,
        reference_time,
        strategy=strategy,
        scoring_weights=scoring_weights,
    )


def evaluate_multitask_grouping(
    graph: RoadGraph,
    dataset: Dataset,
    tasks: list[Task],
    reference_time: datetime,
    *,
    max_total_time_minutes: float = 480.0,
    max_detour_ratio: float = 1.3,
) -> dict[str, object]:
    if not tasks:
        return {
            "groups": [],
            "strategy_summary": "separate",
            "total_distance_km": 0.0,
            "total_time_minutes": 0.0,
            "baseline_distance_km": 0.0,
            "baseline_time_minutes": 0.0,
            "savings_percent": 0.0,
            "reason": "не переданы заявки для анализа",
        }

    single_costs = {
        task.task_id: _best_group_evaluation(
            graph=graph,
            vehicles=copy.deepcopy(dataset.vehicles),
            tasks=[task],
            dataset=dataset,
            reference_time=reference_time,
        )
        for task in tasks
    }
    baseline_distance_m = sum(item.total_distance_m for item in single_costs.values())
    baseline_time_minutes = sum(
        (item.finished_at - item.started_at).total_seconds() / 60.0 for item in single_costs.values()
    )
    pair_candidates = sorted(
        itertools.combinations(tasks, 2),
        key=lambda pair: (
            graph.shortest_path(
                _destination_node(dataset, pair[0]),
                _destination_node(dataset, pair[1]),
            ).distance_m
        ),
    )

    used_task_ids: set[str] = set()
    merged_groups: list[list[Task]] = []
    for left, right in pair_candidates:
        if left.task_id in used_task_ids or right.task_id in used_task_ids:
            continue
        baseline_pair_distance = (
            single_costs[left.task_id].total_distance_m + single_costs[right.task_id].total_distance_m
        )
        grouped = _best_group_evaluation(
            graph=graph,
            vehicles=copy.deepcopy(dataset.vehicles),
            tasks=[left, right],
            dataset=dataset,
            reference_time=reference_time,
        )
        grouped_total_time = (grouped.finished_at - grouped.started_at).total_seconds() / 60.0
        if (
            grouped.route_legs
            and
            grouped_total_time <= max_total_time_minutes
            and grouped.total_distance_m <= baseline_pair_distance * max_detour_ratio
            and grouped.total_distance_m < baseline_pair_distance
        ):
            merged_groups.append([left, right])
            used_task_ids.add(left.task_id)
            used_task_ids.add(right.task_id)

    for task in tasks:
        if task.task_id not in used_task_ids:
            merged_groups.append([task])

    total_distance_m = 0.0
    total_time_minutes = 0.0
    for group in merged_groups:
        evaluation = _best_group_evaluation(
            graph=graph,
            vehicles=copy.deepcopy(dataset.vehicles),
            tasks=group,
            dataset=dataset,
            reference_time=reference_time,
        )
        if evaluation.route_legs:
            total_distance_m += evaluation.total_distance_m
            total_time_minutes += (evaluation.finished_at - evaluation.started_at).total_seconds() / 60.0
        else:
            for task in group:
                single = single_costs[task.task_id]
                total_distance_m += single.total_distance_m
                total_time_minutes += (single.finished_at - single.started_at).total_seconds() / 60.0

    savings_percent = 0.0
    if baseline_distance_m > 0:
        savings_percent = max(0.0, (baseline_distance_m - total_distance_m) / baseline_distance_m * 100.0)

    strategy_summary = "separate"
    if all(len(group) == 1 for group in merged_groups):
        reason = "заявки выгоднее обслуживать раздельно в рамках заданных ограничений"
    elif len(merged_groups) == 1 and len(merged_groups[0]) > 1:
        strategy_summary = "single_unit"
        reason = "все заявки образуют компактный кластер и выгодны для одного выезда"
    else:
        strategy_summary = "mixed"
        reason = "часть заявок образует компактные пары, остальные лучше оставить отдельными"

    return {
        "groups": [[task.task_id for task in group] for group in merged_groups],
        "strategy_summary": strategy_summary,
        "total_distance_km": round(total_distance_m / 1000.0, 2),
        "total_time_minutes": round(total_time_minutes, 1),
        "baseline_distance_km": round(baseline_distance_m / 1000.0, 2),
        "baseline_time_minutes": round(baseline_time_minutes, 1),
        "savings_percent": round(savings_percent, 1),
        "reason": reason,
    }


def _plan_batch_single_task(
    graph: RoadGraph,
    dataset: Dataset,
    tasks: list[Task],
    reference_time: datetime,
    *,
    strategy: str,
    scoring_weights: ScoringWeights,
) -> BatchPlan:
    vehicles = copy.deepcopy(dataset.vehicles)
    assignments: list[PlanAssignment] = []
    unassigned_task_ids: list[str] = []

    for task in _sorted_tasks(tasks):
        candidate_vehicles = Dataset(
            nodes=dataset.nodes,
            edges=dataset.edges,
            wells=dataset.wells,
            vehicles=vehicles,
            tasks=dataset.tasks,
            compatibility=dataset.compatibility,
            metadata=dataset.metadata,
        )
        recommendations = recommend_for_task(
            graph=graph,
            dataset=candidate_vehicles,
            task=task,
            reference_time=reference_time,
            strategy=strategy,
            top_k=len(vehicles),
            scoring_weights=scoring_weights,
        )
        chosen = next((item for item in recommendations if item.compatible), None)
        if chosen is None:
            unassigned_task_ids.append(task.task_id)
            continue

        vehicle = next(vehicle for vehicle in vehicles if vehicle.vehicle_id == chosen.vehicle_id)
        group_eval = _best_group_evaluation(
            graph=graph,
            vehicles=[vehicle],
            tasks=[task],
            dataset=dataset,
            reference_time=reference_time,
        )
        assignment = PlanAssignment(
            vehicle_id=group_eval.vehicle_id,
            vehicle_name=group_eval.vehicle_name,
            task_ids=group_eval.task_ids,
            route_legs=group_eval.route_legs,
            total_distance_m=group_eval.total_distance_m,
            total_travel_minutes=group_eval.total_travel_minutes,
            started_at=group_eval.started_at,
            finished_at=group_eval.finished_at,
            explanation=_build_assignment_explanation(group_eval.task_ids, group_eval.total_distance_m, group_eval.total_late_minutes, strategy),
        )
        assignments.append(assignment)
        _apply_group_to_vehicle(vehicle, assignment)

    return BatchPlan(
        strategy=strategy,
        assignments=assignments,
        unassigned_task_ids=unassigned_task_ids,
        summary=f"Assigned {len(assignments)} task sequences using {strategy}.",
    )


def _plan_batch_multistop(
    graph: RoadGraph,
    dataset: Dataset,
    tasks: list[Task],
    reference_time: datetime,
    *,
    scoring_weights: ScoringWeights,
) -> BatchPlan:
    vehicles = copy.deepcopy(dataset.vehicles)
    task_groups = _build_static_task_groups(graph, dataset, tasks, reference_time)
    assignments: list[PlanAssignment] = []
    unassigned_task_ids: list[str] = []

    for group in task_groups:
        evaluation = _best_group_evaluation(
            graph=graph,
            vehicles=vehicles,
            tasks=group,
            dataset=dataset,
            reference_time=reference_time,
        )
        if not evaluation.route_legs:
            if len(group) > 1:
                fallback = _plan_batch_single_task(
                    graph=graph,
                    dataset=Dataset(
                        nodes=dataset.nodes,
                        edges=dataset.edges,
                        wells=dataset.wells,
                        vehicles=vehicles,
                        tasks=dataset.tasks,
                        compatibility=dataset.compatibility,
                        metadata=dataset.metadata,
                    ),
                    tasks=group,
                    reference_time=reference_time,
                    strategy="priority_greedy",
                    scoring_weights=scoring_weights,
                )
                assignments.extend(fallback.assignments)
                unassigned_task_ids.extend(fallback.unassigned_task_ids)
            else:
                unassigned_task_ids.extend(task.task_id for task in group)
            continue

        assignment = PlanAssignment(
            vehicle_id=evaluation.vehicle_id,
            vehicle_name=evaluation.vehicle_name,
            task_ids=evaluation.task_ids,
            route_legs=evaluation.route_legs,
            total_distance_m=evaluation.total_distance_m,
            total_travel_minutes=evaluation.total_travel_minutes,
            started_at=evaluation.started_at,
            finished_at=evaluation.finished_at,
            explanation=_build_assignment_explanation(
                evaluation.task_ids,
                evaluation.total_distance_m,
                evaluation.total_late_minutes,
                "multistop_heuristic",
            ),
        )
        assignments.append(assignment)
        vehicle = next(vehicle for vehicle in vehicles if vehicle.vehicle_id == evaluation.vehicle_id)
        _apply_group_to_vehicle(vehicle, assignment)

    return BatchPlan(
        strategy="multistop_heuristic",
        assignments=assignments,
        unassigned_task_ids=unassigned_task_ids,
        summary="Assigned task clusters using multi-stop heuristic.",
    )


def _build_static_task_groups(
    graph: RoadGraph,
    dataset: Dataset,
    tasks: list[Task],
    reference_time: datetime,
    *,
    max_total_time_minutes: float = DEFAULT_GROUP_MAX_TIME_MINUTES,
    max_detour_ratio: float = DEFAULT_GROUP_MAX_DETOUR_RATIO,
) -> list[list[Task]]:
    if len(tasks) <= 1:
        return [[task] for task in tasks]

    singles = {
        task.task_id: _best_group_evaluation(
            graph=graph,
            vehicles=copy.deepcopy(dataset.vehicles),
            tasks=[task],
            dataset=dataset,
            reference_time=reference_time,
        )
        for task in tasks
    }

    remaining = {task.task_id: task for task in tasks}
    groups: list[list[Task]] = []

    while remaining:
        current = min(
            remaining.values(),
            key=lambda task: (
                -task.priority_weight,
                task.earliest_start,
            ),
        )
        del remaining[current.task_id]
        best_merge: list[Task] | None = None
        best_savings = 0.0
        for candidate in list(remaining.values()):
            grouped = _best_group_evaluation(
                graph=graph,
                vehicles=copy.deepcopy(dataset.vehicles),
                tasks=[current, candidate],
                dataset=dataset,
                reference_time=reference_time,
            )
            if not grouped.route_legs:
                continue
            baseline_distance = (
                singles[current.task_id].total_distance_m + singles[candidate.task_id].total_distance_m
            )
            grouped_total_time_minutes = (grouped.finished_at - grouped.started_at).total_seconds() / 60.0
            if grouped_total_time_minutes > max_total_time_minutes:
                continue
            if grouped.total_distance_m > baseline_distance * max_detour_ratio:
                continue
            savings = baseline_distance - grouped.total_distance_m
            if savings > best_savings:
                best_savings = savings
                best_merge = [current, candidate]
        if best_merge is not None and best_savings > 0:
            del remaining[best_merge[1].task_id]
            groups.append(sorted(best_merge, key=lambda task: task.earliest_start))
        else:
            groups.append([current])

    return groups


def _best_group_evaluation(
    graph: RoadGraph,
    vehicles: list[Vehicle],
    tasks: list[Task],
    dataset: Dataset,
    reference_time: datetime,
) -> GroupEvaluation:
    best: GroupEvaluation | None = None
    for vehicle in vehicles:
        if any(not _vehicle_can_handle(dataset, vehicle, task) for task in tasks):
            continue
        for ordered_tasks in itertools.permutations(tasks):
            current_node = vehicle.current_node
            current_time = max(vehicle.available_at, reference_time)
            route_legs: list[RouteLeg] = []
            total_distance_m = 0.0
            total_travel_minutes = 0.0
            total_late_minutes = 0.0
            total_shift_violation_minutes = 0.0
            started_at = current_time

            for task in ordered_tasks:
                destination_node = _destination_node(dataset, task)
                route = graph.shortest_path(current_node, destination_node)
                travel_minutes = graph.travel_minutes(route.distance_m, vehicle.avg_speed_kmph)
                arrival_at = current_time + timedelta(minutes=travel_minutes)
                service_start = max(arrival_at, task.earliest_start)
                service_end = service_start + timedelta(minutes=task.service_minutes)
                total_distance_m += route.distance_m
                total_travel_minutes += travel_minutes
                total_late_minutes += max(0.0, (service_start - task.sla_deadline).total_seconds() / 60.0)
                total_shift_violation_minutes += max(0.0, (service_start - task.shift_end).total_seconds() / 60.0)
                route_legs.append(
                    RouteLeg(
                        task_id=task.task_id,
                        route=route,
                        arrival_at=arrival_at,
                        service_start=service_start,
                        service_end=service_end,
                    )
                )
                current_node = destination_node
                current_time = service_end

            cost = (
                total_distance_m / 1000.0 * 2.2
                + total_travel_minutes * 0.3
                + total_late_minutes * 18.0
                + total_shift_violation_minutes * 50.0
            )
            candidate = GroupEvaluation(
                vehicle_id=vehicle.vehicle_id,
                vehicle_name=vehicle.name,
                task_ids=[task.task_id for task in ordered_tasks],
                route_legs=route_legs,
                total_distance_m=total_distance_m,
                total_travel_minutes=total_travel_minutes,
                total_late_minutes=total_late_minutes,
                total_shift_violation_minutes=total_shift_violation_minutes,
                started_at=started_at,
                finished_at=current_time,
                cost=cost,
            )
            if best is None or candidate.cost < best.cost:
                best = candidate

    if best is None:
        return GroupEvaluation(
            vehicle_id=-1,
            vehicle_name="unassigned",
            task_ids=[task.task_id for task in tasks],
            route_legs=[],
            total_distance_m=0.0,
            total_travel_minutes=0.0,
            total_late_minutes=0.0,
            total_shift_violation_minutes=0.0,
            started_at=reference_time,
            finished_at=reference_time,
            cost=1_000_000.0,
        )
    return best


def _apply_group_to_vehicle(vehicle: Vehicle, assignment: PlanAssignment) -> None:
    if not assignment.route_legs:
        return
    last_leg = assignment.route_legs[-1]
    vehicle.current_node = last_leg.route.end_node
    vehicle.lon = last_leg.route.coords[-1][0]
    vehicle.lat = last_leg.route.coords[-1][1]
    vehicle.available_at = last_leg.service_end


def _destination_node(dataset: Dataset, task: Task) -> int:
    well = dataset.well_lookup()[task.destination_uwi]
    if well.nearest_node_id is None:
        raise ValueError(f"Well {well.uwi} is missing nearest_node_id")
    return well.nearest_node_id


def _vehicle_can_handle(dataset: Dataset, vehicle: Vehicle, task: Task) -> bool:
    compatible_types = dataset.compatibility.get(task.task_type, set())
    return vehicle.can_handle(task.task_type) or vehicle.vehicle_type in compatible_types


def _sorted_tasks(tasks: list[Task]) -> list[Task]:
    return sorted(
        tasks,
        key=lambda task: (
            -task.priority_weight,
            task.earliest_start,
        ),
    )


def _build_assignment_explanation(
    task_ids: list[str],
    total_distance_m: float,
    total_late_minutes: float,
    strategy: str,
) -> str:
    distance_km = round(total_distance_m / 1000.0, 2)
    lateness = round(total_late_minutes, 1)
    if len(task_ids) == 1:
        return (
            f"{strategy}: назначена одна заявка {task_ids[0]}, "
            f"суммарный маршрут {distance_km} км, просрочка {lateness} мин."
        )
    return (
        f"{strategy}: объединено {len(task_ids)} заявок ({', '.join(task_ids)}), "
        f"суммарный маршрут {distance_km} км, просрочка {lateness} мин."
    )

