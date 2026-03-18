from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime
import csv
import io
from typing import Any
from uuid import uuid4

from uto_routing.audit import AuditTrailStore
from uto_routing.benchmark import compare_metrics, enrich_metrics, round_metrics, run_benchmark, simulate_batch_plan
from uto_routing.config import RuntimeSettings, get_settings
from uto_routing.data_loading import dataset_summary, load_dataset
from uto_routing.graph import RoadGraph
from uto_routing.models import BatchPlan, Dataset, Priority, Shift, Task, resolve_start_day
from uto_routing.planners import evaluate_multitask_grouping, plan_batch, recommend_for_task
from uto_routing.scoring import ScoringWeights
from uto_routing.replay import run_historical_replay
from uto_routing.storage import ApplicationStore
from uto_routing.tuning import run_weight_tuning


class RoutingPlatform:
    SUPPORTED_STRATEGIES = {"baseline", "priority_greedy", "multistop_heuristic", "ortools_solver"}

    def __init__(self, data_dir: str | None = None, *, settings: RuntimeSettings | None = None) -> None:
        self.settings = settings or get_settings()
        self.data_dir = data_dir or self.settings.data_dir
        self.store = ApplicationStore(self.settings)
        self.store.initialize()
        self.audit_trail = AuditTrailStore(max_entries=self.settings.audit_max_entries, backend=self.store)
        self.dataset = load_dataset(data_dir=self.data_dir, settings=self.settings)
        self.graph = RoadGraph.from_dataset(self.dataset)
        self._normalize_dataset()

    def refresh(self) -> None:
        self.dataset = load_dataset(data_dir=self.data_dir, settings=self.settings)
        self.graph = RoadGraph.from_dataset(self.dataset)
        self._normalize_dataset()
        self.audit_trail.record(
            action="dataset_reload",
            strategy=None,
            summary=f"Данные перезагружены в режиме {self.dataset.metadata.get('dataset_mode', 'unknown')}.",
            request={"data_source": self.settings.data_source, "data_dir": self.data_dir},
            response={"summary": self.summary()},
        )

    def summary(self) -> dict[str, Any]:
        result = dataset_summary(self.dataset)
        result["strategies"] = ["baseline", "priority_greedy", "multistop_heuristic", "ortools_solver"]
        result["metadata"] = dict(self.dataset.metadata)
        result["task_breakdown"] = {
            "by_priority": dict(Counter(task.priority.value for task in self.dataset.tasks)),
            "by_shift": dict(Counter(task.shift.value for task in self.dataset.tasks)),
            "by_task_type": dict(Counter(task.task_type for task in self.dataset.tasks)),
        }
        result["runtime"] = {
            "data_source": self.settings.data_source,
            "audit_max_entries": self.settings.audit_max_entries,
            "ortools_time_limit_seconds": self.settings.ortools_time_limit_seconds,
            "scoring_weights": self.settings.scoring_weights.as_dict(),
        }
        return result

    def catalog(self) -> dict[str, Any]:
        return {
            "summary": self.summary(),
            "strategies": sorted(self.SUPPORTED_STRATEGIES),
            "task_types": sorted(self.dataset.compatibility.keys()),
            "compatibility": {
                task_type: sorted(vehicle_types)
                for task_type, vehicle_types in sorted(self.dataset.compatibility.items())
            },
            "tasks": [
                {
                    "task_id": task.task_id,
                    "priority": task.priority.value,
                    "planned_start": task.planned_start.isoformat(),
                    "earliest_start": task.earliest_start.isoformat(),
                    "sla_deadline": task.sla_deadline.isoformat(),
                    "start_day": task.start_day.isoformat(),
                    "planned_duration_hours": task.planned_duration_hours,
                    "destination_uwi": task.destination_uwi,
                    "task_type": task.task_type,
                    "shift": task.shift.value,
                }
                for task in self.dataset.tasks
            ],
            "wells": [
                {
                    "uwi": well.uwi,
                    "well_name": well.well_name,
                    "lon": well.lon,
                    "lat": well.lat,
                    "nearest_node_id": well.nearest_node_id,
                }
                for well in self.dataset.wells
            ],
            "vehicles": [
                {
                    "vehicle_id": vehicle.vehicle_id,
                    "name": vehicle.name,
                    "vehicle_type": vehicle.vehicle_type,
                    "current_node": vehicle.current_node,
                    "lon": vehicle.lon,
                    "lat": vehicle.lat,
                    "available_at": vehicle.available_at.isoformat(),
                    "avg_speed_kmph": vehicle.avg_speed_kmph,
                    "skills": sorted(vehicle.skills),
                }
                for vehicle in self.dataset.vehicles
            ],
        }

    def audit_events(self, *, limit: int = 50, action: str | None = None) -> dict[str, Any]:
        return {
            "events": self.audit_trail.list(limit=limit, action=action),
            "limit": limit,
            "action": action,
        }

    def clear_audit_events(self) -> dict[str, str]:
        self.audit_trail.clear()
        return {"status": "cleared"}

    def benchmark_reports(self, *, limit: int = 20) -> dict[str, Any]:
        return {
            "reports": self.store.list_reports(report_type="benchmark", limit=limit),
            "limit": limit,
        }

    def benchmark_report(self, report_id: str | None = None, *, latest: bool = False) -> dict[str, Any] | None:
        if latest:
            return self.store.get_report(latest=True, report_type="benchmark")
        if report_id is None:
            raise ValueError("report_id is required unless latest=True")
        return self.store.get_report(report_id, report_type="benchmark")

    def benchmark_report_csv(self, report_id: str | None = None, *, latest: bool = False) -> str:
        report = self.benchmark_report(report_id, latest=latest)
        if report is None:
            raise ValueError("Benchmark report not found.")
        payload = report["payload"]
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(
            [
                "report_id",
                "strategy",
                "total_distance_km",
                "weighted_lateness",
                "high_priority_on_time_rate",
                "runtime_ms",
                "assignment_rate",
                "distance_win_rate_vs_baseline",
                "lateness_win_rate_vs_baseline",
            ]
        )
        for strategy, metrics in payload["strategies"].items():
            writer.writerow(
                [
                    payload.get("report_id", report["report_id"]),
                    strategy,
                    metrics.get("total_distance_km"),
                    metrics.get("weighted_lateness"),
                    metrics.get("high_priority_on_time_rate"),
                    metrics.get("runtime_ms"),
                    metrics.get("assignment_rate"),
                    metrics.get("distance_win_rate_vs_baseline"),
                    metrics.get("lateness_win_rate_vs_baseline"),
                ]
            )
        return output.getvalue()

    def replay(
        self,
        *,
        strategy: str = "priority_greedy",
        frame_interval_minutes: int = 15,
    ) -> dict[str, Any]:
        self._validate_strategy(strategy)
        result = run_historical_replay(
            graph=self.graph,
            dataset=self.dataset,
            reference_time=self.reference_time(),
            strategy=strategy,
            scoring_weights=self.settings.scoring_weights,
            ortools_time_limit_seconds=self.settings.ortools_time_limit_seconds,
            frame_interval_minutes=frame_interval_minutes,
        )
        audit_response = {
            "strategy": result.get("strategy"),
            "reference_time": result.get("reference_time"),
            "metrics": result.get("metrics"),
            "playback": {
                "frame_interval_minutes": result.get("playback", {}).get("frame_interval_minutes"),
                "start_time": result.get("playback", {}).get("start_time"),
                "end_time": result.get("playback", {}).get("end_time"),
                "total_frames": len(result.get("playback", {}).get("frames", [])),
            },
        }
        self.audit_trail.record(
            action="replay",
            strategy=strategy,
            summary=f"Запущено проигрывание сценария {strategy}.",
            request={"strategy": strategy, "frame_interval_minutes": frame_interval_minutes},
            response=audit_response,
        )
        return result

    def tune_weights(self, *, candidate_limit: int = 12) -> dict[str, Any]:
        result = run_weight_tuning(
            graph=self.graph,
            dataset=self.dataset,
            reference_time=self.reference_time(),
            base_weights=self.settings.scoring_weights,
            candidate_limit=candidate_limit,
        )
        report_id = str(uuid4())
        result["report_id"] = report_id
        self.store.save_report(
            report_id=report_id,
            report_type="tuning",
            created_at=datetime.now(UTC).isoformat(),
            name=f"tuning-{report_id[:8]}",
            summary=f"Подбор весов по {candidate_limit} кандидатам.",
            payload=result,
        )
        self.audit_trail.record(
            action="tuning",
            strategy="priority_greedy",
            summary=f"Запущен подбор весов скоринга по {candidate_limit} кандидатам.",
            request={"candidate_limit": candidate_limit},
            response=result,
        )
        return result

    def live_state(self) -> dict[str, Any]:
        latest_plan = self.audit_trail.latest(action="plan")
        latest_route = self.audit_trail.latest(action="route")
        latest_replay = self.audit_trail.latest(action="replay")
        catalog = self.catalog()
        replay_meta = None
        if latest_replay is not None:
            replay_response = latest_replay["response"]
            playback = replay_response.get("playback", {})
            replay_meta = {
                "strategy": replay_response.get("strategy"),
                "reference_time": replay_response.get("reference_time"),
                "start_time": playback.get("start_time"),
                "end_time": playback.get("end_time"),
                "frame_interval_minutes": playback.get("frame_interval_minutes"),
                "total_frames": playback.get("total_frames", len(playback.get("frames", []))),
            }
        return {
            "reference_time": self.reference_time().isoformat(),
            "map_defaults": {
                "lat": self.settings.map_default_lat,
                "lon": self.settings.map_default_lon,
                "zoom": self.settings.map_default_zoom,
            },
            "vehicles": catalog["vehicles"],
            "tasks": catalog["tasks"],
            "wells": catalog["wells"],
            "latest_plan": latest_plan["response"] if latest_plan is not None else None,
            "latest_route": latest_route["response"] if latest_route is not None else None,
            "latest_replay": replay_meta,
        }

    def recommend(
        self,
        *,
        task_id: str | None = None,
        payload: dict[str, Any] | None = None,
        strategy: str = "priority_greedy",
        top_k: int = 3,
    ) -> dict[str, Any]:
        self._validate_strategy(strategy)
        task = self._resolve_task(task_id=task_id, payload=payload)
        reference_time = self.reference_time(task_override=task)
        vehicle_lookup = self.dataset.vehicle_lookup()
        evaluations = recommend_for_task(
            graph=self.graph,
            dataset=self.dataset,
            task=task,
            reference_time=reference_time,
            strategy=strategy,
            top_k=top_k,
            scoring_weights=self.settings.scoring_weights,
        )
        warnings: list[str] = []
        if not any(item.compatible for item in evaluations):
            warnings.append("No compatible vehicles were found; results contain fallback candidates.")
        elif any(not item.compatible for item in evaluations):
            warnings.append("Some fallback candidates are incompatible and should be treated as last resort options.")

        response = {
            "task_id": task.task_id,
            "strategy": strategy,
            "reference_time": reference_time.isoformat(),
            "warnings": warnings,
            "task_context": {
                "priority": task.priority.value,
                "planned_start": task.planned_start.isoformat(),
                "earliest_start": task.earliest_start.isoformat(),
                "sla_deadline": task.sla_deadline.isoformat(),
                "planned_duration_hours": task.planned_duration_hours,
                "destination_uwi": task.destination_uwi,
                "task_type": task.task_type,
                "shift": task.shift.value,
                "start_day": task.start_day.isoformat(),
            },
            "units": [
                {
                    "wialon_id": item.vehicle_id,
                    "name": item.name,
                    "vehicle_type": item.vehicle_type,
                    "compatible": item.compatible,
                    "eta_minutes": round((item.service_start - reference_time).total_seconds() / 60.0),
                    "arrival_minutes": round((item.arrival_at - reference_time).total_seconds() / 60.0),
                    "distance_km": round(item.distance_m / 1000.0, 2),
                    "travel_minutes": round(item.travel_minutes, 1),
                    "wait_minutes": round(item.wait_minutes, 1),
                    "late_minutes": round(item.late_minutes, 1),
                    "arrival_at": item.arrival_at.isoformat(),
                    "service_start_at": item.service_start.isoformat(),
                    "available_at": vehicle_lookup[item.vehicle_id].available_at.isoformat(),
                    "score": round(item.score, 4),
                    "cost": round(item.cost, 4),
                    "score_breakdown": item.score_breakdown,
                    "reason": item.reason,
                }
                for item in evaluations
            ],
        }
        top_unit = response["units"][0] if response["units"] else None
        self.audit_trail.record(
            action="recommendation",
            strategy=strategy,
            summary=(
                f"Рекомендована машина {top_unit['wialon_id']} для заявки {task.task_id}."
                if top_unit is not None
                else f"Для заявки {task.task_id} не найдено кандидатов."
            ),
            request={
                "task_id": task_id,
                "payload": payload or {},
                "strategy": strategy,
                "top_k": top_k,
            },
            response=response,
        )
        return response

    def route(
        self,
        *,
        from_vehicle_id: int | None = None,
        from_lon: float | None = None,
        from_lat: float | None = None,
        to_uwi: str | None = None,
        to_lon: float | None = None,
        to_lat: float | None = None,
        speed_kmph: float | None = None,
    ) -> dict[str, Any]:
        if from_vehicle_id is not None:
            vehicle = self.dataset.vehicle_lookup()[from_vehicle_id]
            start_node = vehicle.current_node
            chosen_speed = speed_kmph or vehicle.avg_speed_kmph
        elif from_lon is not None and from_lat is not None:
            start_node = self.graph.snap_to_node(from_lon, from_lat)
            chosen_speed = speed_kmph or 24.0
        else:
            raise ValueError("Either from_vehicle_id or from coordinates must be provided.")

        if to_uwi is not None:
            well = self.dataset.well_lookup()[to_uwi]
            if well.nearest_node_id is None:
                raise ValueError(f"Well {to_uwi} is missing nearest node.")
            end_node = well.nearest_node_id
        elif to_lon is not None and to_lat is not None:
            end_node = self.graph.snap_to_node(to_lon, to_lat)
        else:
            raise ValueError("Either to_uwi or to coordinates must be provided.")

        route = self.graph.shortest_path(start_node, end_node)
        response = {
            "distance_km": round(route.distance_km, 2),
            "time_minutes": round(self.graph.travel_minutes(route.distance_m, chosen_speed), 1),
            "nodes": route.path_nodes,
            "coords": [[lon, lat] for lon, lat in route.coords],
        }
        self.audit_trail.record(
            action="route",
            strategy=None,
            summary=f"Построен маршрут от узла {start_node} до узла {end_node}.",
            request={
                "from_vehicle_id": from_vehicle_id,
                "from_lon": from_lon,
                "from_lat": from_lat,
                "to_uwi": to_uwi,
                "to_lon": to_lon,
                "to_lat": to_lat,
                "speed_kmph": speed_kmph,
            },
            response=response,
        )
        return response

    def multitask(
        self,
        task_ids: list[str],
        *,
        max_total_time_minutes: float = 480.0,
        max_detour_ratio: float = 1.3,
    ) -> dict[str, Any]:
        tasks = [self.dataset.task_lookup()[task_id] for task_id in task_ids]
        result = evaluate_multitask_grouping(
            graph=self.graph,
            dataset=self.dataset,
            tasks=tasks,
            reference_time=(reference_time := self.reference_time()),
            max_total_time_minutes=max_total_time_minutes,
            max_detour_ratio=max_detour_ratio,
        )
        result["reference_time"] = reference_time.isoformat()
        result["selected_task_ids"] = task_ids
        result["constraints"] = {
            "max_total_time_minutes": max_total_time_minutes,
            "max_detour_ratio": max_detour_ratio,
        }
        self.audit_trail.record(
            action="multitask",
            strategy=result["strategy_summary"],
            summary=f"Оценена группировка для {len(task_ids)} заявок.",
            request={
                "task_ids": task_ids,
                "max_total_time_minutes": max_total_time_minutes,
                "max_detour_ratio": max_detour_ratio,
            },
            response=result,
        )
        return result

    def batch_plan(
        self,
        *,
        task_ids: list[str] | None = None,
        strategy: str = "priority_greedy",
    ) -> dict[str, Any]:
        self._validate_strategy(strategy)
        tasks = self.dataset.tasks if task_ids is None else [self.dataset.task_lookup()[task_id] for task_id in task_ids]
        reference_time = self.reference_time()
        plan = plan_batch(
            graph=self.graph,
            dataset=self.dataset,
            tasks=tasks,
            reference_time=reference_time,
            strategy=strategy,
            ortools_time_limit_seconds=self.settings.ortools_time_limit_seconds,
            scoring_weights=self.settings.scoring_weights,
        )
        response = self._plan_to_dict(plan)
        response["reference_time"] = reference_time.isoformat()
        metrics = round_metrics(enrich_metrics(simulate_batch_plan(plan, self.dataset, reference_time)))
        response["metrics"] = metrics
        response["task_count"] = len(tasks)

        if strategy != "baseline":
            baseline_plan = plan_batch(
                graph=self.graph,
                dataset=self.dataset,
                tasks=tasks,
                reference_time=reference_time,
                strategy="baseline",
                ortools_time_limit_seconds=self.settings.ortools_time_limit_seconds,
                scoring_weights=self.settings.scoring_weights,
            )
            baseline_metrics = round_metrics(enrich_metrics(simulate_batch_plan(baseline_plan, self.dataset, reference_time)))
            response["baseline_metrics"] = baseline_metrics
            response["comparison_vs_baseline"] = compare_metrics(metrics, baseline_metrics)

        self.audit_trail.record(
            action="plan",
            strategy=strategy,
            summary=f"Построен план {strategy} для {len(tasks)} заявок.",
            request={"task_ids": task_ids, "strategy": strategy},
            response=response,
        )
        return response

    def benchmark(
        self,
        *,
        scenarios: int = 250,
        min_tasks: int = 6,
        max_tasks: int = 12,
        min_vehicles: int = 4,
        max_vehicles: int = 7,
        seed: int = 42,
    ) -> dict[str, Any]:
        result = run_benchmark(
            base_dataset=self.dataset,
            graph=self.graph,
            scenarios=scenarios,
            min_tasks=min_tasks,
            max_tasks=max_tasks,
            min_vehicles=min_vehicles,
            max_vehicles=max_vehicles,
            seed=seed,
            ortools_time_limit_seconds=self.settings.ortools_time_limit_seconds,
            scoring_weights=self.settings.scoring_weights,
        )
        report_id = str(uuid4())
        result["report_id"] = report_id
        self.store.save_report(
            report_id=report_id,
            report_type="benchmark",
            created_at=datetime.now(UTC).isoformat(),
            name=f"benchmark-{report_id[:8]}",
            summary=f"Бенчмарк на {scenarios} сценариях.",
            payload=result,
        )
        self.audit_trail.record(
            action="benchmark",
            strategy=None,
            summary=f"Запущен бенчмарк на {scenarios} сценариях.",
            request={
                "scenarios": scenarios,
                "min_tasks": min_tasks,
                "max_tasks": max_tasks,
                "min_vehicles": min_vehicles,
                "max_vehicles": max_vehicles,
                "seed": seed,
            },
            response=result,
        )
        return result

    def reference_time(self, task_override: Task | None = None) -> datetime:
        timestamps = [vehicle.available_at for vehicle in self.dataset.vehicles]
        timestamps.extend(task.planned_start for task in self.dataset.tasks)
        if task_override is not None:
            timestamps.append(task_override.planned_start)
        return min(timestamps)

    def _normalize_dataset(self) -> None:
        snapped_wells = []
        for well in self.dataset.wells:
            if well.nearest_node_id is None:
                snapped_wells.append(
                    type(well)(
                        uwi=well.uwi,
                        lon=well.lon,
                        lat=well.lat,
                        well_name=well.well_name,
                        nearest_node_id=self.graph.snap_to_node(well.lon, well.lat),
                    )
                )
            else:
                snapped_wells.append(well)
        self.dataset.wells = snapped_wells

        for vehicle in self.dataset.vehicles:
            if vehicle.current_node < 0 or vehicle.current_node not in self.graph.index.nodes:
                vehicle.current_node = self.graph.snap_to_node(vehicle.lon, vehicle.lat)

    def _resolve_task(self, *, task_id: str | None, payload: dict[str, Any] | None) -> Task:
        if task_id is not None and task_id in self.dataset.task_lookup():
            return self.dataset.task_lookup()[task_id]
        if payload is None:
            raise ValueError("Task payload is required when task_id is not provided.")

        if payload.get("task_id") and payload["task_id"] in self.dataset.task_lookup():
            return self.dataset.task_lookup()[payload["task_id"]]

        missing_fields = [
            field
            for field in ("priority", "destination_uwi", "planned_start")
            if not payload.get(field)
        ]
        if missing_fields:
            missing = ", ".join(missing_fields)
            raise ValueError(
                f"Unknown task_id '{task_id or payload.get('task_id')}' and missing custom task fields: {missing}"
            )
        if payload.get("duration_hours") is None and payload.get("planned_duration_hours") is None:
            raise ValueError("Custom tasks require 'duration_hours' or 'planned_duration_hours'.")

        try:
            planned_start = datetime.fromisoformat(payload["planned_start"])
        except (TypeError, ValueError) as exc:
            raise ValueError("planned_start must be a valid ISO datetime string.") from exc

        shift = payload.get("shift")
        if shift is None:
            shift = "day" if 8 <= planned_start.hour < 20 else "night"
        try:
            shift_enum = Shift(str(shift).lower())
        except ValueError as exc:
            raise ValueError("shift must be either 'day' or 'night'.") from exc
        task_type = payload.get("task_type", "inspection")
        if task_type not in self.dataset.compatibility:
            supported = ", ".join(sorted(self.dataset.compatibility))
            raise ValueError(f"Unsupported task_type '{task_type}'. Expected one of: {supported}")
        if payload["destination_uwi"] not in self.dataset.well_lookup():
            raise ValueError(f"Unknown destination_uwi: {payload['destination_uwi']}")

        duration_hours = float(payload.get("duration_hours", payload.get("planned_duration_hours", 4.0)))
        if duration_hours <= 0:
            raise ValueError("Duration must be positive.")

        return Task(
            task_id=str(payload.get("task_id", "custom-task")),
            priority=Priority(str(payload["priority"]).lower()),
            planned_start=planned_start,
            planned_duration_hours=duration_hours,
            destination_uwi=payload["destination_uwi"],
            task_type=task_type,
            shift=shift_enum,
            start_day=resolve_start_day(planned_start, shift_enum, payload.get("start_day")),
        )

    def _validate_strategy(self, strategy: str) -> None:
        if strategy not in self.SUPPORTED_STRATEGIES:
            supported = ", ".join(sorted(self.SUPPORTED_STRATEGIES))
            raise ValueError(f"Unsupported strategy '{strategy}'. Expected one of: {supported}")

    def _plan_to_dict(self, plan: BatchPlan) -> dict[str, Any]:
        return {
            "strategy": plan.strategy,
            "summary": plan.summary,
            "unassigned_task_ids": plan.unassigned_task_ids,
            "assignments": [
                {
                    "vehicle_id": assignment.vehicle_id,
                    "vehicle_name": assignment.vehicle_name,
                    "task_ids": assignment.task_ids,
                    "total_distance_km": round(assignment.total_distance_m / 1000.0, 2),
                    "total_travel_minutes": round(assignment.total_travel_minutes, 1),
                    "started_at": assignment.started_at.isoformat(),
                    "finished_at": assignment.finished_at.isoformat(),
                    "explanation": assignment.explanation,
                    "legs": [
                        {
                            "task_id": leg.task_id,
                            "arrival_at": leg.arrival_at.isoformat(),
                            "service_start": leg.service_start.isoformat(),
                            "service_end": leg.service_end.isoformat(),
                            "distance_km": round(leg.route.distance_km, 2),
                            "nodes": leg.route.path_nodes,
                            "coords": [[lon, lat] for lon, lat in leg.route.coords],
                        }
                        for leg in assignment.route_legs
                    ],
                }
                for assignment in plan.assignments
            ],
        }

