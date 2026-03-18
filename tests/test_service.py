from uto_routing.service import RoutingPlatform


def test_recommendations_return_ranked_units() -> None:
    platform = RoutingPlatform()
    task = platform.dataset.tasks[0]

    result = platform.recommend(task_id=task.task_id, strategy="priority_greedy", top_k=3)

    assert result["task_id"] == task.task_id
    assert len(result["units"]) == 3
    assert result["units"][0]["score"] >= result["units"][1]["score"]


def test_batch_plan_assigns_tasks() -> None:
    platform = RoutingPlatform()

    plan = platform.batch_plan(strategy="priority_greedy")

    assert plan["strategy"] == "priority_greedy"
    assert len(plan["assignments"]) > 0
    assert "metrics" in plan
    assert "comparison_vs_baseline" in plan
    assigned_task_ids = {task_id for assignment in plan["assignments"] for task_id in assignment["task_ids"]}
    assert assigned_task_ids


def test_ortools_plan_and_audit_trail() -> None:
    platform = RoutingPlatform()

    plan = platform.batch_plan(strategy="ortools_solver")
    audit = platform.audit_events(limit=10)

    assert plan["strategy"] == "ortools_solver"
    assert "metrics" in plan
    assert any(event["action"] == "plan" and event["strategy"] == "ortools_solver" for event in audit["events"])


def test_replay_and_tuning() -> None:
    platform = RoutingPlatform()

    replay = platform.replay(strategy="priority_greedy", frame_interval_minutes=30)
    tuning = platform.tune_weights(candidate_limit=4)

    assert replay["strategy"] == "priority_greedy"
    assert replay["playback"]["frames"]
    assert tuning["best_candidate"]["weights"]

