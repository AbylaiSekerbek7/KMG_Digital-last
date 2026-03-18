from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from enum import Enum


class Priority(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class Shift(str, Enum):
    DAY = "day"
    NIGHT = "night"


PRIORITY_WEIGHT = {
    Priority.HIGH: 0.55,
    Priority.MEDIUM: 0.35,
    Priority.LOW: 0.10,
}

PRIORITY_SLA_HOURS = {
    Priority.HIGH: 2,
    Priority.MEDIUM: 5,
    Priority.LOW: 12,
}

SHIFT_RULES = {
    Shift.DAY: (time(8, 0), time(20, 0)),
    Shift.NIGHT: (time(20, 0), time(8, 0)),
}


def resolve_start_day(
    planned_start: datetime,
    shift: Shift,
    explicit_start_day: date | str | None = None,
) -> date:
    """Infer the planning start day for a task.

    Night-shift tasks may be recorded after midnight while still belonging to the
    previous operational day. If `start_day` is not explicitly provided, infer it
    from the timestamp and shift rules.
    """

    if explicit_start_day is not None:
        if isinstance(explicit_start_day, str):
            return date.fromisoformat(explicit_start_day)
        return explicit_start_day

    if shift is Shift.NIGHT and planned_start.time() < SHIFT_RULES[Shift.NIGHT][1]:
        return planned_start.date() - timedelta(days=1)
    return planned_start.date()


@dataclass(frozen=True)
class Node:
    node_id: int
    lon: float
    lat: float


@dataclass(frozen=True)
class Edge:
    source: int
    target: int
    weight_m: float


@dataclass(frozen=True)
class Well:
    uwi: str
    lon: float
    lat: float
    well_name: str
    nearest_node_id: int | None = None


@dataclass
class Vehicle:
    vehicle_id: int
    name: str
    vehicle_type: str
    current_node: int
    lon: float
    lat: float
    available_at: datetime
    avg_speed_kmph: float
    skills: set[str] = field(default_factory=set)
    registration_plate: str | None = None

    def can_handle(self, task_type: str) -> bool:
        return task_type in self.skills


@dataclass(frozen=True)
class Task:
    task_id: str
    priority: Priority
    planned_start: datetime
    planned_duration_hours: float
    destination_uwi: str
    task_type: str
    shift: Shift
    start_day: date

    @property
    def service_minutes(self) -> int:
        return max(1, round(self.planned_duration_hours * 60))

    @property
    def shift_start(self) -> datetime:
        start_clock, _ = SHIFT_RULES[self.shift]
        return datetime.combine(self.start_day, start_clock)

    @property
    def shift_end(self) -> datetime:
        _, end_clock = SHIFT_RULES[self.shift]
        end_day = self.start_day
        if self.shift is Shift.NIGHT:
            end_day = self.start_day + timedelta(days=1)
        return datetime.combine(end_day, end_clock)

    @property
    def earliest_start(self) -> datetime:
        return max(self.planned_start, self.shift_start)

    @property
    def sla_deadline(self) -> datetime:
        return self.planned_start + timedelta(hours=PRIORITY_SLA_HOURS[self.priority])

    @property
    def priority_weight(self) -> float:
        return PRIORITY_WEIGHT[self.priority]


@dataclass
class Dataset:
    nodes: list[Node]
    edges: list[Edge]
    wells: list[Well]
    vehicles: list[Vehicle]
    tasks: list[Task]
    compatibility: dict[str, set[str]]
    metadata: dict[str, str] = field(default_factory=dict)

    def node_lookup(self) -> dict[int, Node]:
        return {node.node_id: node for node in self.nodes}

    def well_lookup(self) -> dict[str, Well]:
        return {well.uwi: well for well in self.wells}

    def task_lookup(self) -> dict[str, Task]:
        return {task.task_id: task for task in self.tasks}

    def vehicle_lookup(self) -> dict[int, Vehicle]:
        return {vehicle.vehicle_id: vehicle for vehicle in self.vehicles}


@dataclass(frozen=True)
class Route:
    start_node: int
    end_node: int
    distance_m: float
    path_nodes: list[int]
    coords: list[tuple[float, float]]

    @property
    def distance_km(self) -> float:
        return self.distance_m / 1000.0


@dataclass(frozen=True)
class VehicleEvaluation:
    vehicle_id: int
    name: str
    vehicle_type: str
    compatible: bool
    distance_m: float
    travel_minutes: float
    wait_minutes: float
    arrival_at: datetime
    service_start: datetime
    late_minutes: float
    shift_violation_minutes: float
    score: float
    cost: float
    score_breakdown: dict[str, float]
    reason: str


@dataclass(frozen=True)
class RouteLeg:
    task_id: str
    route: Route
    arrival_at: datetime
    service_start: datetime
    service_end: datetime


@dataclass
class PlanAssignment:
    vehicle_id: int
    vehicle_name: str
    task_ids: list[str]
    route_legs: list[RouteLeg]
    total_distance_m: float
    total_travel_minutes: float
    started_at: datetime
    finished_at: datetime
    explanation: str | None = None


@dataclass
class BatchPlan:
    strategy: str
    assignments: list[PlanAssignment]
    unassigned_task_ids: list[str]
    summary: str

