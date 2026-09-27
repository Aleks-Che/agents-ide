from agents_ide.adapters import activity


def test_tool_activity_is_throttled_and_contains_no_content(monkeypatch):
    events = []
    clock = [0.0]
    monkeypatch.setattr(activity.time, "monotonic", lambda: clock[0])
    pulse = activity.ActivityPulse(lambda kind, payload: events.append((kind, payload)))
    pulse()
    for value in range(1, 5):
        clock[0] = value
        pulse()
    assert len(events) == 1
    clock[0] = 5
    pulse()
    assert events == [("attempt.progress", {"activity": True})] * 2


def test_repeated_busy_and_idle_status_are_not_progress():
    assert not activity.is_progress("attempt.progress", {"native_type": "session.status"})
    assert not activity.is_progress("attempt.progress", {"native_type": "session.idle"})
    assert activity.is_progress("attempt.progress", {"activity": True})
    assert activity.is_progress("agent.input_closed", {"question_id": "q1"})
