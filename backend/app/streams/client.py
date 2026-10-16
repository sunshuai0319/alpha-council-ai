"""WEEX 私有 WebSocket 客户端。

设计上把 IO 压到最小：所有协议逻辑在 protocol.py（纯函数），这里只负责
「连上 → 订阅 → 转发 → 断了退避重连」。socket 通过 ``connect`` 注入，
于是测试可以塞一个假 Transport，不需要起服务器。
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any, Protocol

import websockets

from app.streams.protocol import (
    ACK,
    CONNECTED,
    EVENT,
    PING,
    SUBSCRIBE_CHANNELS,
    StreamEvent,
    classify,
    parse_event,
    pong_message,
    subscribe_message,
    ws_auth_headers,
)

logger = logging.getLogger(__name__)

USER_AGENT = "alpha-council-streams/1.0"

#: 建连超时。超过就当成一次失败连接，走退避重试。
OPEN_TIMEOUT_SECONDS = 20.0


class Transport(Protocol):
    """socket 的最小接口。假对象实现这三个方法即可替掉网络。"""

    async def send(self, message: str) -> None: ...

    async def recv(self) -> str: ...

    async def close(self) -> None: ...


class _WebsocketTransport:
    def __init__(self, socket: Any) -> None:
        self._socket = socket

    async def send(self, message: str) -> None:
        await self._socket.send(message)

    async def recv(self) -> str:
        return await self._socket.recv()

    async def close(self) -> None:
        await self._socket.close()


async def default_connect(*, url: str, headers: dict[str, str]) -> Transport:
    socket = await websockets.connect(
        url,
        additional_headers=headers,
        user_agent_header=USER_AGENT,
        # WEEX 用自己的应用层 JSON ping。关掉协议层自动 ping，免得两套心跳互相干扰。
        ping_interval=None,
        open_timeout=OPEN_TIMEOUT_SECONDS,
    )
    return _WebsocketTransport(socket)


class PrivateStreamClient:
    def __init__(
        self,
        *,
        url: str,
        api_key: str,
        api_secret: str,
        passphrase: str,
        channels: tuple[str, ...] = SUBSCRIBE_CHANNELS,
        connect: Callable[..., Awaitable[Transport]] = default_connect,
        clock_ms: Callable[[], int] | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        initial_backoff: float = 1.0,
        max_backoff: float = 60.0,
        reset_after_seconds: float = 30.0,
    ) -> None:
        self.url = url
        self.api_key = api_key
        self.api_secret = api_secret
        self.passphrase = passphrase
        self.channels = channels
        self.connect = connect
        self.clock_ms = clock_ms or (lambda: int(time.time() * 1000))
        self.sleep = sleep
        #: 单调时钟，用来判断「这次连接活了多久」。不能用 clock_ms —— 墙上时钟会跳。
        self.monotonic = monotonic
        self.initial_backoff = initial_backoff
        self.max_backoff = max_backoff
        #: 连接存活超过这么久才断，就认为它是「被上游掐断」而不是「连不上」，
        #: 退避重置。见 run_forever 的说明。
        self.reset_after_seconds = reset_after_seconds
        #: 进程级计数只用于可观测性，不参与退避决策。
        self._disconnect_count = 0

    async def run_once(self, on_events: Callable[[list[StreamEvent]], None]) -> None:
        """连一次、订阅、转发，直到连接断开。

        断开通过异常向上抛（``ConnectionError`` / ``websockets.ConnectionClosed``），
        由 ``run_forever`` 决定怎么退避。
        """

        headers = ws_auth_headers(
            api_key=self.api_key,
            api_secret=self.api_secret,
            passphrase=self.passphrase,
            timestamp_ms=self.clock_ms(),
        )
        transport = await self.connect(url=self.url, headers=headers)
        try:
            for index, channel in enumerate(self.channels, start=1):
                await transport.send(subscribe_message(channel, index))
            logger.info("private stream subscribed: %s", list(self.channels))
            while True:
                incoming = classify(await transport.recv())
                if incoming.kind == PING:
                    await transport.send(pong_message())
                elif incoming.kind == EVENT:
                    events = parse_event(incoming.payload)
                    if events:
                        on_events(events)
                elif incoming.kind == ACK and not incoming.payload.get("result"):
                    logger.warning("private stream subscription rejected: %s", incoming.payload)
                elif incoming.kind == CONNECTED:
                    logger.info("private stream connected: %s", incoming.payload.get("cid"))
        finally:
            await transport.close()

    async def run_forever(
        self,
        on_events: Callable[[list[StreamEvent]], None],
        *,
        should_stop: Callable[[], bool] | None = None,
    ) -> None:
        """断线重连的主循环。指数退避，封顶在 ``max_backoff``。

        每一轮之后**必定** sleep —— 一个立刻返回的 session 不 sleep 会变成忙循环
        把 CPU 打满。

        退避值必须在 sleep **之后**才翻倍，否则日志说的「retrying in Xs」与实际
        等待时长不一致（这个不一致真的踩过：日志报 1.0s，实际等了 2.0s）。
        """

        backoff = self.initial_backoff
        while should_stop is None or not should_stop():
            session_started = self.monotonic()
            try:
                await self.run_once(on_events)
                backoff = self.initial_backoff
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - 任何连接故障都要退避重试，不能终止进程
                session_seconds = self.monotonic() - session_started
                self._disconnect_count += 1
                stable_session = session_seconds >= self.reset_after_seconds
                if stable_session:
                    # 这次连接**稳定存活过一段**才断 —— 那是上游把长连接掐了，
                    # 不是「连不上」。退避要从最短重来。
                    #
                    # 不重置的话：``run_once`` 永远走异常分支（WS 断开就是异常），
                    # 那个写在上面的「正常返回就重置」永远到不了，退避会随进程
                    # 寿命单调爬到上限。实测后果是每轮「连 3 分钟 + 瞎 60 秒」，
                    # 漏采率 17% —— 而 WS 不重放，漏掉的就是永久丢失。
                    backoff = self.initial_backoff
                else:
                    # 长连接被上游定期掐断是已知现象；连上后很快又失败则代表
                    # 鉴权、网络或端点异常，需要单独提高日志级别，便于告警系统抓取。
                    logger.error(
                        "private stream unstable: connection failed after %.0fs "
                        "(disconnect #%d, threshold=%.0fs)",
                        session_seconds,
                        self._disconnect_count,
                        self.reset_after_seconds,
                    )
                logger.warning(
                    "private stream disconnected after %.0fs (%s: %s, stable=%s, "
                    "disconnect #%d); retrying in %.1fs",
                    session_seconds,
                    type(exc).__name__,
                    exc,
                    stable_session,
                    self._disconnect_count,
                    backoff,
                )
                await self.sleep(backoff)
                backoff = min(backoff * 2, self.max_backoff)
                continue
            await self.sleep(backoff)
