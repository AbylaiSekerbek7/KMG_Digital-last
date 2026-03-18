from __future__ import annotations

import copy
import math
from datetime import datetime, timedelta

from uto_routing.graph import RoadGraph
from uto_routing.models import BatchPlan, Dataset, PlanAssignment, RouteLeg, Task, Vehicle


def solve_batch_with_ortools(
    graph: RoadGraph,
    dataset: Dataset,
    tasks: list[Task],
    reference_time: datetime,
    *,
    time_limit_seconds: int = 10,
) -> BatchPlan:
    try:
        from ortools.constraint_solver import pywrapcp, routing_enums_pb2
    except ImportError as exc:  # pragma: no cover - runtime dependency guard
        raise RuntimeError("OR-Tools is not installed. Add it to runtime dependencies.") from exc

    vehicles = copy.deepcopy(dataset.vehicles)
    compatible_tasks: list[Task] = []
    unassigned_task_ids: list[str] = []
    for task in tasks:
        if any(_vehicle_can_handle(dataset, vehicle, task) for vehicle in vehicles):
            compatible_tasks.append(task)
        else:
            unassigned_task_ids.append(task.task_id)

    if not compatible_tasks:
        return BatchPlan(
            strategy="ortools_solver",
            assignments=[],
            unassigned_task_ids=unassigned_task_ids,
            summary="No tasks had compatible vehicles for OR-Tools optimization.",
        )

    task_destinations = {
        index: _destination_node(dataset, task)
        for index, task in enumerate(compatible_tasks)
    }
    task_count = len(compatible_tasks)
    vehicle_count = len(vehicles)
    start_offset = task_count
    end_offset = task_count + vehicle_count
    node_count = task_count + vehicle_count * 2
    starts = [start_offset + idx for idx in range(vehicle_count)]
    ends = [end_offset + idx for idx in range(vehicle_count)]

    manager = pywrapcp.RoutingIndexManager(node_count, vehicle_count, starts, ends)
    routing = pywrapcp.RoutingModel(manager)
    horizon_minutes = _compute_horizon_minutes(tasks, reference_time)

    graph_distance_cache: dict[tuple[int, int], float] = {}

    def graph_distance(from_graph_node: int, to_graph_node: int) -> float:
        key = (from_graph_node, to_graph_node)
        if key not in graph_distance_cache:
            graph_distance_cache[key] = graph.shortest_path(from_graph_node, to_graph_node).distance_m
        return graph_distance_cache[key]

    def is_task_node(node: int) -> bool:
        return node < task_count

    def is_start_node(node: int) -> bool:
        return start_offset <= node < end_offset

    def is_end_node(node: int) -> bool:
        return node >= end_offset

    def graph_node_for_original(node: int) -> int | None:
        if is_task_node(node):
            return task_destinations[node]
        if is_start_node(node):
            return vehicles[node - start_offset].current_node
        return None

    distance_callbacks: list[int] = []
    time_callbacks: list[int] = []
    for vehicle_index, vehicle in enumerate(vehicles):
        def distance_callback(from_index: int, to_index: int, *, vehicle_index: int = vehicle_index) -> int:
            from_node = manager.IndexToNode(from_index)
            to_node = manager.IndexToNode(to_index)
            if is_end_node(to_node):
                return 0
            from_graph_node = graph_node_for_original(from_node)
            to_graph_node = graph_node_for_original(to_node)
            if from_graph_node is None or to_graph_node is None:
                return 0
            return int(round(graph_distance(from_graph_node, to_graph_node)))

        def time_callback(from_index: int, to_index: int, *, vehicle_index: int = vehicle_index) -> int:
            from_node = manager.IndexToNode(from_index)
            to_node = manager.IndexToNode(to_index)
            if is_end_node(to_node):
                return 0
            from_graph_node = graph_node_for_original(from_node)
            to_graph_node = graph_node_for_original(to_node)
            if from_graph_node is None or to_graph_node is None:
                return 0
            distance_m = graph_distance(from_graph_node, to_graph_node)
            travel_minutes = graph.travel_minutes(distance_m, vehicles[vehicle_index].avg_speed_kmph)
            service_minutes = compatible_tasks[from_node].service_minutes if is_task_node(from_node) else 0
            return max(0, math.ceil(travel_minutes + service_minutes))

        distance_callback_index = routing.RegisterTransitCallback(distance_callback)
        time_callback_index = routing.RegisterTransitCallback(time_callback)
        routing.SetArcCostEvaluatorOfVehicle(distance_callback_index, vehicle_index)
        distance_callbacks.append(distance_callback_index)
        time_callbacks.append(time_callback_index)

    routing.AddDimensionWithVehicleTransits(
        time_callbacks,
        horizon_minutes,
        horizon_minutes,
        False,
        "Time",
    )
    time_dimension = routing.GetDimensionOrDie("Time")

    for vehicle_index, vehicle in enumerate(vehicles):
        start_index = routing.Start(vehicle_index)
        available_minutes = max(
            0,
            math.ceil((vehicle.available_at - reference_time).total_seconds() / 60.0),
        )
        time_dimension.CumulVar(start_index).SetRange(available_minutes, horizon_minutes)

    for task_index, task in enumerate(compatible_tasks):
        routing_index = manager.NodeToIndex(task_index)
        allowed_vehicles = [
            vehicle_index
            for vehicle_index, vehicle in enumerate(vehicles)
            if _vehicle_can_handle(dataset, vehicle, task)
        ]
        disallowed_vehicles = [
            vehicle_index for vehicle_index in range(vehicle_count) if vehicle_index not in allowed_vehicles
        ]
        for vehicle_index in disallowed_vehicles:
            routing.VehicleVar(routing_index).RemoveValue(vehicle_index)
        routing.AddDisjunction([routing_index], _drop_penalty(task))

        earliest_minutes = max(
            0,
            math.ceil((task.earliest_start - reference_time).total_seconds() / 60.0),
        )
        shift_end_minutes = max(
            earliest_minutes,
            math.ceil((task.shift_end - reference_time).total_seconds() / 60.0),
        )
        sla_minutes = max(
            earliest_minutes,
            math.ceil((task.sla_deadline - reference_time).total_seconds() / 60.0),
        )
        time_dimension.CumulVar(routing_index).SetRange(earliest_minutes, shift_end_minutes)
        time_dimension.SetCumulVarSoftUpperBound(routing_index, sla_minutes, _sla_penalty(task))

    search_parameters = pywrapcp.DefaultRoutingSearchParameters()
    search_parameters.first_solution_strategy = routing_enums_pb2.FirstSolutionStrategy.PARALLEL_CHEAPEST_INSERTION
    search_parameters.local_search_metaheuristic = routing_enums_pb2.LocalSearchMetaheuristic.GUIDED_LOCAL_SEARCH
    search_parameters.time_limit.seconds = max(1, time_limit_seconds)

    solution = routing.SolveWithParameters(search_parameters)
    if solution is None:
        return BatchPlan(
            strategy="ortools_solver",
            assignments=[],
            unassigned_task_ids=[task.task_id for task in tasks],
            summary="OR-Tools could not find a feasible solution for the provided scenario.",
        )

    visited_task_ids: set[str] = set()
    assignments: list[PlanAssignment] = []
    for vehicle_index, vehicle in enumerate(vehicles):
        index = routing.Start(vehicle_index)
        ordered_tasks: list[Task] = []
        while not routing.IsEnd(index):
            next_index = solution.Value(routing.NextVar(index))
            if routing.IsEnd(next_index):
                break
            original_node = manager.IndexToNode(next_index)
            if is_task_node(original_node):
                task = compatible_tasks[original_node]
                ordered_tasks.append(task)
                visited_task_ids.add(task.task_id)
            index = next_index

        if ordered_tasks:
            assignments.append(
                build_assignment_from_ordered_tasks(
                    graph=graph,
                    vehicle=vehicle,
                    ordered_tasks=ordered_tasks,
                    dataset=dataset,
                    reference_time=reference_time,
                    explanation="План оптимизирован OR-Tools с учетом временных окон и мягких штрафов по SLA.",
                )
            )

    unassigned_task_ids.extend(
        task.task_id for task in compatible_tasks if task.task_id not in visited_task_ids
    )

    return BatchPlan(
        strategy="ortools_solver",
        assignments=assignments,
        unassigned_task_ids=unassigned_task_ids,
        summary=f"Assigned {sum(len(assignment.task_ids) for assignment in assignments)} tasks with OR-Tools.",
    )


