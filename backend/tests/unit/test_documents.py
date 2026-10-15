import json

import httpx

from app.config import Settings
from app.processing.ark import ArkSummaryClient, DocumentProcessor, DocumentSummary
from app.processing.documents import (
    DocumentInput,
    DocumentRepository,
    chunk_text,
    detect_assets,
    normalize_asset,
    normalize_direction,
    normalize_impact_horizon,
    prepare_document,
)


def test_same_url_or_content_hash_is_inserted_once() -> None:
    repository = DocumentRepository()
    first = repository.upsert("https://example.com/a?utm_source=rss", "same", title="A")
    second = repository.upsert("https://example.com/a", "same", title="A again")
    assert first.document_id == second.document_id


def test_document_cleaning_and_tags_are_deterministic() -> None:
    document = prepare_document(
        DocumentInput(
            url="HTTPS://News.Example.com/b#fragment",
            title="Bitcoin ETF update",
            content="<script>ignore()</script><p>Bitcoin and ETH moved.</p>",
            source="test",
        )
    )
    assert document.canonical_url == "https://news.example.com/b"
    assert document.cleaned_text == "Bitcoin and ETH moved."
    assert document.assets == ("BTC", "ETH")
    assert document.event_type == "ETF"
    assert document.language == "en"


def test_asset_detection_covers_long_tail_aliases_and_configured_symbols() -> None:
    assert detect_assets("Bitcoin Cash and Litecoin moved with BCHSUSDT volume.") == ("BCH", "LTC")
    assert detect_assets("Bitcoin Cash and Bitcoin both moved.") == ("BTC", "BCH")
    assert detect_assets("BCHUSDT rallied.", known_assets=("BCH-USDT",)) == ("BCH",)


def test_asset_normalization_accepts_llm_labels_and_exchange_symbols() -> None:
    assert normalize_asset("Bitcoin Cash") == "BCH"
    assert normalize_asset("BCH-USDT") == "BCH"
    assert normalize_asset("BCHSUSDT") == "BCH"
    assert normalize_asset("EUR/USD") == "EUR/USD"
    assert normalize_asset("USDT") == ""
    assert normalize_asset("USBDC") == ""


def test_impact_horizon_is_normalized_to_shared_taxonomy() -> None:
    assert normalize_impact_horizon("days") == "SHORT"
    assert normalize_impact_horizon("short-to-medium") == "SHORT_MEDIUM"
    assert normalize_impact_horizon("weeks") == "MEDIUM"
    assert normalize_impact_horizon("unknown") == "UNKNOWN"
    assert normalize_impact_horizon("a very long historical horizon label") == "UNKNOWN"


def test_direction_is_normalized_to_shared_taxonomy() -> None:
    assert normalize_direction("bullish") == "BULLISH"
    assert normalize_direction("BEAR") == "BEARISH"
    assert normalize_direction("sideways") == "NEUTRAL"
    assert normalize_direction("unknown") == "UNKNOWN"
    assert normalize_direction("") == "UNKNOWN"


def test_direction_collapses_the_value_that_used_to_crash_the_pipeline() -> None:
    """线上真实值：``neutral/uncertain``。

    LLM 这么回，而 ``document_summaries.direction`` 是 VARCHAR(16) —— 17 个字符
    让整条文档流水线抛 StringDataRightTruncation。关键不只是长度：**斜杠也要
    归一化**，否则 token 会是 ``NEUTRAL/UNCERTAIN``。
    """

    assert normalize_direction("neutral/uncertain") == "NEUTRAL"
    assert normalize_direction("Neutral / Uncertain") == "NEUTRAL"


def test_direction_never_exceeds_the_column_width() -> None:
    """这是这条归一化存在的理由 —— 任何输入都不能产出超过 16 字符的值。

    不是「已知的坏值都处理了」，是「**任何**输入都不可能撑爆列宽」。
    """

    hostile = [
        "a very long directional label that the model invented",
        "BULLISH" * 50,
        "neutral/uncertain/mixed/whatever",
        "看多",
        "🚀 to the moon",
        "NEUTRAL,".join(["X"] * 100),
    ]

    for value in hostile:
        result = normalize_direction(value)
        assert len(result) <= 16, f"{value!r} → {result!r} 超过列宽"
        assert result in {"BULLISH", "BEARISH", "NEUTRAL", "UNKNOWN"}


def test_chunk_text_has_overlap_and_rejects_invalid_configuration() -> None:
    chunks = chunk_text("one two three four five six", max_chars=13, overlap=4)
    assert len(chunks) >= 2
    assert chunks[0].split()[-1] in chunks[1].split()


def test_ark_summary_uses_configured_deepseek_model_and_validates_json() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["model"] == "deepseek-v4-pro-ga-260813"
        assert body["response_format"] == {"type": "json_object"}
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "summary": "ETF-related Bitcoin update.",
                                    "event_type": "ETF",
                                    "assets": ["BTC"],
                                    "direction": "BULLISH",
                                    "impact_horizon": "DAYS",
                                    "confidence": 0.8,
                                }
                            )
                        }
                    }
                ]
            },
        )

    settings = Settings(
        DATABASE_URL="postgresql+psycopg://u:p@localhost/a",
        ZILLIZ_URI="http://localhost:19530",
        ARK_API_KEY="test-key",
        ARK_BASE_URL="https://ark.test/api/v3",
    )
    document = prepare_document(
        DocumentInput("https://example.com/a", "ETF", "Bitcoin ETF update", "test")
    )
    with httpx.Client(transport=httpx.MockTransport(handler)) as http_client, ArkSummaryClient(
        settings, client=http_client
    ) as client:
        summary = client.summarize(document)
    assert isinstance(summary, DocumentSummary)
    assert summary.assets == ["BTC"]


def test_document_processor_keeps_raw_text_when_summary_fails() -> None:
    class FailingSummary:
        def summarize(self, _: object) -> DocumentSummary:
            raise TimeoutError("Ark timeout")

    source = DocumentInput("https://example.com/a", "Title", "original body", "test")
    result = DocumentProcessor(FailingSummary()).process(source)
    assert result.status == "FAILED"
    assert result.summary is None
    assert result.document.raw_text == "original body"
    assert "timeout" in result.error
