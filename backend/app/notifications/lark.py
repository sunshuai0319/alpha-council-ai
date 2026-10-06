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
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import httpx

from app.config import Settings
from app.domain.enums import Action, RiskStatus
from app.domain.schemas import ExecutionResult, RiskDecision, TradeProposal, TradingCycleState

logger = logging.getLogger(__name__)


class LarkNotificationError(RuntimeError):
    """The Lark API rejected or could not process a notification."""


@dataclass(frozen=True)
class _Token:
    value: str
    expires_at: float


def _format_number(value: Any) -> str:
    if value is None:
        return "-"
    try:
        number = Decimal(str(value))
    except Exception:  # noqa: BLE001 - card rendering must not break a decision
        return str(value)
    rendered = format(number, "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return rendered or "0"


def _format_time(milliseconds: int) -> str:
    return datetime.fromtimestamp(milliseconds / 1000, tz=UTC).strftime("%m-%d %H:%M UTC")


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
    return {"is_short": True, "text": {"tag": "lark_md", "content": f"**{label}**\\n{value}"}}


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
    reasons = "、".join(risk.reasons) if risk.reasons else "无"
    reasoning = proposal.reasoning_summary if proposal else "未生成交易提案"
    if len(reasoning) > 240:
        reasoning = f"{reasoning[:237]}..."
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
                    "element_id": "details",
                    "fields": [
                        _md_field("参考下单价格", _format_number(market_price)),
                        _md_field("风控状态", _risk_status_label(risk.status)),
                        _md_field("参考止损价", _format_number(stop_loss)),
                        _md_field("参考止盈价", _format_number(take_profit)),
                        _md_field("仓位比例", f"{_format_number(size_pct)}%"),
                        _md_field("杠杆", f"{leverage}x"),
                    ],
                },
                {"tag": "hr", "element_id": "separator"},
                {
                    "tag": "div",
                    "element_id": "risk",
                    "text": {
                        "tag": "lark_md",
                        "content": (
                            f"**风控理由**\\n{reasons}\\n\\n"
                            f"**策略说明**\\n{reasoning}"
                        ),
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
        self.client = client or httpx.Client(timeout=settings.lark_timeout_seconds)
        self._owns_client = client is None
        self._clock = clock
        self._token: _Token | None = None

    @property
    def configured(self) -> bool:
        return bool(
            self.settings.lark_app_id
            and self.settings.lark_app_secret
            and self.settings.lark_receive_id.strip()
        )

    def close(self) -> None:
        if self._owns_client:
            self.client.close()

    def notify(
        self,
        state: TradingCycleState,
        risk: RiskDecision,
        execution: ExecutionResult | None = None,
    ) -> bool:
        if (
            state.trade_proposal is not None
            and state.trade_proposal.action is Action.HOLD
            and not self.settings.lark_notify_hold
        ):
            return False
        if not self.configured:
            logger.info("Lark notification skipped: app credentials or receive id are not configured")
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
        logger.info("Lark notification sent: user=%s symbol=%s action=%s", state.user_id, state.symbol, _action_label(state.trade_proposal))
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
