from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from app.processing.documents import normalize_asset, normalize_event_type, normalize_impact_horizon


@dataclass(frozen=True)
class RetrievalRequest:
    """RAG 检索的唯一输入契约。

    图层负责表达本轮交易要找什么证据，底层 Retriever 负责召回、过滤和重排。
    将这些字段放在一个对象里，避免生产图、测试图和其他调用方各自拼接一套
    query、limit 或时间过滤参数。
    """

    query: str
    asset: str | None = None
    direction: str | None = None
    event_type: str | None = None
    impact_horizon: str | None = None
    published_after: datetime | None = None
    limit: int = 3
    candidate_limit: int | None = None

    def __post_init__(self) -> None:
        if not self.query.strip():
            raise ValueError("retrieval query must not be empty")
        if self.limit < 1:
            raise ValueError("retrieval limit must be positive")
        if self.candidate_limit is not None and self.candidate_limit < 1:
            raise ValueError("retrieval candidate limit must be positive")
        if self.asset:
            object.__setattr__(self, "asset", normalize_asset(self.asset))
        if self.direction:
            normalized_direction = self.direction.strip().upper()
            if normalized_direction not in {"LONG", "SHORT"}:
                raise ValueError(f"unsupported trade direction: {self.direction}")
            object.__setattr__(self, "direction", normalized_direction)
        if self.event_type:
            object.__setattr__(self, "event_type", normalize_event_type(self.event_type))
        if self.impact_horizon:
            object.__setattr__(self, "impact_horizon", normalize_impact_horizon(self.impact_horizon))
        if self.published_after is not None:
            published_after = self.published_after
            if published_after.tzinfo is None:
                published_after = published_after.replace(tzinfo=UTC)
            else:
                published_after = published_after.astimezone(UTC)
            object.__setattr__(self, "published_after", published_after)


def build_veto_retrieval_request(
    *,
    asset: str,
    direction: str,
    now_ms: int,
    lookback_hours: int,
    limit: int,
    candidate_limit: int | None = None,
    impact_horizon: str | None = None,
) -> RetrievalRequest:
    """为新闻 veto 构造统一的、只寻找反向风险的近期证据请求。"""

    normalized_asset = normalize_asset(asset)
    if not normalized_asset:
        raise ValueError("retrieval asset must not be empty")
    normalized_direction = direction.strip().upper()
    if normalized_direction not in {"LONG", "SHORT"}:
        raise ValueError(f"unsupported trade direction: {direction}")
    if lookback_hours < 0:
        raise ValueError("retrieval lookback hours must not be negative")

    adverse_terms = (
        "bearish downside risk negative catalyst"
        if normalized_direction == "LONG"
        else "bullish upside risk positive catalyst"
    )
    query = f"{normalized_asset} {adverse_terms} recent crypto news macro event"
    now = datetime.fromtimestamp(now_ms / 1000, tz=UTC)
    return RetrievalRequest(
        query=query,
        asset=normalized_asset,
        direction=normalized_direction,
        published_after=now - timedelta(hours=lookback_hours),
        impact_horizon=impact_horizon,
        limit=limit,
        candidate_limit=candidate_limit,
    )
