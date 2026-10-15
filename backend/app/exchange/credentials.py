"""从 TradingAccount 行解析 WEEX 私有凭证。

原先这段逻辑内联在 ``TradingCycleService._exchange_for_user``（app/services/cycle.py）。
采集层也要用它，而构造 TradingCycleService 会连带建 LLM / RAG / Lark 客户端，
代价太大 —— 提到这里让两边共用同一份规则。
"""

from __future__ import annotations

from app.db.models import TradingAccount
from app.exchange.weex import WeexCredentials

#: 历史占位写法（`env:WEEX_API_KEY`），不是真凭证。
ENV_PLACEHOLDER_PREFIX = "env:"


def credentials_for_account(account: TradingAccount | None) -> WeexCredentials | None:
    """三个引用齐全且都不是占位符时返回凭证，否则 ``None``。

    返回 ``None`` 不是错误：调用方要能区分「这个账户没配凭证」与「凭证错了」，
    前者跳过即可，后者要告警。
    """

    if account is None:
        return None
    refs = (account.api_key_ref, account.api_secret_ref, account.passphrase_ref)
    if not all(refs):
        return None
    if any(ref.startswith(ENV_PLACEHOLDER_PREFIX) for ref in refs):
        return None
    return WeexCredentials(
        api_key=account.api_key_ref,
        api_secret=account.api_secret_ref,
        passphrase=account.passphrase_ref,
    )
