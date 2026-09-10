import time
from collections.abc import Callable
from logging import getLogger

from app.config import get_settings
from app.db.session import SessionLocal
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
        if self.pipeline is not None:
            try:
                self.pipeline.run_once()
            except Exception:
                logger.exception("document pipeline failed")
        for user_id in self.service.enabled_user_ids():
            for symbol in self.symbols:
                try:
                    self.service.run(user_id, symbol)
                except Exception as exc:
                    # The service's next cycle is still eligible; a failure must not stop other tenants.
                    logger.exception("trading cycle failed", extra={"user_id": user_id, "symbol": symbol})
                    self.service.record_failure(user_id, symbol, exc)

    def run_forever(self, sleep: Callable[[float], None] = time.sleep) -> None:
        interval = get_settings().decision_interval_seconds
        while True:
            self.run_once()
            sleep(interval)


def main() -> None:
    settings = get_settings()
    with SessionLocal() as db:
        service = TradingCycleService(db=db, settings=settings)
        TradingScheduler(service, pipeline=DocumentPipeline(db=db)).run_forever()


if __name__ == "__main__":
    main()
