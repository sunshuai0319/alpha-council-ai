import json
from datetime import UTC, datetime

import httpx

from app.config import Settings
from app.domain.enums import Action, RiskStatus
from app.domain.schemas import (
    ExecutionResult,
    MarketSnapshot,
    RiskDecision,
    TradeProposal,
    TradingCycleState,
)
from app.notifications.lark import LarkNotifier, build_trade_signal_card


def _settings(**overrides) -> Settings:
    values = {
        "DATABASE_URL": "postgresql+psycopg://u:p@localhost/a",
        "MILVUS_URI": "http://localhost:19530",
        "ARK_API_KEY": "test-key",
        "LARK_APP_ID": "cli_test",
        "LARK_APP_SECRET": "local-secret",
        "LARK_RECEIVE_ID": "oc_one,oc_two",
    }
    values.update(overrides)
    return Settings(**values)


def _state(action: Action = Action.LONG) -> TradingCycleState:
    return TradingCycleState(
        user_id="u-1",
        cycle_id="cycle-1",
        started_at=int(datetime(2026, 10, 6, 3, tzinfo=UTC).timestamp() * 1000),
        symbol="BTC-USDT",
        market_snapshot=MarketSnapshot(
            symbol="BTC-USDT",
            captured_at=1_791_236_000_000,
            last_price=100,
        ),
        trade_proposal=TradeProposal(
            proposal_id="proposal-1",
            action=action,
            symbol="BTC-USDT",
            side="BUY" if action is Action.LONG else None,
            position_size_pct=0.1,
            leverage=5,
            stop_loss=97,
            take_profit=106,
            valid_until=9_999_999_999_999,
            confidence=0.8,
            reasoning_summary="trend and momentum agree",
            evidence_refs=["indicator:trend"],
            model_version="test",
            trace_id="trace-1",
        ),
    )


def test_card_2_schema_contains_reference_order_fields() -> None:
    card = build_trade_signal_card(
        _state(),
        RiskDecision(status=RiskStatus.ALLOWED, reasons=["risk checks passed"]),
        ExecutionResult(
            status="NOT_EXECUTED",
            proposal_id="proposal-1",
            client_order_id="client-1",
            message="notification mode",
        ),
    )

    assert card["schema"] == "2.0"
    assert card["header"]["template"] == "blue"
    assert card["header"]["text_tag_list"][0]["text"]["content"] == "做多"
    signal = next(element for element in card["body"]["elements"] if element.get("element_id") == "signal")
    assert "**做多**" in signal["text"]["content"]
    details = next(element for element in card["body"]["elements"] if element.get("element_id") == "details")
    contents = [field["text"]["content"] for field in details["fields"]]
    assert any("风控状态" in content and "通过" in content for content in contents)
    assert any("参考下单价格" in content and "100" in content for content in contents)
    assert any("参考止损价" in content and "97" in content for content in contents)
    assert any("参考止盈价" in content and "106" in content for content in contents)


def test_notifier_gets_one_token_and_sends_to_each_configured_recipient() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("tenant_access_token/internal"):
            return httpx.Response(200, json={"code": 0, "tenant_access_token": "token-1", "expire": 7200})
        return httpx.Response(200, json={"code": 0, "data": {"message_id": "om-1"}})

    notifier = LarkNotifier(
        _settings(),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        clock=lambda: 1000.0,
    )
    risk = RiskDecision(status=RiskStatus.ALLOWED)

    assert notifier.notify(_state(), risk) is True
    assert notifier.notify(_state(Action.SHORT), risk) is True

    assert [request.url.path for request in requests].count("/open-apis/auth/v3/tenant_access_token/internal") == 1
    message_requests = [request for request in requests if request.url.path.endswith("/messages")]
    assert len(message_requests) == 4
    assert all(request.headers["authorization"] == "Bearer token-1" for request in message_requests)
    payload = json.loads(message_requests[0].content)
    assert payload["msg_type"] == "interactive"
    assert json.loads(payload["content"])["schema"] == "2.0"
    assert message_requests[0].url.params["receive_id_type"] == "chat_id"


def test_hold_is_not_sent_by_default() -> None:
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={"code": 0, "tenant_access_token": "token", "expire": 7200})

    notifier = LarkNotifier(
        _settings(LARK_RECEIVE_ID="oc_one"),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    assert notifier.notify(
        _state(Action.HOLD), RiskDecision(status=RiskStatus.ALLOWED, reasons=["hold"])
    ) is False
    assert calls == 0
