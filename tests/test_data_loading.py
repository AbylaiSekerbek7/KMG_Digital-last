from pathlib import Path

from uto_routing.data_loading import load_directory_dataset


def test_generated_csv_dataset_loads() -> None:
    dataset_dir = Path(__file__).resolve().parents[1] / "sample_dataset_csv"

    dataset = load_directory_dataset(dataset_dir)

    assert len(dataset.nodes) == 36
    assert len(dataset.edges) == 120
    assert len(dataset.wells) == 11
    assert len(dataset.vehicles) == 6
    assert len(dataset.tasks) == 9
    assert dataset.metadata["dataset_mode"] == "directory"
    night_tasks = [task for task in dataset.tasks if task.shift.value == "night"]
    assert night_tasks
    assert all(task.start_day.isoformat() == "2026-03-17" for task in night_tasks)

