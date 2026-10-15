"""只读探测 WEEX 虚拟盘 ``order/history`` 的字段形态。

回答 spec（``docs/superpowers/specs/2026-10-15-onchain-trade-attestation-design.md``）
§15.1 的六个问题，其中两个会直接决定整个方案：

- **order raw 里有没有** ``commission`` / ``fee`` / ``realizedPnl``？
  有的话链上 PnL 不必推导，spec §12 的推导模型可以大幅简化。
- **用户手动在 WEEX 网页下的单在不在** ``order/history`` 里？``clientOrderId`` 长什么样？
  不在的话方案前提塌陷，退路见 spec §15.3。

**纯读**：只调 ``GET order/history``，不碰 ``POST order``，不建仓、不平仓、不撤单。

推荐用法 —— 开平仓前后各跑一次，用 diff 看新增订单：

    uv run python scripts/probe_order_history.py --save /tmp/probe-before.json
    # 到 WEEX 网页手动开一笔仓、再平掉
    uv run python scripts/probe_order_history.py --diff /tmp/probe-before.json
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import Settings, get_settings
from app.db.session import SessionLocal

# ``_exchange_symbol`` 是 weex 模块的私有函数。这里需要它把 BTC-USDT 换算成
# 虚拟盘的 BTCSUSDT；复制一份换算逻辑更糟 —— 虚拟盘与正式盘的规则会各自漂移。
from app.exchange.weex import _exchange_symbol, normalize_symbol
from app.services.cycle import TradingCycleService

#: 出现这些字段就说明盈亏不必推导。这是本次探测最想确认的一件事。
PNL_FIELDS = (
    "commission",
    "fee",
    "openFee",
    "closeFee",
    "fundingFee",
    "realizedPnl",
    "realizedProfit",
    "profit",
    "pnl",
)

#: 系统下单的 clientOrderId 前缀（见 app/execution/service.py 的 stable_client_order_id）。
SYSTEM_CLIENT_ID_PREFIX = "alpha-"

#: 一次拉取的最大条数，与 WeexClient._order_history 保持一致。
MAX_LIMIT = 100


def _short(value: Any, limit: int = 44) -> str:
    text = str(value)
    return text if len(text) <= limit else f"{text[:limit]}…"


def _mask_order_id(value: Any) -> str:
    """订单号只留首尾。控制台输出不该留下完整可查的订单标识。"""

    text = str(value)
    return f"{text[:4]}…{text[-4:]}" if len(text) > 10 else text


def _ms_to_iso(value: Any) -> str:
    try:
        milliseconds = int(value)
    except (TypeError, ValueError):
        return "-"
    if milliseconds <= 0:
        return "-"
    return datetime.fromtimestamp(milliseconds / 1000, tz=UTC).isoformat()


def _client_id_shape(client_id: str) -> str:
    if not client_id:
        return "（空）"
    if client_id.startswith(SYSTEM_CLIENT_ID_PREFIX):
        return f"系统：{SYSTEM_CLIENT_ID_PREFIX}<hex>"
    if client_id.isdigit():
        return f"纯数字（{len(client_id)} 位）"
    return f"其他（{len(client_id)} 位，首 4 位 {client_id[:4]}）"


def fetch_orders(exchange: Any, *, symbol: str | None, limit: int, page: int) -> list[dict[str, Any]]:
    """直接调 ``order/history``，不走 ``WeexClient._order_history``。

    后者把 ``page`` 写死为 0，没法做分页探测；而分页行为正是 §15.1 第 4 条要问的。
    带 ``symbol`` 与不带返回的集合是否一致，也只有直接控制参数才测得出。
    """

    params: dict[str, Any] = {"limit": limit, "page": page}
    if symbol:
        params["symbol"] = _exchange_symbol(symbol, virtual=True)
    raw = exchange._request(
        "GET",
        exchange._private_path("order/history"),
        params=params,
        private=True,
    )
    if not isinstance(raw, list):
        raise TypeError(f"order/history 返回的不是数组，而是 {type(raw).__name__}")
    return [item for item in raw if isinstance(item, dict)]


def field_inventory(orders: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """所有出现过的字段及其出现次数与示例值 —— 不是只列代码用到的那些。"""

    inventory: dict[str, dict[str, Any]] = {}
    for order in orders:
        for key, value in order.items():
            entry = inventory.setdefault(key, {"count": 0, "samples": []})
            entry["count"] += 1
            rendered = _short(value)
            if rendered not in entry["samples"] and len(entry["samples"]) < 3:
                entry["samples"].append(rendered)
    return inventory


def _distribution(orders: list[dict[str, Any]], key: str) -> Counter[str]:
    return Counter(str(order.get(key)) for order in orders)


def print_inventory(orders: list[dict[str, Any]], *, title: str) -> None:
    print(f"\n=== {title}：字段全集（共 {len(orders)} 条订单）===")
    if not orders:
        print("（空）")
        return
    inventory = field_inventory(orders)
    for key in sorted(inventory):
        entry = inventory[key]
        marker = "  ← 盈亏相关" if key in PNL_FIELDS else ""
        print(f"  {key:<22} 出现 {entry['count']:>4}/{len(orders)} 次  例：{entry['samples']}{marker}")


def print_summary(orders: list[dict[str, Any]], *, title: str) -> None:
    print(f"\n=== {title} ===")
    if not orders:
        print("订单数：0（order/history 为空）")
        return
    print(f"订单数：{len(orders)}")
    timestamps = [int(o["time"]) for o in orders if str(o.get("time", "")).isdigit()]
    if timestamps:
        print(f"时间范围：{_ms_to_iso(min(timestamps))}  →  {_ms_to_iso(max(timestamps))}")
    for key in ("symbol", "status", "type", "side", "positionSide", "reduceOnly"):
        distribution = _distribution(orders, key)
        rendered = "、".join(f"{value}×{count}" for value, count in distribution.most_common())
        print(f"{key:<14}：{rendered}")
    shapes = Counter(_client_id_shape(str(order.get("clientOrderId") or "")) for order in orders)
    rendered = "、".join(f"{shape}×{count}" for shape, count in shapes.most_common())
    print(f"{'clientOrderId':<14}：{rendered}")


def print_orders(orders: list[dict[str, Any]]) -> None:
    print(f"\n=== 订单明细（脱敏，共 {len(orders)} 条）===")
    for order in orders:
        print(
            f"  {_mask_order_id(order.get('orderId'))}  "
            f"{order.get('symbol')}  {order.get('side')}/{order.get('positionSide')}  "
            f"{order.get('status')}  qty={order.get('executedQty')}/{order.get('origQty')}  "
            f"avg={order.get('avgPrice')}  reduceOnly={order.get('reduceOnly')}  "
            f"cid={order.get('clientOrderId')}  "
            f"time={_ms_to_iso(order.get('time'))}"
        )


def print_pnl_verdict(orders: list[dict[str, Any]]) -> None:
    """§15.1 第 5 条 —— 本次探测最想确认的一件事。"""

    print("\n=== 结论：盈亏字段（§15.1 第 5 条）===")
    inventory = field_inventory(orders)
    found = [key for key in PNL_FIELDS if key in inventory]
    if found:
        print(f"✅ 找到盈亏相关字段：{found}")
        print("   → 链上 PnL 不必推导，spec §12 的加权平均成本模型可以砍掉。")
    else:
        print("❌ 没有任何盈亏相关字段。")
        print("   → 只能按 spec §12 推导 PnL，并标注 pnl_source=derived-avg-cost-v1。")
    print(f"   本次实际出现的全部字段：{sorted(inventory)}")


def diff_snapshots(before: dict[str, Any], after: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """按 orderId 找出新增订单。没有 orderId 的记录退化为整体比对。"""

    previous_ids = {str(order.get("orderId")) for order in before.get("orders", [])}
    return [order for order in after if str(order.get("orderId")) not in previous_ids]


def save_snapshot(path: Path, *, user_id: str, symbol: str | None, orders: list[dict[str, Any]]) -> None:
    payload = {
        "captured_at": datetime.now(UTC).isoformat(),
        "user_id": user_id,
        "symbol_filter": symbol,
        "orders": orders,
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n已保存快照：{path}")
    print("⚠️  快照含完整 orderId 与 clientOrderId，**不要提交到 git**，用完即删。")


def load_snapshot(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise SystemExit(f"快照不存在：{path}")
    return json.loads(path.read_text(encoding="utf-8"))


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


def probe_pagination(exchange: Any, *, symbol: str | None, limit: int) -> None:
    """§15.1 第 4 条的一半：page 参数到底管不管用。"""

    print("\n=== 分页探测（§15.1 第 4 条）===")
    first = fetch_orders(exchange, symbol=symbol, limit=limit, page=0)
    second = fetch_orders(exchange, symbol=symbol, limit=limit, page=1)
    first_ids = {str(order.get("orderId")) for order in first}
    second_ids = {str(order.get("orderId")) for order in second}
    print(f"page=0 返回 {len(first)} 条，page=1 返回 {len(second)} 条，交集 {len(first_ids & second_ids)} 条")
    if not second:
        print("→ page=1 为空。可能分页不可用，也可能订单总数不足一页（无法区分，需订单更多时复验）。")
    elif first_ids == second_ids:
        print("→ 两页完全相同：page 参数被忽略，分页不可用。水位线必须靠时间/订单号推进。")
    else:
        print("→ 两页不同：分页可用，水位线可以按页回溯。")


def probe_symbol_filter(exchange: Any, *, symbol: str | None, limit: int) -> None:
    """§15.1 第 4 条的另一半：不带 symbol 是否返回全部品种。"""

    if not symbol:
        print("\n（未指定 --symbol，跳过品种过滤探测）")
        return
    print("\n=== 品种过滤探测（§15.1 第 4 条）===")
    scoped = fetch_orders(exchange, symbol=symbol, limit=limit, page=0)
    unscoped = fetch_orders(exchange, symbol=None, limit=limit, page=0)
    scoped_ids = {str(order.get("orderId")) for order in scoped}
    unscoped_ids = {str(order.get("orderId")) for order in unscoped}
    print(f"带 symbol={symbol} 返回 {len(scoped)} 条；不带返回 {len(unscoped)} 条")
    if not scoped_ids <= unscoped_ids:
        print("→ 带 symbol 的结果**不**是不带的子集，行为异常，需要人工核对。")
    elif len(unscoped) > len(scoped):
        print("→ 不带 symbol 能拿到更多品种，水位线应当**不带** symbol 参数。")
    else:
        print("→ 两者一致（可能账户只有该品种的订单）。")


def print_position_cross_check(exchange: Any, orders: list[dict[str, Any]]) -> None:
    """订单历史与当前持仓的对照 —— 判断「平仓单在不在 order/history 里」的唯一办法。

    推理链：某个品种在历史里有成交、当前却无持仓 → 它被平过仓。此时若整份订单
    历史里**找不到任何平仓单**，就说明平仓单不进 ``order/history``（§15.1 第 2 条），
    方案必须走 spec §15.3 的退路。
    """

    print("\n=== 订单历史 vs 当前持仓（§15.1 第 2 条）===")
    positions = exchange.get_positions()
    print(f"当前持仓 {len(positions)} 个：")
    for position in positions:
        print(
            f"  {normalize_symbol(position.symbol):<12} {position.side:<6} "
            f"qty={position.quantity}  entryValue={position.entry_value}  "
            f"unrealized={position.unrealized_pnl}  leverage={position.leverage}"
        )

    closing = [order for order in orders if order.get("reduceOnly") in (True, "True", "true")]
    print(f"\n订单历史里 reduceOnly=True 的平仓单：{len(closing)} 条")

    traded = {normalize_symbol(str(order.get("symbol") or "")) for order in orders}
    held = {normalize_symbol(position.symbol) for position in positions}
    went_flat = sorted(traded - held)
    print(f"历史里出现过成交的品种：{sorted(traded)}")
    print(f"当前仍持仓的品种：{sorted(held)}")

    if went_flat and not closing:
        print(f"\n⚠️  {went_flat} 有成交历史但当前已无持仓，而订单历史里一条平仓单都没有。")
        print("    → 平仓单**不进** order/history。方案必须走 spec §15.3 的退路：")
        print("      只能做账户级（Δbalance）证明，单笔盈亏与信号归因不可达。")
    elif went_flat:
        print(f"\n✅ {went_flat} 已平仓，且订单历史里有 {len(closing)} 条平仓单 → 平仓可见。")
    else:
        print("\n→ 有成交历史的品种都还在持仓中，本次无法判断平仓单是否可见。")
        print("   请在 WEEX 网页手动平掉一笔仓，再跑一次本脚本。")


#: 候选端点。找的是「未文档化的平仓单 / 成交流水」—— 它存不存在直接决定方案可行性。
#: 判读按 docs/weex-virtual-api.md §7 的方法论：
#: 404 = 路径不存在；400/401/405 = 路径存在（缺参数 / 缺鉴权 / 方法不对）。
CANDIDATE_ENDPOINTS = (
    "position/allPosition",  # 已知可用，对照组
    "order/history",  # 已知可用，对照组
    "userTrades",  # 文档记为 404，复核一次
    "position/history",
    "position/allPosition/history",
    "trade/history",
    "fills",
    "transaction/history",
    "account/history",
    "balance/history",
)


def probe_endpoints(exchange: Any) -> None:
    """用裸 httpx 直接读状态码 —— ``_request`` 会把 404 包成异常，读不到原始码。"""

    print("\n=== 端点探测（找未文档化的平仓单 / 成交流水）===")
    print("判读：404 = 路径不存在；400/401/405 = 路径存在")
    for suffix in CANDIDATE_ENDPOINTS:
        path = exchange._private_path(suffix)
        try:
            headers = {"Content-Type": "application/json"}
            headers.update(exchange._auth_headers("GET", path, "", ""))
            response = exchange._client.request("GET", path, headers=headers)
        except Exception as exc:  # noqa: BLE001 - 探测脚本要把失败也打印出来，不能中断整轮
            print(f"  {suffix:<28} 调用失败：{_short(exc, 80)}")
            continue
        body = _short(response.text.replace("\n", " "), 72)
        print(f"  {suffix:<28} HTTP {response.status_code}  {body}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="只读探测 WEEX 虚拟盘 order/history 的字段形态（不建仓、不下单）",
    )
    parser.add_argument("--user-id", help="交易账户所属用户；只有一个启用账户时可省略")
    parser.add_argument("--symbol", help="只查该品种（如 BTC-USDT）；不给则不带 symbol 参数")
    parser.add_argument("--limit", type=int, default=MAX_LIMIT, help=f"每页条数，上限 {MAX_LIMIT}")
    parser.add_argument("--save", type=Path, help="保存本次快照为 JSON，供之后 --diff")
    parser.add_argument("--diff", type=Path, help="与之前保存的快照对比，只报告新增订单")
    parser.add_argument("--probe-pagination", action="store_true", help="对比 page=0 与 page=1")
    parser.add_argument("--probe-symbol-filter", action="store_true", help="对比带/不带 symbol")
    parser.add_argument("--show-orders", action="store_true", help="逐条打印脱敏后的订单明细")
    parser.add_argument(
        "--with-positions",
        action="store_true",
        help="对照当前持仓，判断平仓单是否可见（§15.1 第 2 条）",
    )
    parser.add_argument(
        "--probe-endpoints",
        action="store_true",
        help="探测未文档化的平仓单/成交流水端点",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if not 1 <= args.limit <= MAX_LIMIT:
        print(f"--limit 必须在 1..{MAX_LIMIT} 之间", file=sys.stderr)
        return 2

    settings: Settings = get_settings()
    if not settings.weex_virtual_only:
        print("拒绝运行：WEEX_VIRTUAL_ONLY 不是 true。这个脚本只允许在虚拟盘上跑。", file=sys.stderr)
        return 2

    with SessionLocal() as db:
        service = TradingCycleService(db=db, settings=settings)
        user_id = resolve_user(service, args.user_id)
        if user_id is None:
            return 2
        print(f"用户：{user_id}   虚拟盘：{settings.weex_virtual_only}")
        # 私有方法，但 spec §7 明确建议这样造客户端：它从 trading_accounts 读凭据，
        # 不必在脚本里重复处理 key/secret/passphrase 的加载与剥前缀。
        exchange = service._exchange_for_user(user_id)

        orders = fetch_orders(exchange, symbol=args.symbol, limit=args.limit, page=0)
        print_summary(orders, title="本次拉取概览")
        print_inventory(orders, title="本次拉取")
        print_pnl_verdict(orders)
        if args.show_orders:
            print_orders(orders)

        if args.diff is not None:
            before = load_snapshot(args.diff)
            added = diff_snapshots(before, orders)
            print(
                f"\n=== 与快照对比（{before.get('captured_at', '?')} 起）==="
                f"\n新增订单：{len(added)} 条"
            )
            if added:
                print("→ 新增订单就是快照之后发生的动作。若你刚在网页手动开/平仓，")
                print("  这里能看到它们，说明**手动单在 order/history 里可见**（§15.1 第 1 条）。")
                print_inventory(added, title="新增订单")
                print_orders(added)
            else:
                print("→ 没有新增订单。若你确实手动开平了仓，说明手动单**不在** order/history 里，")
                print("  方案前提塌陷，走 spec §15.3 的退路。请先确认账户是否就是这一个。")

        if args.with_positions:
            print_position_cross_check(exchange, orders)
        if args.probe_pagination:
            probe_pagination(exchange, symbol=args.symbol, limit=args.limit)
        if args.probe_symbol_filter:
            probe_symbol_filter(exchange, symbol=args.symbol, limit=args.limit)
        if args.probe_endpoints:
            probe_endpoints(exchange)

        if args.save is not None:
            save_snapshot(args.save, user_id=user_id, symbol=args.symbol, orders=orders)

    print("\n下一步：把上面「结论：盈亏字段」与「新增订单」两段贴回来。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
