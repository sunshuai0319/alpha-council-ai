import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.db.models import Base, User
from app.services.cycle import TradingCycleService
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


def test_scheduler_ends_its_transaction_after_each_iteration(tmp_path) -> None:
    """每轮结束必须收掉事务。

    worker 是整个进程一个 Session，空闲时那条 `enabled_user_ids()` 的读事务
    会一直开着：它阻塞一切 DDL（实测 ALTER TABLE 等 4 分钟以上），还会钉住
    事务快照，让 autovacuum 回收不了死元组。
    """
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'scheduler.db'}")
    Base.metadata.create_all(engine)
    db = Session(engine)
    db.add(User(id="u-1", clerk_user_id="clerk-u1"))
    db.commit()

    TradingScheduler(TradingCycleService(db=db)).run_once()

    assert db.in_transaction() is False
