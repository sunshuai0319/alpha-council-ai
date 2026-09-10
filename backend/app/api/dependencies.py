from fastapi import Depends
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.services.cycle import TradingCycleService


def get_cycle_service(db: Session = Depends(get_db)) -> TradingCycleService:
    return TradingCycleService(db=db)
