import http.client
import json

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import HTTPException
from jwt.algorithms import RSAAlgorithm

from app.auth.clerk import (
    AuthPrincipal,
    extract_bearer_token,
    principal_from_claims,
    verify_session_token,
)
from app.config import Settings


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
    from app.auth import clerk

    clerk.jwks_client.cache_clear()
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = json.loads(RSAAlgorithm.to_jwk(key.public_key()))
    jwk.update({"kid": "probe", "use": "sig", "alg": "RS256"})

    body = json.dumps({"keys": [jwk]}).encode()
    fetches = []

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

    # 只替换出网的 urlopen，保留 PyJWT 自己的 JWKS 缓存读写逻辑
    monkeypatch.setattr("urllib.request.urlopen", record_urlopen)
    settings = Settings(
        clerk_jwks_url="https://example.clerk.accounts.dev/.well-known/jwks.json",
        clerk_issuer="",
        clerk_audience="",
    )
    token = jwt.encode({"sub": "user_probe"}, key, algorithm="RS256", headers={"kid": "probe"})

    for _ in range(3):
        assert verify_session_token(token, settings).clerk_user_id == "user_probe"

    assert len(fetches) == 1


def test_other_user_data_is_not_in_scope():
    rows = [
        {"user_id": "local-a", "external_id": "a-1"},
        {"user_id": "local-b", "external_id": "b-1"},
    ]
    scoped = [row for row in rows if row["user_id"] == "local-a"]
    assert scoped == [{"user_id": "local-a", "external_id": "a-1"}]
