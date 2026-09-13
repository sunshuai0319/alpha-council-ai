"""LLM 否决必须是封闭枚举，且有效否决必须有证据。

spec 2.3：`证据不足` 不再是合法否决理由 —— 证据不足时规则信号器自己就 HOLD 了。
veto=True 但没有证据引用 → schema 拒绝，由 veto 层 fail-closed。
"""

import pytest
from pydantic import ValidationError

from app.domain.schemas import VetoReason, VetoVerdict


def test_veto_true_requires_evidence() -> None:
    with pytest.raises(ValidationError):
        VetoVerdict(veto=True, reasons=[VetoReason.NEWS_SHOCK], evidence_refs=[], reasoning_summary="x")


def test_veto_false_allows_empty_evidence() -> None:
    verdict = VetoVerdict(veto=False, reasons=[], evidence_refs=[], reasoning_summary="no objection")
    assert verdict.veto is False


def test_unknown_reason_is_rejected() -> None:
    with pytest.raises(ValidationError):
        VetoVerdict(veto=True, reasons=["WHATEVER"], evidence_refs=["doc:1"], reasoning_summary="x")


def test_valid_veto_parses() -> None:
    verdict = VetoVerdict(
        veto=True,
        reasons=[VetoReason.NEWS_SHOCK],
        evidence_refs=["doc:1", "doc:2"],
        reasoning_summary="sharp news contradicts the long signal",
    )
    assert verdict.veto is True
    assert verdict.reasons == [VetoReason.NEWS_SHOCK]
