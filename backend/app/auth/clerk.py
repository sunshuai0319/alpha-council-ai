import logging
from dataclasses import dataclass
from functools import lru_cache
from typing import Any
from uuid import uuid4

import jwt
from fastapi import HTTPException, status
from jwt.exceptions import PyJWKClientConnectionError

from app.config import Settings, get_settings
from app.db.models import User

logger = logging.getLogger(__name__)

#: Clerk 会话 token 只活 60 秒，`iat`/`nbf` 由 Clerk 服务器的时钟签发。本机时钟只要比它慢
#: 零点几秒，刚签发就被用上的 token 就会落进「本机未来」——零冗余校验判成尚未生效
#: （ImmatureSignatureError），对外只表现为 401 Invalid Clerk token。留几秒冗余吸收这点偏差，
#: 也覆盖 NTP 校时前后的抖动。
CLOCK_SKEW_LEEWAY_SECONDS = 5


@dataclass(frozen=True)
class AuthPrincipal:
    clerk_user_id: str
    email: str | None = None
    display_name: str | None = None


def extract_bearer_token(authorization: str | None) -> str:
    if not authorization:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing bearer token")
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid bearer token")
    return token.strip()


def principal_from_claims(claims: dict[str, Any]) -> AuthPrincipal:
    clerk_user_id = claims.get("sub")
    if not isinstance(clerk_user_id, str) or not clerk_user_id:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Token has no user subject")
    return AuthPrincipal(
        clerk_user_id=clerk_user_id,
        email=claims.get("email") if isinstance(claims.get("email"), str) else None,
        display_name=claims.get("name") if isinstance(claims.get("name"), str) else None,
    )


@lru_cache(maxsize=4)
def jwks_client(jwks_url: str) -> jwt.PyJWKClient:
    """按 URL 复用客户端，让 PyJWT 实例级的 JWKS 缓存真正生效；否则每个请求都要出网拉一次。"""
    return jwt.PyJWKClient(jwks_url)


def verify_session_token(token: str, settings: Settings | None = None) -> AuthPrincipal:
    settings = settings or get_settings()
    if not settings.clerk_jwks_url:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Clerk JWKS is not configured")

    decode_kwargs: dict[str, Any] = {
        "algorithms": ["RS256"],
        "leeway": CLOCK_SKEW_LEEWAY_SECONDS,
    }
    if settings.clerk_issuer:
        decode_kwargs["issuer"] = settings.clerk_issuer
    if settings.clerk_audience:
        decode_kwargs["audience"] = settings.clerk_audience
    else:
        decode_kwargs["options"] = {"verify_aud": False}

    try:
        signing_key = jwks_client(settings.clerk_jwks_url).get_signing_key_from_jwt(token).key
        claims = jwt.decode(token, signing_key, **decode_kwargs)
    except (PyJWKClientConnectionError, OSError) as exc:
        # Clerk 端偶发断连/超时属于依赖不可用，不能让它冒泡成 500（会丢掉 CORS 头，前端只看到 CORS 报错）
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Clerk JWKS is unavailable"
        ) from exc
    except (jwt.PyJWTError, ValueError) as exc:
        # 401 的根因就那几类（过期 / 尚未生效 / 签名不符 / JWKS 缺 kid / iss 不符），
        # 对外文案统一是 Invalid Clerk token，只有异常类名能区分。只记类名，不带 token 内容。
        logger.warning("clerk token rejected: %s", type(exc).__name__)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid Clerk token") from exc
    return principal_from_claims(claims)


def sync_local_user(db: Any, principal: AuthPrincipal) -> User:
    user = db.query(User).filter(User.clerk_user_id == principal.clerk_user_id).one_or_none()
    if user is None:
        user = User(
            id=str(uuid4()),
            clerk_user_id=principal.clerk_user_id,
            email=principal.email,
            display_name=principal.display_name,
        )
        db.add(user)
    else:
        user.email = principal.email or user.email
        user.display_name = principal.display_name or user.display_name
    db.commit()
    db.refresh(user)
    return user
