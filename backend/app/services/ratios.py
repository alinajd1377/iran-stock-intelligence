"""Point-in-time financial ratio calculations."""
from datetime import date
from decimal import Decimal
from sqlalchemy import select
from sqlalchemy.orm import Session
from .. import models

def calculate_ratios(db: Session, symbol: str, market_cap: Decimal | None = None, as_of: date | None = None) -> dict:
    as_of = as_of or date.today()
    rows = db.execute(
        select(models.FinancialFact, models.CodalAnnouncement)
        .join(models.CodalAnnouncement, models.FinancialFact.announcement_id == models.CodalAnnouncement.id)
        .where(models.CodalAnnouncement.symbol_ticker == symbol,
               models.FinancialFact.period_end <= as_of,
               models.CodalAnnouncement.publish_at.is_not(None),
               models.CodalAnnouncement.publish_at <= as_of)
        .order_by(models.FinancialFact.period_end.desc(), models.CodalAnnouncement.publish_at.desc())
    ).all()
    latest = {}
    for fact, announcement in rows:
        latest.setdefault(fact.concept, Decimal(fact.value))
    snapshot = db.scalar(select(models.MarketSnapshot).where(
        models.MarketSnapshot.symbol_ticker == symbol, models.MarketSnapshot.as_of <= as_of
    ).order_by(models.MarketSnapshot.as_of.desc()).limit(1))
    cap = market_cap or (Decimal(snapshot.market_cap) if snapshot and snapshot.market_cap else None)
    equity, revenue, net_income = latest.get("equity"), latest.get("revenue"), latest.get("net_income")
    return {
        "symbol": symbol, "as_of": as_of.isoformat(),
        "market_cap": str(cap) if cap is not None else None,
        "net_income_latest": str(net_income) if net_income is not None else None,
        "revenue_latest": str(revenue) if revenue is not None else None,
        "equity_latest": str(equity) if equity is not None else None,
        "pe": str(cap / net_income) if cap is not None and net_income and net_income > 0 else None,
        "pb": str(cap / equity) if cap is not None and equity and equity > 0 else None,
        "ps": str(cap / revenue) if cap is not None and revenue and revenue > 0 else None,
        "methodology": "Latest published fact per concept; not TTM unless facts have been normalized to TTM. Null means insufficient data."
    }
