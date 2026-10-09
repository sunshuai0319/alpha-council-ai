import json
from datetime import UTC, datetime

import httpx
import pytest

from app.config import Settings
from app.domain.enums import Action, RiskStatus
from app.domain.schemas import (
    ExecutionResult,
    MarketSnapshot,
    RiskDecision,
    TradeProposal,
    TradingCycleState,
)
from app.notifications.lark import (
    LarkNotificationError,
    LarkNotifier,
    build_trade_signal_card,
    signal_signature,
)


def _settings(**overrides) -> Settings:
    values = {
        "DATABASE_URL": "postgresql+psycopg://u:p@localhost/a",
        "ZILLIZ_URI": "http://localhost:19530",
        "ARK_API_KEY": "test-key",
        "LARK_APP_ID": "cli_test",
        "LARK_APP_SECRET": "local-secret",
        "LARK_RECEIVE_ID": "oc_one,oc_two",
    }
    values.update(overrides)
    return Settings(**values)


def _state(
    action: Action = Action.LONG,
    *,
    started_at: datetime = datetime(2026, 10, 6, 3, tzinfo=UTC),
    last_price: float = 100,
    position_size_pct: float = 0.1,
    stop_loss: float = 97,
    take_profit: float = 106,
    reasoning_summary: str = "trend and momentum agree",
) -> TradingCycleState:
    return TradingCycleState(
        user_id="u-1",
        cycle_id="cycle-1",
        started_at=int(started_at.timestamp() * 1000),
        symbol="BTC-USDT",
        market_snapshot=MarketSnapshot(
            symbol="BTC-USDT",
            captured_at=1_791_236_000_000,
            last_price=last_price,
        ),
        trade_proposal=TradeProposal(
            proposal_id="proposal-1",
            action=action,
            symbol="BTC-USDT",
            side="BUY" if action is Action.LONG else None,
            position_size_pct=position_size_pct,
            leverage=5,
            stop_loss=stop_loss,
            take_profit=take_profit,
            valid_until=9_999_999_999_999,
            confidence=0.8,
            reasoning_summary=reasoning_summary,
            evidence_refs=["indicator:trend"],
            model_version="test",
            trace_id="trace-1",
        ),
    )


def _recording_notifier(**overrides: object) -> tuple[LarkNotifier, list[httpx.Request]]:
    """返回一个只在内存里收请求的 notifier，以及它收到的消息请求列表。"""

    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("tenant_access_token/internal"):
            return httpx.Response(200, json={"code": 0, "tenant_access_token": "token-1", "expire": 7200})
        return httpx.Response(200, json={"code": 0, "data": {"message_id": "om-1"}})

    notifier = LarkNotifier(
        _settings(LARK_RECEIVE_ID="oc_one", **overrides),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        clock=lambda: 1000.0,
    )
    return notifier, requests


def _sent_messages(requests: list[httpx.Request]) -> list[httpx.Request]:
    return [request for request in requests if request.url.path.endswith("/messages")]


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
    assert all("\\n" not in content for content in contents)
    assert all("\n" in content for content in contents)
    assert any("风控状态" in content and "通过" in content for content in contents)
    assert any("参考下单价格" in content and "100" in content for content in contents)
    assert any("参考止损价" in content and "97" in content for content in contents)
    assert any("参考止盈价" in content and "106" in content for content in contents)
    risk = next(element for element in card["body"]["elements"] if element.get("element_id") == "risk")
    assert "\\n" not in risk["text"]["content"]


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


def test_constructing_a_notifier_does_not_open_a_connection_pool(monkeypatch: pytest.MonkeyPatch) -> None:
    """`get_cycle_service` 没有缓存，API 的每个请求都会构造一次 TradingCycleService。

    以前 `LarkNotifier.__init__` 直接建 `httpx.Client(...)`，而 `close()` 全仓库没有
    调用点 —— 每个请求都留下一个没人关闭的连接池。连接要等到真发通知时才建。
    """

    built: list[httpx.Client] = []
    real_client = httpx.Client

    def counting_client(*args: object, **kwargs: object) -> httpx.Client:
        client = real_client(*args, **kwargs)
        built.append(client)
        return client

    monkeypatch.setattr(httpx, "Client", counting_client)
    notifier = LarkNotifier(_settings())

    assert built == []
    assert notifier.client is notifier.client
    assert len(built) == 1
    notifier.close()


def test_suppressed_notification_does_not_open_a_connection_pool(monkeypatch: pytest.MonkeyPatch) -> None:
    """HOLD 被跳过时连一次 TCP 都不该开。"""

    built: list[httpx.Client] = []
    real_client = httpx.Client

    def counting_client(*args: object, **kwargs: object) -> httpx.Client:
        client = real_client(*args, **kwargs)
        built.append(client)
        return client

    monkeypatch.setattr(httpx, "Client", counting_client)
    notifier = LarkNotifier(_settings(LARK_RECEIVE_ID="oc_one"))

    assert notifier.notify(
        _state(Action.HOLD), RiskDecision(status=RiskStatus.ALLOWED, reasons=["hold"])
    ) is False
    assert built == []


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


