from dataclasses import dataclass, field
from datetime import UTC, datetime


@dataclass
class CollectorResult[T]:
    source: str
    items: list[T] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    collected_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    @property
    def ok(self) -> bool:
        return not self.errors

    @property
    def partial(self) -> bool:
        return bool(self.items) and bool(self.errors)
