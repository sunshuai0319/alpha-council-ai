from enum import StrEnum


class Action(StrEnum):
    HOLD = "HOLD"
    LONG = "LONG"
    SHORT = "SHORT"
    CLOSE = "CLOSE"


class RiskStatus(StrEnum):
    ALLOWED = "ALLOWED"
    REJECTED = "REJECTED"
    PAUSED = "PAUSED"


class OrderStatus(StrEnum):
    PENDING = "PENDING"
    OPEN = "OPEN"
    FILLED = "FILLED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    CANCELED = "CANCELED"
    REJECTED = "REJECTED"
    UNKNOWN = "UNKNOWN"
