"""Lark international interactive-card notifications.

The worker only needs outbound REST calls. Event subscriptions are unrelated to
sending a card; if card callbacks are added later, use Lark's persistent
connection mode for a local development process without a public HTTPS URL.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

import httpx

from app.config import Settings
from app.domain.enums import Action, RiskStatus
from app.domain.schemas import ExecutionResult, RiskDecision, TradeProposal, TradingCycleState
from app.notifications.reasons import reason_parts

logger = logging.getLogger(__name__)

_BEIJING = timezone(timedelta(hours=8))


class LarkNotificationError(RuntimeError):
    """The Lark API rejected or could not process a notification."""


@dataclass(frozen=True)
class _Token:
    value: str
    expires_at: float


def _format_number(value: Any, places: int | None = None) -> str:
    """渲染数字；`places` 给定时先舍入到该位数，再统一去掉尾随零。

    仓位比例与价格都是浮点算出来的（0.27073999999999998），不量化就会在卡片上
    露出十五位小数。
    """

    if value is None:
        return "-"
    try:
        number = Decimal(str(value))
    except Exception:  # noqa: BLE001 - card rendering must not break a decision
        return str(value)
    # `quantize` 对 Infinity/NaN 抛 InvalidOperation。放它们走到 format 只会渲染成
    # "Infinity"（改动前就是这个行为），但异常会一路冒到 cycle 的 _best_effort →
    # record_failure 计进熔断计数器 —— 一个通知格式问题不该把账户停掉。
    if places is not None and number.is_finite():
        number = number.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)
    rendered = format(number, "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return rendered or "0"


def _format_time(milliseconds: int) -> str:
    """UTC 与北京时间并列 —— 卡片读者在国内，UTC 要心算 +8。

    北京时间**带日期**：UTC 16:00 之后北京已跨日，只写时分会被读成当天已经过去的凌晨。
    """

    moment = datetime.fromtimestamp(milliseconds / 1000, tz=UTC)
    beijing = moment.astimezone(_BEIJING)
    return f"{moment.strftime('%m-%d %H:%M')} UTC（北京 {beijing.strftime('%m-%d %H:%M')}）"


def _normalize_line_breaks(value: str) -> str:
    """Convert escaped line breaks from model text into card-rendered breaks."""

    return value.replace("\\r\\n", "\n").replace("\\n", "\n").replace("\\r", "\n")


_ACTION_LABELS = {
    Action.LONG: "做多",
    Action.SHORT: "做空",
    Action.CLOSE: "平仓",
    Action.HOLD: "观望",
}

_RISK_STATUS_LABELS = {
    RiskStatus.ALLOWED: "通过",
    RiskStatus.REJECTED: "拒绝",
    RiskStatus.PAUSED: "暂停",
}

_EXECUTION_STATUS_LABELS = {
    "NOT_EXECUTED": "未自动下单",
    "FILLED": "已成交",
    "OPEN": "已挂单",
    "PARTIALLY_FILLED": "部分成交",
    "PENDING": "等待成交",
    "CANCELED": "已撤单",
    "REJECTED": "执行被拒绝",
    "UNKNOWN": "状态未知",
    "SKIPPED": "已跳过",
}


def _action_code(proposal: TradeProposal | None) -> Action | None:
    if proposal is None:
        return None
    return proposal.action


def _action_label(proposal: TradeProposal | None) -> str:
    action = _action_code(proposal)
    return "无信号" if action is None else _ACTION_LABELS[action]


def _action_color(action: Action | None) -> str:
    if action is None:
        return "blue"
    return {Action.LONG: "green", Action.SHORT: "red", Action.CLOSE: "orange"}.get(action, "blue")


def _risk_status_label(status: RiskStatus) -> str:
    return _RISK_STATUS_LABELS.get(status, "未知")


def _execution_status_label(execution: ExecutionResult | None) -> str:
    if execution is None:
        return "未发送订单"
    return _EXECUTION_STATUS_LABELS.get(execution.status, "已记录")


def _md_field(label: str, value: str) -> dict[str, Any]:
    return {"is_short": True, "text": {"tag": "lark_md", "content": f"**{label}**\n{value}"}}


def _reason_code(reason: str) -> str:
    """指纹只用码本身，丢掉 `:` 后面的参数。

    `cycle.py` 里写的是 `f"account_unavailable:{exc}"` —— 异常原文进了理由，
    "connection refused" 与 "timed out" 是两条不同字符串，不归一化的话指纹每轮都变、
    每轮都推。
    """

    return reason.split(":", 1)[0].strip()


def signal_signature(state: TradingCycleState, risk: RiskDecision) -> str:
    """决策指纹：动作 + 风控状态 + 风控理由码。

    只含决策语义，**不含价格与信号分数** —— 价格每轮都在动，算进去等于不去重。
    """

    action = state.trade_proposal.action.value if state.trade_proposal else "NONE"
    codes = "|".join(sorted({_reason_code(reason) for reason in risk.reasons}))
    return f"{action}:{risk.status.value}:{codes}"


def build_trade_signal_card(
    state: TradingCycleState,
    risk: RiskDecision,
    execution: ExecutionResult | None = None,
) -> dict[str, Any]:
    """Build a Card 2.0 payload from a cycle decision without network access."""

    proposal = state.trade_proposal
    action_code = _action_code(proposal)
    action = _action_label(proposal)
    action_color = _action_color(action_code)
    market_price = state.market_snapshot.last_price if state.market_snapshot else None
    stop_loss = proposal.stop_loss if proposal else None
    take_profit = proposal.take_profit if proposal else None
    size_pct = proposal.position_size_pct * 100 if proposal else 0
    leverage = proposal.leverage if proposal else 0
    reasons = _normalize_line_breaks("、".join(reason_parts(reason) for reason in risk.reasons) or "无")
    # 说明文字同样要逐条翻译：`safe_hold` 写的是 `";".join(errors[-3:])`，
    # 整串丢给 reason_text 会一个码都匹配不上，原样冒英文。
    reasoning = _normalize_line_breaks(reason_parts(proposal.reasoning_summary) if proposal else "未生成交易提案")
    if len(reasoning) > 240:
        reasoning = f"{reasoning[:237]}..."
    # 暂停轮走 `_hold_proposal(state, "paused")`，它把 reasoning_summary 设成了同一个
    # halt_reason，两栏会渲染出同一句话，读起来像卡片卡住了。同句就不重复渲染第二栏。
    risk_text = f"**风控理由**\n{reasons}"
    if reasoning and reasoning != reasons:
        risk_text = f"{risk_text}\n\n**策略说明**\n{reasoning}"
    execution_text = _execution_status_label(execution)

    # Card 2.0 follows lark-im's card workflow: one primary focus, grouped
    # fields, a restrained blue palette, and no callback interaction.
    return {
        "schema": "2.0",
        "config": {
            "update_multi": True,
            "width_mode": "default",
            "style": {
                "text_size": {
                    "title": {"default": "heading-2", "pc": "heading-2", "mobile": "heading-3"},
                    "body": {"default": "normal", "pc": "normal", "mobile": "normal"},
                    "caption": {"default": "notation", "pc": "notation", "mobile": "notation"},
                },
                "color": {
                    "cus-muted": {
                        "light_mode": "rgba(100,106,115,1)",
                        "dark_mode": "rgba(150,155,163,1)",
                    }
                },
            },
        },
        "header": {
            "title": {"tag": "plain_text", "content": f"Alpha Council · {state.symbol}"},
            "subtitle": {"tag": "plain_text", "content": _format_time(state.started_at)},
            "template": "blue",
            "icon": {"tag": "standard_icon", "token": "ai-common_colorful"},
            "text_tag_list": [
                {
                    "tag": "text_tag",
                    "text": {"tag": "plain_text", "content": action},
                    "color": action_color,
                }
            ],
        },
        "body": {
            "direction": "vertical",
            "padding": "12px 12px 20px 12px",
            "vertical_spacing": "8px",
            "elements": [
                {
                    "tag": "div",
                    "element_id": "signal",
                    "text": {
                        "tag": "lark_md",
                        "content": f"**{action}**  <font color='{action_color}'>参考交易信号</font>",
                        "text_size": "title",
                        "lines": 2,
                    },
                },
                {
                    "tag": "div",
                    "element_id": "details-title",
                    "text": {
                        "tag": "lark_md",
                        "content": "**交易参数**",
                        "text_size": "heading-4",
                        "lines": 1,
                    },
                },
                {
                    "tag": "div",
                    "element_id": "details",
                    "fields": [
                        _md_field("参考下单价格", _format_number(market_price, places=2)),
                        _md_field("风控状态", _risk_status_label(risk.status)),
                        _md_field("参考止损价", _format_number(stop_loss, places=2)),
                        _md_field("参考止盈价", _format_number(take_profit, places=2)),
                        _md_field("仓位比例", f"{_format_number(size_pct, places=2)}%"),
                        _md_field("杠杆", f"{leverage}x"),
                    ],
                },
                {"tag": "hr", "element_id": "separator"},
                {
                    "tag": "div",
                    "element_id": "risk",
                    "text": {
                        "tag": "lark_md",
                        "content": risk_text,
                        "text_size": "body",
                        "lines": 6,
                    },
                },
                {
                    "tag": "div",
                    "element_id": "status",
                    "text": {
                        "tag": "lark_md",
                        "content": f"<font color='cus-muted'>执行状态：{execution_text}（通知模式不会自动下单）</font>",
                        "text_size": "caption",
                        "lines": 2,
                    },
                },
            ],
        },
    }


class LarkNotifier:
    """Small REST client for Lark internal app bots.

    Tokens are cached until five minutes before expiry. Message sends are not
    retried automatically because Lark's message API has no idempotency support.
    """

    def __init__(
        self,
        settings: Settings,
        *,
        client: httpx.Client | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.settings = settings
        self._injected_client = client
        self._client: httpx.Client | None = None
        self._clock = clock
        self._token: _Token | None = None
        #: 每个 (用户, 品种) 最后一次**推送成功**的决策指纹，用来消掉重复刷屏。
        #: 进程内存即可：worker 常驻，重启后最多多发一条，不值得为它加一张表。
        self._last_signatures: dict[tuple[str, str], str] = {}

    @property
    def client(self) -> httpx.Client:
        """惰性建连：没真发通知就不该占一个连接池。

        `get_cycle_service`（`app/api/dependencies.py`）没有缓存，API 的每个请求都会
        新建一个 `TradingCycleService`，而 API 进程从不发通知。以前在 `__init__` 里
        直接建 `httpx.Client`，于是每个请求都留下一个没人关闭的连接池。
        """

        if self._client is None:
            self._client = (
                self._injected_client
                if self._injected_client is not None
                else httpx.Client(timeout=self.settings.lark_timeout_seconds)
            )
        return self._client

    @property
    def configured(self) -> bool:
        return bool(
            self.settings.lark_app_id
            and self.settings.lark_app_secret
            and self.settings.lark_receive_id.strip()
        )

    def close(self) -> None:
        # 调用方注入的 client 归调用方所有，不能替它关。
        if self._client is not None and self._injected_client is None:
            self._client.close()

    def notify(
        self,
        state: TradingCycleState,
        risk: RiskDecision,
        execution: ExecutionResult | None = None,
    ) -> bool:
        action = state.trade_proposal.action.value if state.trade_proposal else "NONE"
        # 熔断/暂停代表「系统已经停手」，比任何单笔信号都重要，不能被 HOLD 过滤吞掉。
        # 两种都要认：触发熔断那轮提案可能恰好是观望（`halt=True`），熔断之后每轮
        # 都是 `status=PAUSED` 的 HOLD 提案 —— 后者拦不住，因为 `evaluate_risk`
        # 在 `paused` 分支早退，`halt` 是 False。漏掉它的后果是暂停后再无任何推送。
        account_stopped = risk.halt or risk.status is RiskStatus.PAUSED
        if (
            state.trade_proposal is not None
            and state.trade_proposal.action is Action.HOLD
            and not self.settings.lark_notify_hold
            and not account_stopped
        ):
            logger.info(
                "lark notification: user=%s symbol=%s action=HOLD result=skipped reason=hold_notifications_disabled",
                state.user_id,
                state.symbol,
            )
            return False
        if not self.configured:
            logger.warning(
                "lark notification: user=%s symbol=%s action=%s result=skipped reason=not_configured "
                "app_id_configured=%s app_secret_configured=%s receive_ids=%d",
                state.user_id,
                state.symbol,
                action,
                bool(self.settings.lark_app_id),
                bool(self.settings.lark_app_secret),
                len(self._receive_ids()),
            )
            return False
        # 单笔层面的拒绝（position_already_open、max_notional…）只是「这轮没下单」的常规
        # 噪声，默认不打扰。账户级熔断（risk.halt）必须推 —— 它代表系统已经停手，漏掉最危险。
        if (
            risk.status is RiskStatus.REJECTED
            and not risk.halt
            and not self.settings.lark_notify_rejected_signals
        ):
            logger.info(
                "lark notification: user=%s symbol=%s action=%s result=skipped "
                "reason=rejected_signal_suppressed",
                state.user_id,
                state.symbol,
                action,
            )
            return False
        key = (state.user_id, state.symbol)
        signature = signal_signature(state, risk)
        if self._last_signatures.get(key) == signature:
            logger.info(
                "lark notification: user=%s symbol=%s action=%s result=skipped reason=duplicate_signal",
                state.user_id,
                state.symbol,
                action,
            )
            return False
        card = build_trade_signal_card(state, risk, execution)
        content = json.dumps(card, ensure_ascii=False, separators=(",", ":"))
        token = self._tenant_access_token()
        url = f"{self.settings.lark_base_url.rstrip('/')}/open-apis/im/v1/messages"
        for receive_id in self._receive_ids():
            response = self.client.post(
                url,
                params={"receive_id_type": self.settings.lark_receive_id_type},
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json; charset=utf-8",
                },
                json={"receive_id": receive_id, "msg_type": "interactive", "content": content},
            )
            self._raise_for_api_error(response, "send message")
        # 全部收件人都成功后才记账：任一条失败都不该把这个信号永久静音。
        self._last_signatures[key] = signature
        logger.info(
            "lark notification: user=%s symbol=%s action=%s result=sent recipients=%d",
            state.user_id,
            state.symbol,
            action,
            len(self._receive_ids()),
        )
        return True

    def _receive_ids(self) -> tuple[str, ...]:
        return tuple(item.strip() for item in self.settings.lark_receive_id.split(",") if item.strip())

    def _tenant_access_token(self) -> str:
        now = self._clock()
        if self._token is not None and now < self._token.expires_at:
            return self._token.value
        url = f"{self.settings.lark_base_url.rstrip('/')}/open-apis/auth/v3/tenant_access_token/internal"
        response = self.client.post(
            url,
            headers={"Content-Type": "application/json; charset=utf-8"},
            json={"app_id": self.settings.lark_app_id, "app_secret": self.settings.lark_app_secret},
        )
        self._raise_for_api_error(response, "obtain tenant access token")
        payload = response.json()
        token = payload.get("tenant_access_token")
        if not token:
            raise LarkNotificationError("Lark token response did not include tenant_access_token")
        expires_in = max(int(payload.get("expire", 7200)), 60)
        self._token = _Token(value=token, expires_at=now + expires_in - min(300, expires_in // 2))
        return token

    @staticmethod
    def _raise_for_api_error(response: httpx.Response, operation: str) -> None:
        try:
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise LarkNotificationError(f"Lark {operation} request failed: {exc}") from exc
        if payload.get("code", 0) != 0:
            raise LarkNotificationError(
                f"Lark {operation} rejected: code={payload.get('code')} msg={payload.get('msg', 'unknown')}"
            )
