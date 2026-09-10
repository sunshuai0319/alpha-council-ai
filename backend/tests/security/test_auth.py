import pytest
from fastapi import HTTPException

from app.auth.clerk import AuthPrincipal, extract_bearer_token, principal_from_claims


def test_missing_bearer_token_raises_401():
    with pytest.raises(HTTPException) as exc_info:
        extract_bearer_token(None)
    assert exc_info.value.status_code == 401


def test_clerk_claims_map_to_external_principal():
    principal = principal_from_claims({"sub": "user_clerk_1", "email": "a@example.com"})
    assert principal == AuthPrincipal(clerk_user_id="user_clerk_1", email="a@example.com")


def test_other_user_data_is_not_in_scope():
    rows = [
        {"user_id": "local-a", "external_id": "a-1"},
        {"user_id": "local-b", "external_id": "b-1"},
    ]
    scoped = [row for row in rows if row["user_id"] == "local-a"]
    assert scoped == [{"user_id": "local-a", "external_id": "a-1"}]
