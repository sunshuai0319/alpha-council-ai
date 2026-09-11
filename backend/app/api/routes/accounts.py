from typing import Literal
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator
from sqlalchemy import select

from app.api.dependencies import get_cycle_service
from app.auth.dependencies import get_current_user
from app.config import Settings
from app.db.models import TradingAccount
from app.risk.engine import RiskLimits
from app.services.cycle import TradingCycleService

router = APIRouter(prefix="/api/accounts", tags=["accounts"])

#: 从 .env 复制凭证行时会带着变量名前缀一起粘进来，签名因此永远失败。
#: 剥掉前缀并按名匹配，避免把 WEEX_API_SECRET=abc 整串当成 secret。
_WEEX_ENV_PREFIXES = {
    "api_key_ref": "WEEX_API_KEY=",
    "api_secret_ref": "WEEX_API_SECRET=",
    "passphrase_ref": "WEEX_PASSPHRASE=",
}


class AccountCreate(BaseModel):
    api_key_ref: str = Field(min_length=1)
    api_secret_ref: str = Field(min_length=1)
    passphrase_ref: str = Field(min_length=1)
    environment: Literal["virtual"] = "virtual"
    provider: Literal["weex"] = "weex"

    @field_validator("api_key_ref", "api_secret_ref", "passphrase_ref")
    @classmethod
    def reject_environment_references(cls, value: str, info: ValidationInfo) -> str:
        if value.startswith("env:"):
            raise ValueError("environment references are not supported")
        prefix = _WEEX_ENV_PREFIXES.get(info.field_name or "")
        if prefix and value.startswith(prefix):
            value = value[len(prefix):]
        return value.strip()


class AccountRiskLimits(BaseModel):
    """账户级风控偏好。只能收得更紧，越界由 `_validate` 明确拒绝。"""

    model_config = ConfigDict(extra="forbid")

    max_position_notional_pct: float | None = Field(default=None, gt=0, le=1)
    max_single_trade_risk_pct: float | None = Field(default=None, gt=0, le=1)
    max_daily_loss_pct: float | None = Field(default=None, gt=0, le=1)
    max_consecutive_losses: int | None = Field(default=None, ge=1)


class AccountUpdate(BaseModel):
    enabled: bool
    #: 不收 None 之外的缺省语义：不传就是不动已存的值。
    risk_limits: AccountRiskLimits | None = None


def _platform_limits(settings: Settings) -> dict[str, float | int]:
    return {
        "max_position_notional_pct": settings.max_position_notional_pct,
        "max_single_trade_risk_pct": settings.max_single_trade_risk_pct,
        "max_daily_loss_pct": settings.max_daily_loss_pct,
        "max_consecutive_losses": settings.max_consecutive_losses,
    }


def _validate_risk_limits(
    limits: AccountRiskLimits, settings: Settings
) -> dict[str, float | int]:
    """越界直接报错并说清上限 —— 静默夹回会让用户以为自己设成了。"""

    provided = limits.model_dump(exclude_none=True)
    platform = _platform_limits(settings)
    for key, value in provided.items():
        if value > platform[key]:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"{key} cannot exceed the platform limit {platform[key]}",
            )
    return provided


def _response(account: TradingAccount, settings: Settings | None = None) -> dict[str, object]:
    effective = account.risk_limits
    if settings is not None:
        effective = RiskLimits.from_settings(settings).tightened(account.risk_limits).account_view()
    configured = bool(account.api_key_ref and account.api_secret_ref and account.passphrase_ref)
    return {
        "id": account.id,
        "provider": account.provider,
        "environment": account.environment,
        "enabled": account.enabled,
        "configured": configured,
        #: 返回给账户主人自己，前端默认脱敏展示、点击后展开。这是排查
        #: 凭证填错（例如粘上 WEEX_API_SECRET= 前缀）的唯一入口。
        "credentials": (
            {
                "api_key": account.api_key_ref,
                "api_secret": account.api_secret_ref,
                "passphrase": account.passphrase_ref,
            }
            if configured
            else None
        ),
        "risk_limits": account.risk_limits,
        "effective_risk_limits": effective,
        "platform_limits": _platform_limits(settings) if settings else None,
    }


@router.get("")
def list_accounts(
    user=Depends(get_current_user),
    service: TradingCycleService = Depends(get_cycle_service),
) -> dict[str, list[dict[str, object]]]:
    db = service.db
    if db is None:
        return {"items": []}
    accounts = db.scalars(select(TradingAccount).where(TradingAccount.user_id == user.id)).all()
    return {"items": [_response(account, service.settings) for account in accounts]}


@router.post("", status_code=status.HTTP_201_CREATED)
def create_account(
    payload: AccountCreate,
    user=Depends(get_current_user),
    service: TradingCycleService = Depends(get_cycle_service),
) -> dict[str, object]:
    if service.db is None:
        raise HTTPException(status_code=503, detail="Database is unavailable")
    db = service.db
    account = TradingAccount(
        id=str(uuid4()),
        user_id=user.id,
        provider=payload.provider,
        environment=payload.environment,
        api_key_ref=payload.api_key_ref,
        api_secret_ref=payload.api_secret_ref,
        passphrase_ref=payload.passphrase_ref,
        enabled=False,
    )
    db.add(account)
    db.commit()
    db.refresh(account)
    return _response(account, service.settings)


@router.patch("/{account_id}")
def update_account(
    account_id: str,
    payload: AccountUpdate,
    user=Depends(get_current_user),
    service: TradingCycleService = Depends(get_cycle_service),
) -> dict[str, object]:
    if service.db is None:
        raise HTTPException(status_code=503, detail="Database is unavailable")
    db = service.db
    account = db.scalar(
        select(TradingAccount).where(
            TradingAccount.id == account_id,
            TradingAccount.user_id == user.id,
        )
    )
    if account is None:
        raise HTTPException(status_code=404, detail="Trading account not found")
    if payload.risk_limits is not None:
        # 整体替换：前端每次提交完整的一份偏好，避免残留旧键。
        account.risk_limits = _validate_risk_limits(payload.risk_limits, service.settings) or None
    account.enabled = payload.enabled
    db.commit()
    db.refresh(account)
    return _response(account, service.settings)
