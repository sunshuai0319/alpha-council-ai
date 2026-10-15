"""常驻监听进程：每个启用的虚拟账户一条私有 WS 连接。

**与交易路径完全解耦**（docs/web3-roadmap.md 的原则）：这个进程崩了不影响下单，
重启后靠 REST 对账补漏。所以它单独跑，不塞进 workers/scheduler.py。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.db.models import TradingAccount
from app.db.session import SessionLocal
from app.exchange.credentials import credentials_for_account
from app.exchange.weex import WeexClient, WeexCredentials
from app.streams.client import PrivateStreamClient
from app.streams.protocol import SUBSCRIBE_CHANNELS, StreamEvent
from app.streams.reconciler import OrderHistoryReconciler
from app.streams.repository import StreamRepository

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class StreamTarget:
    user_id: str
    trading_account_id: str
    credentials: WeexCredentials


def load_targets(db: Session) -> list[StreamTarget]:
    """启用中、有凭证的虚拟账户。

    条件与 ``TradingCycleService.enabled_user_ids`` 保持一致（provider=weex、
    environment=virtual、enabled），但按账户返回而不是按用户 —— 采集需要账户 id
    做落库的归属键。
    """

    accounts = db.scalars(
        select(TradingAccount).where(
            TradingAccount.enabled.is_(True),
            TradingAccount.provider == "weex",
            TradingAccount.environment == "virtual",
        )
    ).all()
    targets: list[StreamTarget] = []
    for account in accounts:
        credentials = credentials_for_account(account)
        if credentials is None:
            logger.warning("stream target skipped: account %s has no credentials", account.id)
            continue
        targets.append(
            StreamTarget(
                user_id=account.user_id,
                trading_account_id=account.id,
                credentials=credentials,
            )
        )
    return targets


def build_client(
    *,
    url: str,
    api_key: str,
    api_secret: str,
    passphrase: str,
    initial_backoff: float,
    max_backoff: float,
) -> PrivateStreamClient:
    return PrivateStreamClient(
        url=url,
        api_key=api_key,
        api_secret=api_secret,
        passphrase=passphrase,
        channels=SUBSCRIBE_CHANNELS,
        initial_backoff=initial_backoff,
        max_backoff=max_backoff,
    )


class StreamListener:
    def __init__(
        self,
        *,
        settings: Settings,
        session_factory: Callable[[], Session] = SessionLocal,
    ) -> None:
        self.settings = settings
        self.session_factory = session_factory

    async def run_forever(self) -> None:
        """扫描账户 → 每个账户一条连接。

        没有可用账户时**不退出** —— 退出会被 compose 的 restart 策略拉起来，
        变成崩溃循环，日志里只剩重启噪音。改为定期重扫。
        """

        while True:
            with self.session_factory() as db:
                targets = load_targets(db)
            if not targets:
                logger.warning(
                    "no enabled virtual accounts with credentials; re-checking in %.0fs",
                    self.settings.streams_rescan_seconds,
                )
                await asyncio.sleep(self.settings.streams_rescan_seconds)
                continue
            logger.info("starting %d private stream(s)", len(targets))
            await asyncio.gather(*(self._run_target(target) for target in targets))
            await asyncio.sleep(self.settings.streams_rescan_seconds)

    async def _run_target(self, target: StreamTarget) -> None:
        """一个账户：一条 WS 长连接 + 一个周期性 REST 对账。

        每个账户独占一个 Session 跑 WS 落库 —— gather 并发时共享会话会互相踩
        事务，worker 那边靠串行规避同一个问题，这里不能照搬。
        """

        client = build_client(
            url=self.settings.streams_ws_url,
            api_key=target.credentials.api_key,
            api_secret=target.credentials.api_secret,
            passphrase=target.credentials.passphrase,
            initial_backoff=self.settings.streams_reconnect_initial_seconds,
            max_backoff=self.settings.streams_reconnect_max_seconds,
        )
        exchange = WeexClient(self.settings, credentials=target.credentials)
        with self.session_factory() as db:
            repository = StreamRepository(
                db, user_id=target.user_id, trading_account_id=target.trading_account_id
            )
            await asyncio.gather(
                client.run_forever(lambda events: self._persist(repository, events)),
                self._reconcile_forever(target, exchange),
            )

    async def _reconcile_forever(self, target: StreamTarget, exchange: WeexClient) -> None:
        while True:
            await asyncio.to_thread(self._reconcile_once, target, exchange)
            await asyncio.sleep(self.settings.streams_reconcile_seconds)

    def _reconcile_once(self, target: StreamTarget, exchange: WeexClient) -> None:
        """跑一轮对账。

        **另开一个 Session**：这个方法由 ``asyncio.to_thread`` 放到工作线程执行，
        而 SQLAlchemy 的 Session 不是线程安全的，不能与 WS 落库那个会话共用。

        走线程而不是直接在事件循环里跑，是因为它是**阻塞 HTTP** —— 在循环里跑
        会卡住 WS 的 PONG 回执，服务端 10 次收不到就断连。
        """

        with self.session_factory() as db:
            try:
                outcome = OrderHistoryReconciler(
                    db, trading_account_id=target.trading_account_id, exchange=exchange
                ).run_once()
                db.commit()
            except Exception:
                logger.exception("stream reconciliation failed")
                db.rollback()
                return
            logger.info(
                "stream reconcile: inspected=%d symbols_backfilled=%d missed=%d",
                outcome.inspected,
                outcome.symbols_backfilled,
                len(outcome.missed_order_ids),
            )

    @staticmethod
    def _persist(repository: StreamRepository, events: list[StreamEvent]) -> None:
        """落一批事件。

        失败必须 rollback —— 会话一旦处于待回滚状态，后续每一次 flush 都会抛
        PendingRollbackError，整条连接就废了（worker 那边踩过同一个坑）。
        """

        try:
            written = repository.record_all(events)
            repository.commit()
        except Exception:
            logger.exception("failed to persist %d stream event(s)", len(events))
            repository.db.rollback()
            return
        logger.debug("persisted %d/%d stream event(s)", written, len(events))
