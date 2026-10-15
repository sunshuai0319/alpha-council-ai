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
        initial_backoff: float = 1.0,
        max_backoff: float = 60.0,
    ) -> None:
        self.url = url
        self.api_key = api_key
        self.api_secret = api_secret
        self.passphrase = passphrase
        self.channels = channels
        self.connect = connect
        self.clock_ms = clock_ms or (lambda: int(time.time() * 1000))
        self.sleep = sleep
        self.initial_backoff = initial_backoff
        self.max_backoff = max_backoff

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
            try:
                await self.run_once(on_events)
                backoff = self.initial_backoff
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - 任何连接故障都要退避重试，不能终止进程
                logger.warning(
                    "private stream disconnected (%s: %s); retrying in %.1fs",
                    type(exc).__name__,
                    exc,
                    backoff,
                )
                await self.sleep(backoff)
                backoff = min(backoff * 2, self.max_backoff)
                continue
            await self.sleep(backoff)
