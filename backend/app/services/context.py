"""把数据库里已有的宏观事实装配成决策周期可用的上下文。

行情事实不写进 RAG（见 CLAUDE.md），所以宏观数值只能从库里读。周期本身不做采集 ——
采集由 DocumentPipeline 负责，这里只负责「读出来、算成 agent 能用的形状」。
"""

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import MacroObservationRecord, SourceDocument

#: FED 新闻只取最近这么多天的，更早的对短线决策没有意义。
FED_PRESS_WINDOW_DAYS = 7
#: 每个序列最多取两条观测，用来算变化。
SERIES_HISTORY = 2
#: FED 新闻最多带几条进上下文。
FED_PRESS_LIMIT = 5


def _series_context(db: Session) -> list[dict[str, Any]]:
    """每个 series 取最近两个非空观测，给出当前值、前值与变化。

    只查非空行：解析修好之前写进库的 NULL 行不能污染上下文。
    """

    rows = db.scalars(
        select(MacroObservationRecord)
        .where(MacroObservationRecord.value.is_not(None))
        .order_by(
            MacroObservationRecord.series_id,
            MacroObservationRecord.observation_date.desc(),
        )
    ).all()

    buckets: dict[str, list[MacroObservationRecord]] = {}
    for row in rows:
        bucket = buckets.setdefault(row.series_id, [])
        if len(bucket) < SERIES_HISTORY:
            bucket.append(row)

    context: list[dict[str, Any]] = []
    for series_id, bucket in sorted(buckets.items()):
        current = bucket[0]
        # WHERE 已经把 NULL 过滤掉了，这里再判一次是为了让类型收敛（也更稳）。
        if current.value is None:
            continue
        entry: dict[str, Any] = {
            "kind": "macro_series",
            "series_id": series_id,
            "observation_date": current.observation_date.date().isoformat(),
            "value": float(current.value),
        }
        previous = bucket[1] if len(bucket) > 1 else None
        if previous is not None and previous.value is not None:
            entry["previous_value"] = float(previous.value)
            entry["change"] = float(current.value) - float(previous.value)
        context.append(entry)
    return context


def _fed_press_context(db: Session, *, now: datetime) -> list[dict[str, Any]]:
    since = now - timedelta(days=FED_PRESS_WINDOW_DAYS)
    rows = db.scalars(
        select(SourceDocument)
        .where(
            SourceDocument.source == "federal-reserve",
            SourceDocument.published_at.is_not(None),
            SourceDocument.published_at >= since,
        )
        .order_by(SourceDocument.published_at.desc())
        .limit(FED_PRESS_LIMIT)
    ).all()
    context: list[dict[str, Any]] = []
    for row in rows:
        # WHERE 已过滤 NULL，这里再判一次让类型收敛。
        if row.published_at is None:
            continue
        context.append(
            {
                "kind": "fed_press",
                "title": row.title,
                "published_at": row.published_at.isoformat(),
                "source_url": row.canonical_url,
            }
        )
    return context


def load_macro_context(
    db: Session | None,
    *,
    limit: int = 20,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """返回可直接放进 ``TradingCycleState.macro_events`` 的列表。

    库不可用时返回空列表：宏观缺失只应让 agent 说「数据不足」，不该让整个周期失败。
    """

    if db is None:
        return []
    context = [*_series_context(db), *_fed_press_context(db, now=now or datetime.now(UTC))]
    return context[:limit]
