from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from agents_ide.domain.model_schedule import ModelSchedule, schedule_unavailability


def schedule(*, shared=False, timezone="UTC", enabled=True):
    return {
        "enabled": enabled,
        "timezone": timezone,
        "same_every_day": shared,
        "days": [[False] * 24 for _ in range(1 if shared else 7)],
    }


def test_local_hour_boundaries_and_week_rollover():
    value = schedule(timezone="Asia/Yekaterinburg")
    value["days"][0][0] = True  # Monday, midnight UTC+5.
    for instant, expected in [
        ("2026-09-20T18:59:59+00:00", "outside_schedule"),
        ("2026-09-20T19:00:00+00:00", None),
        ("2026-09-20T19:59:59+00:00", None),
        ("2026-09-20T20:00:00+00:00", "outside_schedule"),
    ]:
        assert schedule_unavailability(value, now=datetime.fromisoformat(instant)) == expected


@pytest.mark.parametrize("weekday", range(7))
def test_shared_hours_apply_to_every_weekday(weekday):
    value = schedule(shared=True)
    value["days"][0][17] = True
    assert (
        schedule_unavailability(value, now=datetime(2026, 9, 14 + weekday, 17, tzinfo=UTC)) is None
    )
    assert schedule_unavailability(value, now=datetime(2026, 9, 14 + weekday, 18, tzinfo=UTC))


def test_daylight_saving_uses_wall_clock_in_both_repeated_hours():
    value = schedule(shared=True, timezone="America/New_York")
    value["days"][0][1] = True
    for hour in (5, 6):
        assert schedule_unavailability(value, now=datetime(2026, 11, 1, hour, tzinfo=UTC)) is None
    assert schedule_unavailability(value, now=datetime(2026, 11, 1, 7, tzinfo=UTC))


def test_missing_and_disabled_schedules_preserve_unrestricted_selection():
    assert schedule_unavailability(None) is None
    assert schedule_unavailability(schedule(enabled=False)) is None


@pytest.mark.parametrize(
    "patch",
    [
        {"timezone": "Not/AZone"},
        {"timezone": "../UTC"},
        {"days": []},
        {"days": [[False] * 24]},
        {"days": [[False] * 23] * 7},
        {"days": [[1] * 24] * 7},
        {"same_every_day": True},
        {"enabled": "false"},
    ],
)
def test_malformed_schedule_rejected_and_pinned_data_fails_closed(patch):
    value = {**schedule(), **patch}
    with pytest.raises(ValidationError):
        ModelSchedule.model_validate(value)
    assert schedule_unavailability(value) == "schedule_invalid"