def build_assignment_from_ordered_tasks(
    *,
    graph: RoadGraph,
    vehicle: Vehicle,
    ordered_tasks: list[Task],
    dataset: Dataset,
    reference_time: datetime,
    explanation: str | None = None,
) -> PlanAssignment:
    current_node = vehicle.current_node
    current_time = max(vehicle.available_at, reference_time)
    route_legs: list[RouteLeg] = []
    total_distance_m = 0.0
    total_travel_minutes = 0.0
    started_at = current_time

    for task in ordered_tasks:
        destination_node = _destination_node(dataset, task)
        route = graph.shortest_path(current_node, destination_node)
        travel_minutes = graph.travel_minutes(route.distance_m, vehicle.avg_speed_kmph)
        arrival_at = current_time + timedelta(minutes=travel_minutes)
        service_start = max(arrival_at, task.earliest_start)
        service_end = service_start + timedelta(minutes=task.service_minutes)
        route_legs.append(
            RouteLeg(
                task_id=task.task_id,
                route=route,
                arrival_at=arrival_at,
                service_start=service_start,
                service_end=service_end,
            )
        )
        total_distance_m += route.distance_m
        total_travel_minutes += travel_minutes
        current_node = destination_node
        current_time = service_end

    return PlanAssignment(
        vehicle_id=vehicle.vehicle_id,
        vehicle_name=vehicle.name,
        task_ids=[task.task_id for task in ordered_tasks],
        route_legs=route_legs,
        total_distance_m=total_distance_m,
        total_travel_minutes=total_travel_minutes,
        started_at=started_at,
        finished_at=current_time,
        explanation=explanation,
    )


def _compute_horizon_minutes(tasks: list[Task], reference_time: datetime) -> int:
    horizon_end = max(task.shift_end for task in tasks) + timedelta(hours=12)
    return max(1, math.ceil((horizon_end - reference_time).total_seconds() / 60.0))


def _destination_node(dataset: Dataset, task: Task) -> int:
    well = dataset.well_lookup()[task.destination_uwi]
    if well.nearest_node_id is None:
        raise ValueError(f"Well {well.uwi} is missing nearest_node_id")
    return well.nearest_node_id


def _vehicle_can_handle(dataset: Dataset, vehicle: Vehicle, task: Task) -> bool:
    compatible_types = dataset.compatibility.get(task.task_type, set())
    return vehicle.can_handle(task.task_type) or vehicle.vehicle_type in compatible_types


def _drop_penalty(task: Task) -> int:
    if task.priority.value == "high":
        return 200_000
    if task.priority.value == "medium":
        return 100_000
    return 50_000


def _sla_penalty(task: Task) -> int:
    if task.priority.value == "high":
        return 600
    if task.priority.value == "medium":
        return 250
    return 80

