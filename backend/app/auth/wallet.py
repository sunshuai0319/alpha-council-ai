from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import WalletAddress, WalletChallenge


def build_wallet_message(address: str, nonce: str) -> str:
    return f"Alpha Council AI wallet verification\nAddress: {address}\nNonce: {nonce}"


def verify_evm_signature(message: str, signature: str, expected_address: str) -> bool:
    try:
        from eth_account import Account
        from eth_account.messages import encode_defunct

        recovered = Account.recover_message(encode_defunct(text=message), signature=signature)
        return recovered.lower() == expected_address.lower()
    except (ImportError, TypeError, ValueError):
        return False


class WalletChallengeService:
    def __init__(self, db: Session, ttl: timedelta = timedelta(minutes=10)) -> None:
        self.db = db
        self.ttl = ttl

    def create(
        self,
        user_id: str,
        address: str,
        chain: str,
        *,
        expires_at: datetime | None = None,
    ) -> WalletChallenge:
        normalized = address.strip().lower()
        nonce = uuid4().hex
        challenge = WalletChallenge(
            id=str(uuid4()),
            user_id=user_id,
            address=normalized,
            chain=chain.upper(),
            nonce=nonce,
            message=build_wallet_message(normalized, nonce),
            expires_at=expires_at or datetime.now(UTC) + self.ttl,
        )
        self.db.add(challenge)
        self.db.commit()
        self.db.refresh(challenge)
        return challenge

    def verify(
        self,
        *,
        user_id: str,
        challenge_id: str,
        signature: str,
        verifier: Callable[[str, str], bool],
    ) -> WalletAddress | None:
        challenge = self.db.scalar(
            select(WalletChallenge).where(
                WalletChallenge.id == challenge_id,
                WalletChallenge.user_id == user_id,
            )
        )
        if challenge is None:
            return None
        expires_at = challenge.expires_at
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=UTC)
        if (
            challenge.used_at is not None
            or expires_at <= datetime.now(UTC)
            or not verifier(challenge.message, signature)
        ):
            return None
        challenge.used_at = datetime.now(UTC)
        wallet = self.db.scalar(
            select(WalletAddress).where(
                WalletAddress.user_id == user_id,
                WalletAddress.address == challenge.address,
                WalletAddress.chain == challenge.chain,
            )
        )
        if wallet is None:
            wallet = WalletAddress(
                id=str(uuid4()),
                user_id=user_id,
                address=challenge.address,
                chain=challenge.chain,
            )
            self.db.add(wallet)
        wallet.verified_at = datetime.now(UTC)
        wallet.status = "active"
        self.db.commit()
        self.db.refresh(wallet)
        return wallet
