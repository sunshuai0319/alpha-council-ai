"""只读探测 WEEX 合约私有 WebSocket 频道。

要回答的问题：**虚拟盘是否支持私有 WS？**

支持的话，spec 里那些 ``derived-*`` 标注可以整片去掉 —— 缺的字段全在推送里：

- ``fill`` 频道的 ``realizePnl`` / ``fillFee`` / ``direction``
  → 单笔已实现盈亏是交易所回报值，不必从 Δbalance 反推
- ``orders`` 频道的 ``triggerPrice`` / ``status: UNTRIGGERED`` / ``positionTpsl``
  → 条件单触发平仓不再不可见，``exit_reason`` 可判定
- ``orders`` 频道的 ``orderSource``（WEB / API）
  → 手动单与系统单不必再靠 clientOrderId 前缀猜
- ``account`` 频道的 ``cumDepositAmount`` / ``cumWithdrawAmount``
  → 能把充值/划转从净值变化里剔出去

**只读**：只 SUBSCRIBE，不下单、不平仓、不撤单。订阅本身不改动账户状态。

用法 —— 连上后去 WEEX 网页手动开一笔仓、再平掉，看推送是否实时到达：

    uv run python scripts/probe_private_ws.py --duration 180
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import hmac
import json
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# stdout 重定向到管道/文件时 Python 会切成块缓冲，实时推送会攒到进程结束才出现
# （实测 `| tee` 时表现为"卡住没输出"）。这是长时监听脚本，必须逐行吐出。
sys.stdout.reconfigure(line_buffering=True)

import websockets

from app.config import Settings, get_settings
from app.db.session import SessionLocal
from app.services.cycle import TradingCycleService

WS_PRIVATE_URL = "wss://ws-contract.weex.com/v3/ws/private"

#: WS 签名用的 requestPath。与 REST 的签名串格式不同（不含 method / query / body），
#: 所以不能复用 WeexClient.signature —— 拿 REST 的串去连 WS 会鉴权失败。
WS_PRIVATE_PATH = "/v3/ws/private"

DEFAULT_CHANNELS = ("fill", "orders", "positions", "account")

#: 服务端断开前允许的连续未响应 ping 次数（文档：超过 10 次不回应会断开）。
MAX_MISSED_PINGS = 10


def ws_signature(secret: str, timestamp: str) -> str:
    """``base64(hmac_sha256(secret, timestamp + requestPath))``，见 WS 文档。"""

    message = f"{timestamp}{WS_PRIVATE_PATH}"
    digest = hmac.new(secret.encode(), message.encode(), hashlib.sha256).digest()
    return base64.b64encode(digest).decode()


def _ms(value: Any) -> str:
    try:
        milliseconds = int(value)
    except (TypeError, ValueError):
        return "-"
    if milliseconds <= 0:
        return "-"
    return datetime.fromtimestamp(milliseconds / 1000, tz=UTC).isoformat()


def _short(value: Any, limit: int = 90) -> str:
    text = str(value)
    return text if len(text) <= limit else f"{text[:limit]}…"


def summarize_rows(event: str, rows: list[Any]) -> list[str]:
    """把推送里我们最关心的字段挑出来 —— 探测的目的就是确认它们真的存在。"""

    lines: list[str] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        if event == "fill":
            lines.append(
                f"{row.get('symbol')} {row.get('positionSide')}/{row.get('orderSide')} "
                f"size={row.get('fillSize')} value={row.get('fillValue')} "
                f"fee={row.get('fillFee')} realizePnl={row.get('realizePnl')} "
                f"dir={row.get('direction')} at={_ms(row.get('createdTime'))}"
            )
        elif event == "orders":
            lines.append(
                f"{row.get('symbol')} {row.get('type')} {row.get('status')} "
                f"side={row.get('orderSide')} source={row.get('orderSource')} "
                f"reduceOnly={row.get('reduceOnly')} triggerPrice={row.get('triggerPrice')} "
                f"positionTpsl={row.get('positionTpsl')} "
                f"takerFee={row.get('takerFeeRate')} cumPnl={row.get('cumRealizePnl')} "
                f"cumFee={row.get('cumFillFee')} cid={row.get('clientOrderId')}"
            )
        elif event == "positions":
            lines.append(
                f"{row.get('symbol')} {row.get('side')} size={row.get('size')} "
                f"openValue={row.get('openValue')} cumOpenFee={row.get('cumOpenFee')} "
                f"cumCloseFee={row.get('cumCloseFee')} cumFundingFee={row.get('cumFundingFee')} "
                f"cumLiquidateFee={row.get('cumLiquidateFee')}"
            )
        elif event == "account":
            lines.append(
                f"{row.get('coin')} amount={row.get('amount')} "
                f"cumDeposit={row.get('cumDepositAmount')} cumWithdraw={row.get('cumWithdrawAmount')} "
                f"cumFillFee={row.get('cumPositionFillFeeAmount')} "
                f"cumFunding={row.get('cumPositionFundingAmount')} "
                f"liquidating={row.get('liquidating')}"
            )
        else:
            lines.append(_short(row))
    return lines


def print_push(payload: dict[str, Any], *, show_raw: bool) -> None:
    event = payload.get("e")
    rows = payload.get("d")
    rows = rows if isinstance(rows, list) else []
    header = f"[{event}] {_ms(payload.get('E'))}  v={payload.get('v')}  msgEvent={payload.get('msgEvent')}"
    print(header)
    for line in summarize_rows(str(event), rows):
        print(f"    {line}")
    if show_raw:
        print(f"    raw: {json.dumps(payload, ensure_ascii=False)}")


async def listen(
    exchange: Any,
    *,
    channels: tuple[str, ...],
    duration: float,
    show_raw: bool,
) -> int:
    credentials = exchange.credentials
    if credentials is None:
        print("账户没有配置 API 凭证，无法连私有频道。", file=sys.stderr)
        return 2

    timestamp = str(int(time.time() * 1000))
    headers = {
        "ACCESS-KEY": credentials.api_key,
        "ACCESS-PASSPHRASE": credentials.passphrase,
        "ACCESS-TIMESTAMP": timestamp,
        "ACCESS-SIGN": ws_signature(credentials.api_secret, timestamp),
    }

    print(f"连接 {WS_PRIVATE_URL}")
    print(f"订阅频道：{list(channels)}")
    try:
        async with websockets.connect(
            WS_PRIVATE_URL,
            additional_headers=headers,
            user_agent_header="alpha-council-probe/1.0",
            # WEEX 用自己的应用层 ping（JSON 消息），关掉协议层自动 ping 免得混淆。
            ping_interval=None,
            open_timeout=20,
        ) as socket:
            print("✅ 握手成功：虚拟盘凭证被私有频道接受")
            for index, channel in enumerate(channels, start=1):
                await socket.send(json.dumps({"method": "SUBSCRIBE", "params": [channel], "id": index}))

            print(f"\n现在去 WEEX 网页手动开一笔仓、再平掉。监听 {duration:.0f} 秒…\n")
            deadline = time.monotonic() + duration
            pushes = 0
            missed_pings = 0

            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                try:
                    raw = await asyncio.wait_for(socket.recv(), timeout=remaining)
                except TimeoutError:
                    break
                except websockets.ConnectionClosed as exc:
                    print(f"\n❌ 连接被服务端关闭：{exc}")
                    return 3

                try:
                    payload = json.loads(raw)
                except ValueError:
                    print(f"[非 JSON] {_short(raw)}")
                    continue
                if not isinstance(payload, dict):
                    print(f"[非对象] {_short(raw)}")
                    continue

                # 私有频道的 ping 用 type 字段（公有频道用 event），回同一个 PONG 即可。
                if payload.get("type") == "ping" or payload.get("event") == "ping":
                    await socket.send(json.dumps({"method": "PONG", "id": 1}))
                    missed_pings = 0
                    continue

                # 握手后服务端先发一条 connected 控制消息，不是异常。
                if payload.get("event") == "connected":
                    print(f"[已连接] cid={payload.get('cid')} time={_ms(payload.get('time'))}")
                    continue

                # 订阅回执：{"result": true, "id": N} 或失败时带 msg。
                if "result" in payload:
                    if payload.get("result"):
                        print(f"[订阅成功] id={payload.get('id')}")
                    else:
                        print(f"[订阅失败] id={payload.get('id')} msg={payload.get('msg')}")
                    continue

                if payload.get("e"):
                    pushes += 1
                    print_push(payload, show_raw=show_raw)
                else:
                    print(f"[其他] {_short(raw)}")
                    missed_pings += 1
                    if missed_pings >= MAX_MISSED_PINGS:
                        print("连续收到无法识别的消息，判定连接异常。")
                        return 3

            print(f"\n监听结束。收到业务推送 {pushes} 条。")
            if pushes == 0:
                print("⚠️  一条业务推送都没有。可能原因：")
                print("   1. 期间账户没有任何成交/持仓/资金变动（那就再去开平一笔仓重试）")
                print("   2. 虚拟盘不推送私有事件（端点存在但账户类型不支持）")
            return 0
    except Exception as exc:  # noqa: BLE001 - 探测脚本要把失败原因原样打出来
        print(f"\n❌ 连接失败：{type(exc).__name__}: {_short(exc, 200)}")
        print("   → 若为握手 401/403，说明虚拟盘凭证不被正式合约 WS 接受。")
        return 3


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="只读探测 WEEX 合约私有 WebSocket 频道")
    parser.add_argument("--user-id", help="交易账户所属用户；只有一个启用账户时可省略")
    parser.add_argument(
        "--channels",
        default=",".join(DEFAULT_CHANNELS),
        help=f"逗号分隔的频道，默认 {','.join(DEFAULT_CHANNELS)}",
    )
    parser.add_argument("--duration", type=float, default=180.0, help="监听秒数，默认 180")
    parser.add_argument("--raw", action="store_true", help="同时打印完整推送 JSON")
    return parser


def resolve_user(service: TradingCycleService, explicit: str | None) -> str | None:
    if explicit:
        return explicit
    candidates = service.enabled_user_ids()
    if len(candidates) == 1:
        return candidates[0]
    print(f"需要 --user-id。当前启用的虚拟账户有 {len(candidates)} 个：", file=sys.stderr)
    for candidate in candidates:
        print(f"  {candidate}", file=sys.stderr)
    return None


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    settings: Settings = get_settings()
    if not settings.weex_virtual_only:
        print("拒绝运行：WEEX_VIRTUAL_ONLY 不是 true。这个脚本只允许在虚拟盘上跑。", file=sys.stderr)
        return 2

    channels = tuple(part.strip() for part in args.channels.split(",") if part.strip())
    if not channels:
        print("--channels 不能为空", file=sys.stderr)
        return 2

    with SessionLocal() as db:
        service = TradingCycleService(db=db, settings=settings)
        user_id = resolve_user(service, args.user_id)
        if user_id is None:
            return 2
        print(f"用户：{user_id}   虚拟盘：{settings.weex_virtual_only}")
        exchange = service._exchange_for_user(user_id)  # 私有方法：从 trading_accounts 读凭据，避免脚本重复处理
        return asyncio.run(
            listen(exchange, channels=channels, duration=args.duration, show_raw=args.raw)
        )


if __name__ == "__main__":
    raise SystemExit(main())
