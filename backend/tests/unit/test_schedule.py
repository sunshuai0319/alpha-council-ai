from app.workers.schedule import SourceSchedule


def test_first_fetch_is_always_due() -> None:
    schedule = SourceSchedule({"serie": 3600}, clock=lambda: 0.0)
    assert schedule.due("serie") is True


def test_source_is_not_due_until_its_interval_elapses() -> None:
    now = [0.0]
    schedule = SourceSchedule({"monthly": 86400, "daily": 3600}, clock=lambda: now[0])
    schedule.mark("monthly")
    schedule.mark("daily")

    now[0] = 3600
    assert schedule.due("daily") is True
    assert schedule.due("monthly") is False

    now[0] = 86400
    assert schedule.due("monthly") is True


def test_source_without_a_configured_interval_is_always_due() -> None:
    """没配间隔就照常抓，不能因为漏配而静默停止采集。"""
    schedule = SourceSchedule({}, clock=lambda: 1_000_000.0)
    assert schedule.due("unknown") is True


def test_due_keys_filters_and_preserves_order() -> None:
    now = [0.0]
    schedule = SourceSchedule({"a": 3600, "b": 3600}, clock=lambda: now[0])
    schedule.mark("a")

    assert schedule.due_keys(["a", "b"]) == ["b"]
