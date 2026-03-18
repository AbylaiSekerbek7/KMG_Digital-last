from datetime import datetime

from uto_routing.models import Shift, resolve_start_day
from uto_routing.sample_data import create_sample_dataset


def test_resolve_start_day_for_night_shift_after_midnight() -> None:
    planned_start = datetime(2026, 3, 18, 0, 5, 0)

    start_day = resolve_start_day(planned_start, Shift.NIGHT)

    assert start_day.isoformat() == "2026-03-17"


def test_sample_dataset_uses_operational_day_for_night_tasks() -> None:
    dataset = create_sample_dataset()
    night_tasks = [task for task in dataset.tasks if task.shift is Shift.NIGHT]

    assert night_tasks
    assert all(task.start_day.isoformat() == "2026-03-17" for task in night_tasks)

