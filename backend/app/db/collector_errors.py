"""采集错误的归并落库。

只打日志的话，"网络到底稳不稳"无从判断 —— 翻日志数不出错误率，也分不清是单个
站点的波动还是整条出口的故障。归并计数避免每轮写一行撑爆表。

行情采集（交易周期）与文档采集（新闻 / 宏观）共用这一份 —— 两者都会遇到上游
抖动，也都不该各自维护一套归并逻辑。

**不 commit**：调用方决定事务边界。
"""

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import CollectorError


def record_merged_error(db: Session, *, collector: str, message: str) -> None:
    """按 (collector, message) 归并计数一条错误。

    只打日志的话「这个故障到底发生过几次」数不出来；每轮写一行又会撑爆表。
    归并计数是这两者之间的折中。

    ``message`` 必须是**稳定的**串，别把 orderId 这类每次都不同的东西拼进来 ——
    那会让每次发生都产生新行，归并就失效了。
    """

    now = datetime.now(UTC)
    row = db.scalar(
        select(CollectorError).where(
            CollectorError.collector == collector,
            CollectorError.message == message,
        )
    )
    if row is None:
        db.add(
            CollectorError(
                collector=collector,
                message=message,
                occurrences=1,
                first_seen_at=now,
                last_seen_at=now,
            )
        )
    else:
        row.occurrences += 1
        row.last_seen_at = now


def record_collector_errors(db: Session | None, *results: Any) -> None:
    """把每个 ``CollectorResult`` 的 errors 按 (collector, message) 归并计数。

    存的是**原始错误串**（含异常原文）—— 这是排查用的，与展示用的稳定码
    （``app/collectors/codes.py``）分工不同。
    """

    if db is None:
        return
    for result in results:
        for message in result.errors:
            record_merged_error(db, collector=result.source, message=message)
