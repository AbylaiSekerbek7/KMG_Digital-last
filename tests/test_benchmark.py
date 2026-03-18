from uto_routing.sample_data import create_sample_dataset
from uto_routing.graph import RoadGraph
from uto_routing.benchmark import run_benchmark


def test_benchmark_returns_strategy_report() -> None:
    dataset = create_sample_dataset()
    graph = RoadGraph.from_dataset(dataset)

    report = run_benchmark(
        base_dataset=dataset,
        graph=graph,
        scenarios=5,
        min_tasks=4,
        max_tasks=5,
        min_vehicles=3,
        max_vehicles=4,
        seed=1,
    )

    assert report["scenarios"] == 5
    assert "baseline" in report["strategies"]
    assert "priority_greedy" in report["strategies"]
    assert "multistop_heuristic" in report["strategies"]
    assert "ortools_solver" in report["strategies"]
    assert report["strategies"]["baseline"]["total_distance_km"] > 0
    assert "assignment_rate" in report["strategies"]["baseline"]
    assert "comparison_vs_baseline" in report["strategies"]["priority_greedy"]

