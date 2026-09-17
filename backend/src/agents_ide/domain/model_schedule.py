"""Weekly, hour-granularity availability in a pinned IANA time zone."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, StrictBool, field_validator, model_validator


class ModelSchedule(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: StrictBool = True
    timezone: str = Field(min_length=1, max_length=128)
    same_every_day: StrictBool = False
    # Monday first; one row when the same schedule applies every day.
    days: list[list[StrictBool]] = Field(min_length=1, max_length=7)

    @field_validator("timezone")
    @classmethod
    def _timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError("Unknown IANA time zone") from None
        return value

    @model_validator(mode="after")
    def _hours(self) -> ModelSchedule:
        if len(self.days) != (1 if self.same_every_day else 7):
            raise ValueError("Expected one day or seven weekdays according to same_every_day")
        if any(len(day) != 24 for day in self.days):
            raise ValueError("Each day must contain exactly 24 hourly permissions")
        return self


def schedule_unavailability(
    schedule: dict[str, Any] | None, *, now: datetime | None = None
) -> str | None:
    """Check before each external call; malformed pinned data must never allow a paid call."""
    if schedule is None:
        return None
    try:
        value = ModelSchedule.model_validate(schedule)
    except ValueError:
        return "schedule_invalid"
    if not value.enabled:
        return None
    local = (now or datetime.now(UTC)).astimezone(ZoneInfo(value.timezone))
    day = 0 if value.same_every_day else local.weekday()
    return None if value.days[day][local.hour] else "outside_schedule"