# --- 决策指纹 -------------------------------------------------------------


def test_signal_signature_ignores_price_and_level_changes() -> None:
    """价格每轮都在动，决策没变就不该重新打扰。"""

    risk = RiskDecision(status=RiskStatus.ALLOWED, reasons=[])
    before = _state(last_price=2473.66, stop_loss=2565.1, take_profit=2336.48)
    after = _state(last_price=2477.73, stop_loss=2569.17, take_profit=2340.55)

    assert signal_signature(before, risk) == signal_signature(after, risk)


def test_signal_signature_ignores_reason_parameters() -> None:
    """`account_unavailable:{exc}` 里是异常原文，逐字比较会让指纹每轮都变。"""

    def signature(reason: str) -> str:
        return signal_signature(_state(), RiskDecision(status=RiskStatus.REJECTED, reasons=[reason]))

    assert signature("account_unavailable:connection refused") == signature("account_unavailable:timed out")
    assert signature("max_notional") != signature("max_position_notional")


def test_duplicate_signal_is_sent_once() -> None:
    notifier, requests = _recording_notifier()
    risk = RiskDecision(status=RiskStatus.ALLOWED, reasons=[])

    assert notifier.notify(_state(), risk) is True
    assert notifier.notify(_state(), risk) is False
    assert notifier.notify(_state(), risk) is False

    assert len(_sent_messages(requests)) == 1


def test_changed_action_is_sent_again() -> None:
    notifier, requests = _recording_notifier()
    risk = RiskDecision(status=RiskStatus.ALLOWED, reasons=[])

    assert notifier.notify(_state(Action.SHORT), risk) is True
    assert notifier.notify(_state(Action.LONG), risk) is True

    assert len(_sent_messages(requests)) == 2


def test_failed_send_does_not_update_signature() -> None:
    """发送失败时不能记账，否则一次网络抖动就把新信号永久吞掉。"""

    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        if request.url.path.endswith("tenant_access_token/internal"):
            return httpx.Response(200, json={"code": 0, "tenant_access_token": "token-1", "expire": 7200})
        attempts += 1
        if attempts == 1:
            return httpx.Response(500, text="boom")
        return httpx.Response(200, json={"code": 0, "data": {"message_id": "om-1"}})

    notifier = LarkNotifier(
        _settings(LARK_RECEIVE_ID="oc_one"),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        clock=lambda: 1000.0,
    )
    risk = RiskDecision(status=RiskStatus.ALLOWED, reasons=[])

    with pytest.raises(LarkNotificationError):
        notifier.notify(_state(), risk)
    assert notifier.notify(_state(), risk) is True


# --- 拒绝信号的抑制 -------------------------------------------------------


def test_single_trade_rejection_is_suppressed() -> None:
    """`position_already_open` 这类「这轮没下单」的常规拒绝不该刷屏。"""

    notifier, requests = _recording_notifier()
    risk = RiskDecision(status=RiskStatus.REJECTED, reasons=["position_already_open"])

    assert notifier.notify(_state(), risk) is False
    assert notifier.notify(_state(), risk) is False

    assert _sent_messages(requests) == []


def test_halting_rejection_is_sent() -> None:
    """熔断代表「系统停手了」，漏掉最危险，必须推。"""

    notifier, requests = _recording_notifier()
    risk = RiskDecision(status=RiskStatus.REJECTED, reasons=["daily_loss_limit"], halt=True)

    assert notifier.notify(_state(), risk) is True
    assert notifier.notify(_state(), risk) is False

    assert len(_sent_messages(requests)) == 1


def test_halting_rejection_after_allowed_signal_is_sent() -> None:
    notifier, requests = _recording_notifier()

    assert notifier.notify(_state(), RiskDecision(status=RiskStatus.ALLOWED, reasons=[])) is True
    assert notifier.notify(
        _state(), RiskDecision(status=RiskStatus.REJECTED, reasons=["daily_loss_limit"], halt=True)
    ) is True

    assert len(_sent_messages(requests)) == 2


def test_allowed_signal_then_single_trade_rejection_stays_silent() -> None:
    """截图里的场景：01:24 通过推了一条，01:30 变成 position_already_open。

    状态确实变了（指纹也变了），但单笔层面的拒绝被抑制，所以仍然只推一条。
    """

    notifier, requests = _recording_notifier()

    assert notifier.notify(
        _state(Action.SHORT), RiskDecision(status=RiskStatus.ALLOWED, reasons=[])
    ) is True
    assert notifier.notify(
        _state(Action.SHORT), RiskDecision(status=RiskStatus.REJECTED, reasons=["position_already_open"])
    ) is False

    assert len(_sent_messages(requests)) == 1


def test_single_trade_rejection_is_sent_when_switch_enabled() -> None:
    notifier, requests = _recording_notifier(LARK_NOTIFY_REJECTED_SIGNALS=True)
    risk = RiskDecision(status=RiskStatus.REJECTED, reasons=["position_already_open"])

    assert notifier.notify(_state(), risk) is True
    assert notifier.notify(_state(), risk) is False

    assert len(_sent_messages(requests)) == 1


