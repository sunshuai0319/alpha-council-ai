import time
from collections.abc import Callable, Iterable, Mapping


class SourceSchedule:
    """按源记录上次抓取时间，决定本轮该抓哪些源。

    第三方公开接口没必要每轮都重抓：FRED 的月度序列按 5 分钟频率抓是纯浪费
    （约 1440 次/天），也招来过整批 503。未配置间隔的源一律视为到期 —— 默认
    抓取，而不是默默不抓。
    """

    def __init__(
        self,
        intervals: Mapping[str, int],
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._intervals = dict(intervals)
        self._clock = clock
        self._last_fetch: dict[str, float] = {}

    def due(self, key: str) -> bool:
        last = self._last_fetch.get(key)
        if last is None:
            return True
        return self._clock() - last >= self._intervals.get(key, 0)

    def mark(self, key: str) -> None:
        self._last_fetch[key] = self._clock()

    def due_keys(self, keys: Iterable[str]) -> list[str]:
        return [key for key in keys if self.due(key)]
