from typing import Literal
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select

from app.api.dependencies import get_cycle_service
from app.auth.dependencies import get_current_user
from app.db.models import TradingAccount
from app.services.cycle import TradingCycleService

router = APIRouter(prefix="/api/accounts", tags=["accounts"])


class AccountCreate(BaseModel):
    api_key_ref: str = Field(min_length=1)
    api_secret_ref: str = Field(min_length=1)
    passphrase_ref: str = Field(min_length=1)
    environment: Literal["virtual"] = "virtual"
    provider: Literal["weex"] = "weex"

    @field_validator("api_key_ref", "api_secret_ref", "passphrase_ref")
    @classmethod
    def reject_environment_references(cls, value: str) -> str:
        if value.startswith("env:"):
            raise ValueError("environment references are not supported")
        return value


class AccountUpdate(BaseModel):
    enabled: bool


def _response(account: TradingAccount) -> dict[str, object]:
    return {
        "id": account.id,
        "provider": account.provider,
        "environment": account.environment,
        "enabled": account.enabled,
        "configured": bool(account.api_key_ref and account.api_secret_ref and account.passphrase_ref),
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
    return {"items": [_response(account) for account in accounts]}


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
    return _response(account)


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
    account.enabled = payload.enabled
    db.commit()
    db.refresh(account)
    return _response(account)
