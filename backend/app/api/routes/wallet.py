from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.api.dependencies import get_cycle_service
from app.auth.dependencies import get_current_user
from app.auth.wallet import WalletChallengeService, verify_evm_signature
from app.db.models import WalletChallenge
from app.services.cycle import TradingCycleService

router = APIRouter(prefix="/api/wallet", tags=["wallet"])


class ChallengeRequest(BaseModel):
    address: str
    chain: str = "EVM"


class VerifyRequest(BaseModel):
    signature: str


@router.post("/challenge")
def create_challenge(
    payload: ChallengeRequest,
    user=Depends(get_current_user),
    service: TradingCycleService = Depends(get_cycle_service),
):
    if service.db is None:
        raise HTTPException(status_code=503, detail="Database is unavailable")
    challenge = WalletChallengeService(service.db).create(user.id, payload.address, payload.chain)
    return {
        "id": challenge.id,
        "address": challenge.address,
        "chain": challenge.chain,
        "message": challenge.message,
        "expires_at": challenge.expires_at,
    }


@router.post("/challenge/{challenge_id}/verify")
def verify_challenge(
    challenge_id: str,
    payload: VerifyRequest,
    user=Depends(get_current_user),
    service: TradingCycleService = Depends(get_cycle_service),
):
    if service.db is None:
        raise HTTPException(status_code=503, detail="Database is unavailable")
    challenge_service = WalletChallengeService(service.db)
    challenge = service.db.get(WalletChallenge, challenge_id)
    if challenge is None or challenge.user_id != user.id:
        raise HTTPException(status_code=404, detail="Wallet challenge not found")
    wallet = challenge_service.verify(
        user_id=user.id,
        challenge_id=challenge_id,
        signature=payload.signature,
        verifier=lambda message, signature: verify_evm_signature(message, signature, challenge.address),
    )
    if wallet is None:
        raise HTTPException(status_code=400, detail="Invalid or expired wallet signature")
    return {"id": wallet.id, "address": wallet.address, "chain": wallet.chain, "verified": True}
