import csv
import io
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

import feedparser  # type: ignore[import-untyped]

from app.collectors.base import CollectorResult
from app.collectors.fetching import ConditionalFetcher
from app.collectors.rss import _clean_text, _published_at, canonicalize_url


@dataclass(frozen=True)
class MacroObservation:
    series_id: str
    observation_date: date
    value: float | None
    source_url: str
    fetched_at: datetime


@dataclass(frozen=True)
class MacroEvent:
    event_id: str
    event_type: str
    title: str
    summary: str
    source_url: str
    published_at: datetime | None
    fetched_at: datetime


DEFAULT_FRED_SERIES = (
    "FEDFUNDS",  # effective federal funds rate
    "CPIAUCSL",  # CPI, all urban consumers
    "UNRATE",  # unemployment rate
    "DFF",  # daily effective federal funds rate, a free dollar-liquidity proxy
    "DGS10",  # 10-year Treasury constant maturity rate
)

#: 各序列的更新频率，用于决定重抓间隔。月度序列按分钟级频率重抓毫无收益，
#: 还可能是 FRED 返回 503 的来源。
FRED_SERIES_CADENCE = {
    "FEDFUNDS": "monthly",
    "CPIAUCSL": "monthly",
    "UNRATE": "monthly",
    "DFF": "daily",
    "DGS10": "daily",
}
FRED_GRAPH_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv"
FED_PRESS_RSS_URL = "https://www.federalreserve.gov/feeds/press_all.xml"

#: FRED 的 CSV 表头形如 `observation_date,CPIAUCSL` —— 值列名就是序列 id，
#: 不是固定的 "value"。早期实现按 "value" 取值，导致所有观测都解析成 None。
FRED_DATE_COLUMNS = ("observation_date", "DATE")
FRED_VALUE_COLUMNS = ("value", "VALUE")


def _fred_value_column(fieldnames: Sequence[str] | None, series_id: str) -> str | None:
    """挑出承载数值的那一列，优先序列 id，其次兼容旧式 value/VALUE 表头。"""

    named = [name for name in (fieldnames or []) if name not in FRED_DATE_COLUMNS]
    if not named:
        return None
    for candidate in (series_id, *FRED_VALUE_COLUMNS):
        if candidate in named:
            return candidate
    return named[0]


class MacroCollector:
    """Collect free FRED series and Federal Reserve official RSS events."""

    def __init__(
        self,
        fetcher: Callable[[str], bytes | str] | None = None,
        parser: Callable[[Any], Any] | None = None,
    ) -> None:
        self._fetcher = fetcher or ConditionalFetcher()
        self._parser = parser or feedparser.parse

    def collect_fred(
        self,
        series_ids: tuple[str, ...] = DEFAULT_FRED_SERIES,
        limit: int = 30,
    ) -> CollectorResult[MacroObservation]:
        if limit < 1:
            raise ValueError("FRED limit must be positive")
        result: CollectorResult[MacroObservation] = CollectorResult(source="fred")
        for series_id in series_ids:
            url = f"{FRED_GRAPH_URL}?id={series_id}"
            try:
                payload = self._fetcher(url)
                text = payload.decode() if isinstance(payload, bytes) else payload
                reader = csv.DictReader(io.StringIO(text))
                value_column = _fred_value_column(reader.fieldnames, series_id)
                rows = list(reader)
                for row in rows[-limit:]:
                    raw_date = (row.get("observation_date") or row.get("DATE") or "").strip()
                    raw_value = ((row.get(value_column) if value_column else None) or "").strip()
                    result.items.append(
                        MacroObservation(
                            series_id=series_id,
                            observation_date=date.fromisoformat(raw_date),
                            value=None if raw_value in {"", "."} else float(raw_value),
                            source_url=url,
                            fetched_at=result.collected_at,
                        )
                    )
                result.succeeded.append(series_id)
            except Exception as exc:  # noqa: BLE001 - isolate failures per external series
                result.errors.append(f"{series_id}: {exc}")
        return result

    def collect_federal_reserve_events(
        self,
        feed_url: str = FED_PRESS_RSS_URL,
    ) -> CollectorResult[MacroEvent]:
        result: CollectorResult[MacroEvent] = CollectorResult(source="federal-reserve")
        try:
            payload = self._fetcher(feed_url)
            if payload is None:  # 304 Not Modified
                result.succeeded.append("federal-reserve")
                return result
            feed = self._parser(payload)
            for entry in getattr(feed, "entries", []) or []:
                title = _clean_text(str(entry.get("title", "")))
                link = canonicalize_url(str(entry.get("link") or entry.get("id") or ""))
                if not title or not link:
                    continue
                event_id = str(entry.get("id") or link)
                result.items.append(
                    MacroEvent(
                        event_id=event_id,
                        event_type="FED_PRESS",
                        title=title,
                        summary=_clean_text(str(entry.get("summary", ""))),
                        source_url=link,
                        published_at=_published_at(entry),
                        fetched_at=result.collected_at,
                    )
                )
        except Exception as exc:  # noqa: BLE001 - an unavailable feed is non-fatal
            result.errors.append(f"federal-reserve: {exc}")
        return result

    def collect(
        self,
        series_ids: tuple[str, ...] = DEFAULT_FRED_SERIES,
        fred_limit: int = 30,
        fed_feed_url: str = FED_PRESS_RSS_URL,
    ) -> tuple[CollectorResult[MacroObservation], CollectorResult[MacroEvent]]:
        return self.collect_fred(series_ids, fred_limit), self.collect_federal_reserve_events(fed_feed_url)