# --- 时间与数字格式 -------------------------------------------------------


def test_subtitle_shows_utc_and_beijing_time() -> None:
    card = build_trade_signal_card(
        _state(started_at=datetime(2026, 10, 6, 3, tzinfo=UTC)),
        RiskDecision(status=RiskStatus.ALLOWED),
    )

    assert card["header"]["subtitle"]["content"] == "10-06 03:00 UTC（北京 10-06 11:00）"


def test_subtitle_beijing_time_carries_the_next_day() -> None:
    """UTC 16:00 之后北京已跨日，只写时分会被读成当天已经过去的凌晨。"""

    card = build_trade_signal_card(
        _state(started_at=datetime(2026, 10, 9, 20, tzinfo=UTC)),
        RiskDecision(status=RiskStatus.ALLOWED),
    )

    assert card["header"]["subtitle"]["content"] == "10-09 20:00 UTC（北京 10-10 04:00）"


def test_position_size_and_prices_are_rounded() -> None:
    card = build_trade_signal_card(
        _state(position_size_pct=0.27073999999999998, stop_loss=2565.108051, take_profit=2336.487922),
        RiskDecision(status=RiskStatus.ALLOWED),
    )
    details = next(element for element in card["body"]["elements"] if element.get("element_id") == "details")
    contents = [field["text"]["content"] for field in details["fields"]]

    assert any("仓位比例" in content and "27.07%" in content for content in contents)
    assert any("参考止损价" in content and "2565.11" in content for content in contents)
    assert any("参考止盈价" in content and "2336.49" in content for content in contents)


def test_non_finite_numbers_do_not_break_rendering() -> None:
    """畸形价格不能炸掉卡片渲染。

    `quantize` 对 Infinity/NaN 抛 InvalidOperation，而渲染异常会经 cycle 的
    `_best_effort` → `record_failure` 计进熔断计数器 —— 一个通知格式问题不该把账户停掉。
    """

    card = build_trade_signal_card(
        _state(last_price=float("inf"), stop_loss=float("nan"), take_profit=106),
        RiskDecision(status=RiskStatus.ALLOWED),
    )
    details = next(element for element in card["body"]["elements"] if element.get("element_id") == "details")
    contents = [field["text"]["content"] for field in details["fields"]]

    assert any("参考下单价格" in content and "Infinity" in content for content in contents)
    assert any("参考止损价" in content and "NaN" in content for content in contents)


def test_integer_prices_keep_no_trailing_zeros() -> None:
    card = build_trade_signal_card(
        _state(stop_loss=97, take_profit=106), RiskDecision(status=RiskStatus.ALLOWED)
    )
    details = next(element for element in card["body"]["elements"] if element.get("element_id") == "details")
    contents = [field["text"]["content"] for field in details["fields"]]

    assert any("参考止损价" in content and content.endswith("\n97") for content in contents)
    assert any("参考止盈价" in content and content.endswith("\n106") for content in contents)


# --- 中文化 ---------------------------------------------------------------


def test_card_translates_semicolon_joined_reasoning_summary() -> None:
    """`safe_hold` 永远产出 `";".join(errors[-3:])`，说明文字几乎总是拼接串。

    只翻译整串等于没译：多码拼在一起时一个都匹配不上精确表。
    """

    card = build_trade_signal_card(
        _state(reasoning_summary="entry_stop_loss_missing;entry_signal_evidence_missing"),
        RiskDecision(status=RiskStatus.REJECTED, reasons=["hold_no_order"]),
    )
    risk_element = next(
        element for element in card["body"]["elements"] if element.get("element_id") == "risk"
    )
    content = risk_element["text"]["content"]

    assert "提案缺少止损、提案缺少入场证据引用" in content
    assert "entry_stop_loss_missing" not in content


def test_card_translates_veto_fail_closed_prefix() -> None:
    """`graph.py` 的 fail-closed 走 `_hold_proposal(current, f"veto_fail_closed:{reasons}")`，
    这个前缀两边的前缀表都漏了。"""

    card = build_trade_signal_card(
        _state(reasoning_summary="veto_fail_closed:news_macro"),
        RiskDecision(status=RiskStatus.REJECTED, reasons=["hold_no_order"]),
    )
    risk_element = next(
        element for element in card["body"]["elements"] if element.get("element_id") == "risk"
    )
    content = risk_element["text"]["content"]

    assert "否决链异常，已安全观望：news_macro" in content
    assert "veto_fail_closed" not in content


def test_card_translates_machine_codes() -> None:
    card = build_trade_signal_card(
        _state(reasoning_summary="rule signal SHORT score=-0.35"),
        RiskDecision(status=RiskStatus.REJECTED, reasons=["position_already_open"], halt=True),
    )
    risk_element = next(
        element for element in card["body"]["elements"] if element.get("element_id") == "risk"
    )
    content = risk_element["text"]["content"]

    assert "该品种已有持仓，不再加仓" in content
    assert "规则信号 做空，分数 -0.35" in content
    assert "position_already_open" not in content
    assert "rule signal" not in content
