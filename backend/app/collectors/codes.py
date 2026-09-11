"""把采集失败的原始异常归一成稳定码。

**为什么要有这一层**：采集错误会顺着 `state.errors` 一路走到提案理由，而提案理由
是直接渲染到界面上的。原来的写法（`f"{symbol}/{timeframe}: {exc}"`）等于把
`_ssl.c:1011: The handshake operation timed out` 这种英文原文丢给用户看。

与 ``account_unavailable:`` 那类码的区别：这里**连参数也归一**。账户不可用只有一个
原因（对端连不上），而行情抓取失败的原因有几十种，透传原文就会透传英文。真正要
展示给用户的信息是「哪个品种/周期没抓到」，不是上游为什么没抓到 —— 后者留在日志和
``collector_errors`` 表里，供排查用。
"""

import re

MARKET_DATA_UNAVAILABLE = "market_data_unavailable"

#: 采集器写在错误串最前面的范围标识：`BTC-USDT`（快照）或 `ETH-USDT/12h`（K 线）。
#: 只认这个字符集，保证任何情况下都不会把一整句英文当成范围透传出去。
_SCOPE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9/_.-]{0,31}$")


def market_data_error_code(raw: str) -> str:
    """`ETH-USDT/12h: WEEX request failed: <原因>` → `market_data_unavailable:ETH-USDT/12h`。"""

    scope = raw.partition(": ")[0].strip()
    if _SCOPE.match(scope):
        return f"{MARKET_DATA_UNAVAILABLE}:{scope}"
    return MARKET_DATA_UNAVAILABLE
