from dataclasses import dataclass, field
from datetime import UTC, datetime


@dataclass
class CollectorResult[T]:
    source: str
    items: list[T] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    #: 本论抓取成功的源标识（FRED 的序列 id、RSS 的源名）。调度层据此决定
    #: 哪些源可以进入下一个间隔 —— 失败的源必须下一轮立刻重试。
    succeeded: list[str] = field(default_factory=list)
    collected_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    @property
    def ok(self) -> bool:
        return not self.errors

    @property
    def partial(self) -> bool:
        return bool(self.items) and bool(self.errors)
