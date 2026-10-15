"""从 TradingAccount 解析 WEEX 凭证。

这段逻辑原先埋在 TradingCycleService._exchange_for_user 里。监听器也要用它，
但构造那个服务会连带建 LLM / RAG / Lark 客户端 —— 提出来才能单独复用。
"""

from app.db.models import TradingAccount
from app.exchange.credentials import credentials_for_account


def _account(**overrides) -> TradingAccount:
    values = {
        "id": "acct-1",
        "user_id": "u-1",
        "api_key_ref": "key-1",
        "api_secret_ref": "secret-1",
        "passphrase_ref": "pass-1",
    }
    values.update(overrides)
    return TradingAccount(**values)


def test_returns_credentials_when_all_refs_present():
    credentials = credentials_for_account(_account())

    assert credentials is not None
    assert credentials.api_key == "key-1"
    assert credentials.api_secret == "secret-1"
    assert credentials.passphrase == "pass-1"


def test_returns_none_when_account_is_missing():
    assert credentials_for_account(None) is None


def test_returns_none_when_any_ref_is_missing():
    assert credentials_for_account(_account(passphrase_ref=None)) is None


def test_returns_none_for_env_placeholder_refs():
    """`env:` 前缀是历史占位写法，不是真凭证 —— 当成没有。"""

    assert credentials_for_account(_account(api_key_ref="env:WEEX_API_KEY")) is None
