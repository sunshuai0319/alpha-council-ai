"""WS 客户端。IO 只有 Transport 那一层，其余全用假对象测。

项目里没有 pytest-asyncio，所以用 asyncio.run 包一个 sync 测试函数。
"""

import asyncio
import json

import pytest

from app.streams.client import PrivateStreamClient
from app.streams.protocol import FillEvent

FILL_ROW = {
    "id": "f-1",
    "coin": "SUSDT",
    "orderId": "o-1",
    "positionSide": "SHORT",
    "orderSide": "BUY",
    "fillSize": "100",
    "fillValue": "7.99",
    "fillFee": "0.006392",
    "realizePnl": "0.01",
    "direction": "TAKER",
    "createdTime": "1792069086664",
    "updatedTime": "1792069086664",
}


class FakeTransport:
    def __init__(self, script):
        self._script = list(script)
        self.sent: list[str] = []
        self.closed = False

    async def send(self, message: str) -> None:
        self.sent.append(message)

    async def recv(self) -> str:
        if not self._script:
            raise ConnectionError("script exhausted")
        item = self._script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    async def close(self) -> None:
        self.closed = True


def _client(transport, **overrides):
    async def connect(*, url, headers):
        connect.headers = headers
        return transport

    connect.headers = {}
    values = {
        "url": "wss://example.test/v3/ws/private",
        "api_key": "key-1",
        "api_secret": "secret-1",
        "passphrase": "pass-1",
        "channels": ("fill",),
        "connect": connect,
        "clock_ms": lambda: 1700000000000,
    }
    values.update(overrides)
    return PrivateStreamClient(**values), connect


def test_run_once_subscribes_answers_ping_and_dispatches_events():
    transport = FakeTransport(
        [
            json.dumps({"event": "connected", "cid": "x"}),
            json.dumps({"result": True, "id": 1}),
            json.dumps({"type": "ping", "time": "1"}),
            json.dumps({"e": "fill", "E": 1, "v": 703, "d": [FILL_ROW]}),
            ConnectionError("server closed"),
        ]
    )
    received: list = []
    client, _connect = _client(transport)

    async def scenario():
        with pytest.raises(ConnectionError):
            await client.run_once(received.extend)

    asyncio.run(scenario())

    sent = [json.loads(item) for item in transport.sent]
    assert sent[0] == {"method": "SUBSCRIBE", "params": ["fill"], "id": 1}
    assert sent[1] == {"method": "PONG", "id": 1}
    assert transport.closed is True
    assert len(received) == 1
    assert isinstance(received[0], FillEvent)


def test_run_once_passes_signed_headers_to_the_transport():
    transport = FakeTransport([ConnectionError("boom")])
    client, connect = _client(transport)

    async def scenario():
        with pytest.raises(ConnectionError):
            await client.run_once(lambda events: None)

    asyncio.run(scenario())

    assert connect.headers["ACCESS-KEY"] == "key-1"
    assert connect.headers["ACCESS-TIMESTAMP"] == "1700000000000"
    assert connect.headers["ACCESS-SIGN"]


def test_run_once_closes_the_transport_even_when_the_loop_fails():
    transport = FakeTransport([ValueError("bad frame")])
    client, _connect = _client(transport)

    async def scenario():
        with pytest.raises(ValueError):
            await client.run_once(lambda events: None)

    asyncio.run(scenario())

    assert transport.closed is True


def test_run_forever_backs_off_exponentially_and_caps():
    """退避翻倍直到上限，且每轮之间必定 sleep —— 不能变成忙循环。"""

    delays: list[float] = []
    attempts = {"count": 0}

    async def connect(*, url, headers):
        attempts["count"] += 1
        raise ConnectionError("unreachable")

    async def sleep(seconds: float) -> None:
        delays.append(seconds)

    client = PrivateStreamClient(
        url="wss://example.test/v3/ws/private",
        api_key="k",
        api_secret="s",
        passphrase="p",
        channels=("fill",),
        connect=connect,
        clock_ms=lambda: 1700000000000,
        sleep=sleep,
        initial_backoff=1.0,
        max_backoff=4.0,
    )

    asyncio.run(client.run_forever(lambda events: None, should_stop=lambda: attempts["count"] >= 4))

    assert delays == [1.0, 2.0, 4.0, 4.0]


class _Clock:
    """可控的单调时钟。"""

    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


def _client_with_lifetime(*, lifetime: float, rounds: int):
    """构造一个 client：每次连接都存活 ``lifetime`` 秒然后被上游掐断。

    返回 (client, delays, clock)。``delays`` 记录每次重连前的等待时长。
    """

    clock = _Clock()
    delays: list[float] = []
    attempts = {"count": 0}

    class Transport:
        async def send(self, message: str) -> None:
            return None

        async def recv(self) -> str:
            clock.advance(lifetime)
            raise ConnectionError("upstream dropped us")

        async def close(self) -> None:
            return None

    async def connect(*, url, headers):
        attempts["count"] += 1
        return Transport()

    async def sleep(seconds: float) -> None:
        delays.append(seconds)

    client = PrivateStreamClient(
        url="wss://example.test/v3/ws/private",
        api_key="k",
        api_secret="s",
        passphrase="p",
        channels=("fill",),
        connect=connect,
        clock_ms=lambda: 1700000000000,
        sleep=sleep,
        monotonic=clock,
        initial_backoff=1.0,
        max_backoff=60.0,
        reset_after_seconds=30.0,
    )
    return client, delays, attempts


def test_backoff_resets_after_a_session_that_stayed_connected():
    """连上并稳定存活过一段之后被掐断 → 退避重置。

    上游会定期掐长连接（实测每 1.5–4 分钟一次）。**不重置的话退避会一路爬到
    60 秒**：``run_once`` 断线永远走异常分支，「正常返回就重置」那行永远到不了。
    实测后果是每轮「连 3 分钟 + 瞎 60 秒」，当天漏采 17% —— 而 WS 不重放，
    漏掉的成交就是永久丢失。
    """

    client, delays, attempts = _client_with_lifetime(lifetime=120.0, rounds=3)

    asyncio.run(client.run_forever(lambda events: None, should_stop=lambda: attempts["count"] >= 3))

    assert delays == [1.0, 1.0, 1.0]


def test_backoff_still_grows_when_connections_keep_dying_immediately():
    """连上就断（真的连不通）时退避照常翻倍 —— 重置只针对「存活过一段」的会话。"""

    client, delays, attempts = _client_with_lifetime(lifetime=2.0, rounds=3)

    asyncio.run(client.run_forever(lambda events: None, should_stop=lambda: attempts["count"] >= 3))

    assert delays == [1.0, 2.0, 4.0]


def test_run_forever_does_nothing_when_already_stopped():
    async def connect(*, url, headers):  # pragma: no cover - 不该被调用
        raise AssertionError("should not connect")

    client = PrivateStreamClient(
        url="wss://example.test/v3/ws/private",
        api_key="k",
        api_secret="s",
        passphrase="p",
        channels=("fill",),
        connect=connect,
        clock_ms=lambda: 1700000000000,
    )

    asyncio.run(client.run_forever(lambda events: None, should_stop=lambda: True))
