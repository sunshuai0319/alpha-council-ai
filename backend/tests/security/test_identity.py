from datetime import UTC, datetime, timedelta

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.auth.wallet import WalletChallengeService, build_wallet_message
from app.auth.webhooks import process_clerk_event
from app.db.models import Base, ClerkWebhookEvent, User, WalletAddress


def test_clerk_user_event_is_idempotent_and_updates_local_user(tmp_path) -> None:
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'identity.db'}")
    Base.metadata.create_all(engine)
    event = {
        "id": "evt_user_1",
        "type": "user.created",
        "data": {
            "id": "user_clerk_1",
            "email_addresses": [{"id": "email_1", "email_address": "alice@example.com"}],
            "primary_email_address_id": "email_1",
            "first_name": "Alice",
            "last_name": "Trader",
        },
    }
    with Session(engine) as db:
        assert process_clerk_event(db, event) is True
        assert process_clerk_event(db, event) is False
        user = db.scalar(select(User).where(User.clerk_user_id == "user_clerk_1"))
        assert user is not None
        assert user.email == "alice@example.com"
        assert user.display_name == "Alice Trader"
        assert db.scalar(select(ClerkWebhookEvent).where(ClerkWebhookEvent.id == "evt_user_1")) is not None


def test_wallet_challenge_is_single_use_and_binds_verified_address(tmp_path) -> None:
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'wallet.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        user = User(id="user-wallet", clerk_user_id="clerk-wallet")
        db.add(user)
        db.commit()
        service = WalletChallengeService(db)
        challenge = service.create("user-wallet", "0xABC", "EVM")
        assert challenge.message == build_wallet_message("0xabc", challenge.nonce)
        address = service.verify(
            user_id="user-wallet",
            challenge_id=challenge.id,
            signature="test-signature",
            verifier=lambda message, signature: signature == "test-signature",
        )
        assert address.address == "0xabc"
        assert address.verified_at is not None
        assert service.verify(
            user_id="user-wallet",
            challenge_id=challenge.id,
            signature="test-signature",
            verifier=lambda message, signature: True,
        ) is None
        assert db.scalar(select(WalletAddress).where(WalletAddress.user_id == "user-wallet")) is not None


def test_expired_wallet_challenge_is_rejected(tmp_path) -> None:
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'wallet-expired.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        user = User(id="user-wallet", clerk_user_id="clerk-wallet")
        db.add(user)
        db.commit()
        service = WalletChallengeService(db)
        challenge = service.create(
            "user-wallet",
            "0xabc",
            "EVM",
            expires_at=datetime.now(UTC) - timedelta(seconds=1),
        )
        assert service.verify(
            user_id="user-wallet",
            challenge_id=challenge.id,
            signature="test-signature",
            verifier=lambda message, signature: True,
        ) is None
