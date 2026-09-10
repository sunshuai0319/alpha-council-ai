import time
from collections.abc import Callable
from logging import getLogger

from app.config import get_settings
from app.db.session import SessionLocal
from app.logging import configure_logging
from app.services.cycle import TradingCycleService
from app.workers.pipeline import DocumentPipeline

logger = getLogger(__name__)


class TradingScheduler:
    """Lightweight five-minute scheduler; PostgreSQL remains the source of enabled accounts."""

    def __init__(
        self,
        service: TradingCycleService,
        symbols: tuple[str, ...] = ("BTC-USDT", "ETH-USDT"),
        pipeline: DocumentPipeline | None = None,
    ) -> None:
        self.service = service
        self.symbols = symbols
        self.pipeline = pipeline

    def run_once(self) -> None:
        try:
            self._run_cycles()
        finally:
            self._end_transaction()

    def _end_transaction(self) -> None:
        """收掉本轮留下的隐式事务。

        worker 整个进程共用一个 Session，空闲时 `enabled_user_ids()` 那个只读
        事务会一直挂着：它阻塞一切 DDL（实测 ALTER TABLE 被卡住 4 分钟以上），
        并钉住事务快照让 autovacuum 回收不了死元组。

        所有写入路径都显式 commit，所以这里 rollback 只是结束空事务，不会丢数据。
        """

        db = self.service.db
        if db is not None:
            db.rollback()

    def _run_cycles(self) -> None:
        if self.pipeline is not None:
            try:
                self.pipeline.run_once()
            except Exception:
                logger.exception("document pipeline failed")
                # 失败的 flush/commit 会把共享会话置于待回滚状态，必须先回滚才能复用。
                if self.service.db is not None:
                    self.service.db.rollback()
        user_ids = self.service.enabled_user_ids()
        succeeded = 0
        for user_id in user_ids:
            for symbol in self.symbols:
                try:
                    self.service.run(user_id, symbol)
                    succeeded += 1
                except Exception as exc:
                    # The service's next cycle is still eligible; a failure must not stop other tenants.
                    logger.exception("trading cycle failed", extra={"user_id": user_id, "symbol": symbol})
                    if self.service.db is not None:
                        self.service.db.rollback()
                    self.service.record_failure(user_id, symbol, exc)
        total = len(user_ids) * len(self.symbols)
        if total:
            logger.info("trading cycles: runs=%d succeeded=%d failed=%d", total, succeeded, total - succeeded)
        else:
            logger.info("trading cycles: no enabled accounts (idle)")

    def run_forever(self, sleep: Callable[[float], None] = time.sleep) -> None:
        interval = get_settings().decision_interval_seconds
        logger.info("trading scheduler started: interval=%ds symbols=%s", interval, self.symbols)
        while True:
            try:
                self.run_once()
            except Exception:
                # 单轮失败（数据库短暂不可用、取账户列表出错等）不能让调度器退出：
                # 进程退出后没有自动重启，交易会永久静默停止。
                logger.exception("scheduler iteration failed")
                db = self.service.db
                if db is not None:
                    db.rollback()
            sleep(interval)


def main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    with SessionLocal() as db:
        service = TradingCycleService(db=db, settings=settings)
        TradingScheduler(service, pipeline=DocumentPipeline(db=db)).run_forever()


if __name__ == "__main__":
    main()
