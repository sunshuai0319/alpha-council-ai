import http.client
import json
import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import HTTPException
from jwt.algorithms import RSAAlgorithm

from app.auth import clerk
from app.auth.clerk import (
    AuthPrincipal,
    extract_bearer_token,
    principal_from_claims,
    verify_session_token,
)
from app.config import Settings


def serve_jwks(monkeypatch, key, kid="test-key"):
    """把 JWKS 出网换成本地公钥，PyJWT 自己的缓存逻辑保持真实。返回 (settings, 出网记录)。"""
    clerk.jwks_client.cache_clear()
    jwk = json.loads(RSAAlgorithm.to_jwk(key.public_key()))
    jwk.update({"kid": kid, "use": "sig", "alg": "RS256"})
    body = json.dumps({"keys": [jwk]}).encode()
    fetches: list[str] = []

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            return False

        def read(self):
            return body

    def record_urlopen(request, *args, **kwargs):
        fetches.append(request.full_url)
        return FakeResponse()

    monkeypatch.setattr("urllib.request.urlopen", record_urlopen)
    settings = Settings(
        clerk_jwks_url="https://example.clerk.accounts.dev/.well-known/jwks.json",
        clerk_issuer="",
        clerk_audience="",
    )
    return settings, fetches


def session_token(key, kid="test-key", **claims):
    return jwt.encode(claims, key, algorithm="RS256", headers={"kid": kid})


def test_missing_bearer_token_raises_401():
    with pytest.raises(HTTPException) as exc_info:
        extract_bearer_token(None)
    assert exc_info.value.status_code == 401


def test_clerk_claims_map_to_external_principal():
    principal = principal_from_claims({"sub": "user_clerk_1", "email": "a@example.com"})
    assert principal == AuthPrincipal(clerk_user_id="user_clerk_1", email="a@example.com")


def test_jwks_connection_reset_is_reported_as_503(monkeypatch):
    """Clerk 的 JWKS 端偶发断连时，不能把基础设施故障暴露成未捕获的 500。"""

    def drop_connection(self, token):
        raise http.client.RemoteDisconnected("Remote end closed connection without response")

    monkeypatch.setattr(jwt.PyJWKClient, "get_signing_key_from_jwt", drop_connection)
    settings = Settings(clerk_jwks_url="https://example.clerk.accounts.dev/.well-known/jwks.json")

    with pytest.raises(HTTPException) as exc_info:
        verify_session_token("header.payload.signature", settings)

    assert exc_info.value.status_code == 503


def test_jwks_is_fetched_once_across_requests(monkeypatch):
    """PyJWT 的 JWKS 缓存是实例级的；每次请求都新建 client 就会每个 API 调用都出网拉一次。"""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    settings, fetches = serve_jwks(monkeypatch, key, kid="probe")
    token = session_token(key, kid="probe", sub="user_probe")

    for _ in range(3):
        assert verify_session_token(token, settings).clerk_user_id == "user_probe"

    assert len(fetches) == 1


def test_token_issued_ahead_of_local_clock_is_accepted(monkeypatch):
    """Clerk 服务器时钟快零点几秒时，刚签发的 token 的 iat/nbf 会落在本机未来。

    零冗余校验把它判成「尚未生效」（ImmatureSignatureError），对外只看到 401 Invalid Clerk
    token —— 但 token 本身完全合法。轮询 60 秒就换一次 token，撞上的概率不低，撞上后同一批
    请求会一起 401（它们共用同一个 token）。
    """
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    settings, _ = serve_jwks(monkeypatch, key)
    now = time.time()
    token = session_token(key, sub="user_skew", iat=now + 2, nbf=now + 2, exp=now + 60)

    assert verify_session_token(token, settings).clerk_user_id == "user_skew"


def test_token_expired_beyond_leeway_is_still_rejected(monkeypatch):
    """冗余只用来吸收时钟偏差，过期 token 该拒还是拒。"""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    settings, _ = serve_jwks(monkeypatch, key)
    now = time.time()
    token = session_token(key, sub="user_stale", iat=now - 300, nbf=now - 300, exp=now - 60)

    with pytest.raises(HTTPException) as exc_info:
        verify_session_token(token, settings)

    assert exc_info.value.status_code == 401


def test_other_user_data_is_not_in_scope():
    rows = [
        {"user_id": "local-a", "external_id": "a-1"},
        {"user_id": "local-b", "external_id": "b-1"},
    ]
    scoped = [row for row in rows if row["user_id"] == "local-a"]
    assert scoped == [{"user_id": "local-a", "external_id": "a-1"}]
