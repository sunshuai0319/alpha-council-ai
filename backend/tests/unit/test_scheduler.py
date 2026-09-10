import pytest

from workers.scheduler import TradingScheduler


class FlakyService:
    """第一轮抛错，之后正常。"""

    db = None

    def __init__(self) -> None:
        self.iterations = 0

    def enabled_user_ids(self) -> list[str]:
        self.iterations += 1
        if self.iterations == 1:
            raise RuntimeError("database is down")
        return []

    def run(self, user_id: str, symbol: str) -> None:
        raise AssertionError("no users are enabled")

    def record_failure(self, user_id: str, symbol: str, error: Exception) -> None:
        raise AssertionError("failures are handled inside run_once")


def _stop_after(sleeps: int, recorded: list[float]):
    def fake_sleep(seconds: float) -> None:
        recorded.append(seconds)
        if len(recorded) >= sleeps:
            raise KeyboardInterrupt

    return fake_sleep


def test_scheduler_survives_a_failed_iteration() -> None:
    """单轮失败不能让调度器退出。

    进程退出后 docker-compose 没有重启策略，worker 会永久静默停止交易。
    """
    service = FlakyService()
    scheduler = TradingScheduler(service)  # type: ignore[arg-type]  # 鸭子类型替身
    sleeps: list[float] = []

    with pytest.raises(KeyboardInterrupt):
        scheduler.run_forever(sleep=_stop_after(2, sleeps))

    assert service.iterations == 2  # 出错的下一轮照常执行
