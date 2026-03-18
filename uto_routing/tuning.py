from __future__ import annotations

from datetime import datetime
from typing import Any

from uto_routing.benchmark import enrich_metrics, round_metrics, simulate_batch_plan
from uto_routing.planners import plan_batch
from uto_routing.scoring import DEFAULT_SCORING_WEIGHTS, ScoringWeights


def run_weight_tuning(
    *,
    graph,
    dataset,
    reference_time: datetime,
    base_weights: ScoringWeights = DEFAULT_SCORING_WEIGHTS,
    candidate_limit: int = 12,
) -> dict[str, Any]:
    candidates = generate_weight_candidates(base_weights)[:candidate_limit]
    evaluations: list[dict[str, Any]] = []

    for index, weights in enumerate(candidates, start=1):
        plan = plan_batch(
            graph=graph,
            dataset=dataset,
            tasks=dataset.tasks,
            reference_time=reference_time,
            strategy="priority_greedy",
            scoring_weights=weights,
        )
        metrics = round_metrics(enrich_metrics(simulate_batch_plan(plan, dataset, reference_time)))
        objective = (
            metrics["weighted_lateness"] * 100.0
            + metrics["total_distance_km"] * 5.0
            + metrics["unassigned_tasks"] * 1000.0
        )
        evaluations.append(
            {
                "candidate_id": f"candidate-{index}",
                "weights": weights.as_dict(),
                "objective": round(objective, 4),
                "metrics": metrics,
            }
        )

    evaluations.sort(key=lambda item: item["objective"])
    best = evaluations[0]
    return {
        "reference_time": reference_time.isoformat(),
        "base_weights": base_weights.as_dict(),
        "best_candidate": best,
        "leaderboard": evaluations,
    }


def generate_weight_candidates(base: ScoringWeights) -> list[ScoringWeights]:
    scales = [0.75, 1.0, 1.25]
    candidates = [base]
    for distance_scale in scales:
        for wait_scale in scales:
            for lateness_scale in scales:
                if distance_scale == wait_scale == lateness_scale == 1.0:
                    continue
                candidates.append(
                    ScoringWeights(
                        distance_weight=round(base.distance_weight * distance_scale, 4),
                        travel_weight=base.travel_weight,
                        wait_weight=round(base.wait_weight * wait_scale, 4),
                        lateness_base_penalty=base.lateness_base_penalty,
                        lateness_priority_multiplier=round(
                            base.lateness_priority_multiplier * lateness_scale,
                            4,
                        ),
                        shift_penalty=base.shift_penalty,
                        incompatibility_penalty=base.incompatibility_penalty,
                        score_scale=base.score_scale,
                    )
                )
    return candidates

