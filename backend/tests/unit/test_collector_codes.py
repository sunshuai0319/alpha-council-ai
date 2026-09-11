"""采集失败要归一成稳定码，而不是把英文异常带进决策记录。

决策记录里的理由会直接渲染到界面。存原始异常（`_ssl.c:1011: The handshake
operation timed out`）等于把英文堆栈丢给用户看 —— 而 `collector_errors`
表和日志已经存了原文，界面不需要再背一份。
"""

from app.collectors.codes import MARKET_DATA_UNAVAILABLE, market_data_error_code


def test_candle_error_becomes_symbol_and_timeframe_scope() -> None:
    raw = (
        "ETH-USDT/12h: WEEX request failed: GET /capi/v3/market/klines: "
        "_ssl.c:1011: The handshake operation timed out"
    )
    assert market_data_error_code(raw) == "market_data_unavailable:ETH-USDT/12h"


def test_snapshot_error_keeps_only_the_symbol() -> None:
    raw = "BTC-USDT: WEEX request failed: GET /capi/v3/market/ticker/24hr: 503 Service Unavailable"
    assert market_data_error_code(raw) == "market_data_unavailable:BTC-USDT"


def test_the_upstream_reason_never_survives_into_the_code() -> None:
    """换一个上游故障原因，码必须一模一样 —— 否则界面又会冒出新的英文。"""

    timeout = market_data_error_code("SOL-USDT/1d: WEEX request failed: timed out")
    server = market_data_error_code("SOL-USDT/1d: WEEX request failed: 503 Service Unavailable")
    assert timeout == server == "market_data_unavailable:SOL-USDT/1d"


def test_unparseable_error_degrades_to_the_bare_code() -> None:
    """拿不到范围时退回无参数的码，绝不把原文透传出去。"""

    assert market_data_error_code("something exploded") == MARKET_DATA_UNAVAILABLE
