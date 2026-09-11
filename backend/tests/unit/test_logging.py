import logging

from app.logging import PollingAccessLogFilter


def access_record(method: str, path: str, status: int) -> logging.LogRecord:
    return logging.LogRecord(
        name="uvicorn.access",
        level=logging.INFO,
        pathname="test",
        lineno=1,
        msg='%s - "%s %s HTTP/%s" %d',
        args=("127.0.0.1:12345", method, path, "1.1", status),
        exc_info=None,
    )


def test_successful_dashboard_poll_is_filtered() -> None:
    log_filter = PollingAccessLogFilter()

    assert log_filter.filter(access_record("GET", "/api/market", 200)) is False
    assert log_filter.filter(access_record("OPTIONS", "/api/events", 200)) is False


def test_failed_dashboard_poll_is_kept_for_diagnosis() -> None:
    log_filter = PollingAccessLogFilter()

    assert log_filter.filter(access_record("GET", "/api/market", 503)) is True


def test_non_polling_request_is_kept() -> None:
    log_filter = PollingAccessLogFilter()

    assert log_filter.filter(access_record("POST", "/api/control/pause", 200)) is True
    assert log_filter.filter(access_record("GET", "/api/health", 200)) is True
