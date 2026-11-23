from contextlib import asynccontextmanager
from datetime import date
from decimal import Decimal
from pathlib import Path
from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field
from sqlalchemy import select, text
from sqlalchemy.orm import Session, selectinload
from .config import settings
from .db import Base, engine, get_db
from . import models
from .services.codal import CodalClient
from .services.tsetmc import TsetmcClient
from .services.storage import download_document
from .services.financial_parser import parse_document
from .services.ratios import calculate_ratios
from .services.scenario import project

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Local development bootstrap. Production deployments should use Alembic.
    Base.metadata.create_all(bind=engine)
    with Session(engine) as db:
        if not db.scalar(select(models.Symbol).where(models.Symbol.ticker == "ساروم")):
            db.add(models.Symbol(ticker="ساروم", name="سیمان ارومیه"))
            db.commit()
    yield

app = FastAPI(title=settings.app_name, version="0.3.0", description="Iranian equity research platform", lifespan=lifespan)

class MarketSnapshotIn(BaseModel):
    symbol_ticker: str
    as_of: date
    close_price: Decimal | None = None
    shares_outstanding: int | None = Field(default=None, gt=0)
    market_cap: Decimal | None = None
    source: str = "manual"

class MonthlySaleIn(BaseModel):
    symbol_ticker: str = "ساروم"
    period_year: int = Field(ge=1300, le=1500)
    period_month: int = Field(ge=1, le=12)
    product: str
    quantity: Decimal | None = None
    sales_amount: Decimal | None = None
    unit: str = "IRR"
    source_announcement_id: int | None = None

class PortfolioIn(BaseModel):
    name: str
    base_currency: str = "IRR"

class HoldingIn(BaseModel):
    symbol_ticker: str
    quantity: Decimal = Field(gt=0)
    average_cost: Decimal | None = Field(default=None, gt=0)

class ScenarioIn(BaseModel):
    symbol_ticker: str
    metric: str
    base_value: float
    annual_growth: float = Field(gt=-1)
    periods: int = Field(default=3, ge=1, le=10)

@app.get("/dashboard", response_class=HTMLResponse)
def dashboard():
    return (Path(__file__).parent / "dashboard.html").read_text(encoding="utf-8")

@app.get("/")
def root():
    return {"name": settings.app_name, "version": "0.3.0", "docs": "/docs"}

@app.get("/health")
def health():
    return {"status": "ok", "environment": settings.app_env}

@app.get("/health/db")
def database_health(db: Session = Depends(get_db)):
    db.execute(text("SELECT 1"))
    return {"database": "ok"}

@app.get("/api/v1/symbols")
def list_symbols(db: Session = Depends(get_db)):
    return db.scalars(select(models.Symbol).order_by(models.Symbol.ticker)).all()

@app.get("/api/v1/announcements")
def list_announcements(symbol: str = Query(default="ساروم"), limit: int = Query(default=50, ge=1, le=200), db: Session = Depends(get_db)):
    stmt = select(models.CodalAnnouncement).where(models.CodalAnnouncement.symbol_ticker == symbol).order_by(models.CodalAnnouncement.publish_at.desc().nullslast()).limit(limit)
    return db.scalars(stmt).all()

@app.post("/api/v1/sync/codal/{symbol}")
async def sync_codal(symbol: str, pages: int = Query(default=1, ge=1, le=10), db: Session = Depends(get_db)):
    client = CodalClient()
    inserted = seen = 0
    try:
        for page in range(1, pages + 1):
            payload = await client.search(symbol=symbol, page=page)
            rows = payload.get("Letters") or payload.get("letters") or payload.get("Data") or payload.get("data") or []
            if isinstance(rows, dict):
                rows = rows.get("Letters") or rows.get("letters") or []
            if not isinstance(rows, list):
                raise ValueError("ساختار پاسخ کدال با قالب مورد انتظار سازگار نیست")
            if not rows: break
            for row in rows:
                if not isinstance(row, dict): continue
                tracing = row.get("TracingNo") or row.get("tracingNo") or row.get("tracing_no")
                if not tracing: continue
                seen += 1
                exists = db.scalar(select(models.CodalAnnouncement).where(models.CodalAnnouncement.tracing_no == str(tracing)))
                if exists: continue
                title = row.get("Title") or row.get("title") or row.get("LetterCodeTitle") or "بدون عنوان"
                url = row.get("Url") or row.get("url") or row.get("ExcelUrl") or row.get("PdfUrl")
                db.add(models.CodalAnnouncement(tracing_no=str(tracing), symbol_ticker=symbol, title=str(title), source_url=str(url) if url else None, raw_payload=row))
                inserted += 1
            db.commit()
    except Exception as exc:
        db.rollback()
        raise HTTPException(status_code=502, detail=f"دریافت کدال ناموفق بود: {type(exc).__name__}: {exc}") from exc
    return {"symbol": symbol, "rows_seen": seen, "inserted": inserted, "warning": "قرارداد API و نگاشت تاریخ/نوع گزارش هنوز نیازمند اعتبارسنجی زنده است."}

@app.post("/api/v1/announcements/{announcement_id}/download")
async def download_announcement(announcement_id: int, period_end: date | None = None, db: Session = Depends(get_db)):
    announcement = db.get(models.CodalAnnouncement, announcement_id)
    if not announcement: raise HTTPException(404, "اطلاعیه پیدا نشد")
    if not announcement.source_url: raise HTTPException(422, "برای این اطلاعیه لینک فایل موجود نیست")
    try:
        stored = await download_document(announcement.source_url)
        existing = db.scalar(select(models.DocumentVersion).where(models.DocumentVersion.announcement_id == announcement_id, models.DocumentVersion.file_hash == stored["sha256"]))
        if existing:
            return {"status": "already_downloaded", "version_id": existing.id, "sha256": stored["sha256"]}
        version_no = (db.scalar(select(models.DocumentVersion.version_no).where(models.DocumentVersion.announcement_id == announcement_id).order_by(models.DocumentVersion.version_no.desc()).limit(1)) or 0) + 1
        version = models.DocumentVersion(announcement_id=announcement_id, version_no=version_no, file_hash=stored["sha256"], file_name=Path(stored["path"]).name, storage_path=stored["path"], content_type=stored["content_type"])
        db.add(version)
        db.flush()
        facts = parse_document(stored["path"])
        if period_end:
            for fact in facts:
                db.add(models.FinancialFact(announcement_id=announcement_id, concept=fact["concept"], period_end=period_end, value=fact["value"], unit="IRR", currency="IRR", source_label=fact.get("source_label")))
        db.commit()
        return {"status": "downloaded", "version_id": version.id, "version_no": version_no, "sha256": stored["sha256"], "bytes": stored["size"], "parsed_facts": len(facts) if period_end else 0, "note": "Parser عمومی و محافظه‌کار است؛ برای هر قالب کدال باید قواعد اختصاصی و بازبینی انسانی اضافه شود."}
    except Exception as exc:
        db.rollback()
        raise HTTPException(502, f"دانلود یا پردازش فایل ناموفق بود: {type(exc).__name__}: {exc}") from exc

@app.get("/api/v1/analytics/{symbol}/ratios")
def symbol_ratios(symbol: str, as_of: date | None = None, market_cap: Decimal | None = Query(default=None, gt=0), db: Session = Depends(get_db)):
    return calculate_ratios(db, symbol, market_cap=market_cap, as_of=as_of)

@app.post("/api/v1/market/snapshots")
def save_market_snapshot(payload: MarketSnapshotIn, db: Session = Depends(get_db)):
    row = db.scalar(select(models.MarketSnapshot).where(models.MarketSnapshot.symbol_ticker == payload.symbol_ticker, models.MarketSnapshot.as_of == payload.as_of, models.MarketSnapshot.source == payload.source))
    if row is None:
        row = models.MarketSnapshot(**payload.model_dump())
        db.add(row)
    else:
        for key, value in payload.model_dump().items(): setattr(row, key, value)
    db.commit()
    db.refresh(row)
    return row

@app.get("/api/v1/market/snapshots/{symbol}")
def market_history(symbol: str, limit: int = Query(default=100, ge=1, le=1000), db: Session = Depends(get_db)):
    return db.scalars(select(models.MarketSnapshot).where(models.MarketSnapshot.symbol_ticker == symbol).order_by(models.MarketSnapshot.as_of.desc()).limit(limit)).all()

@app.get("/api/v1/monthly-sales")
def monthly_sales(symbol: str = "ساروم", year: int | None = None, db: Session = Depends(get_db)):
    stmt = select(models.MonthlySale).where(models.MonthlySale.symbol_ticker == symbol)
    if year: stmt = stmt.where(models.MonthlySale.period_year == year)
    return db.scalars(stmt.order_by(models.MonthlySale.period_year, models.MonthlySale.period_month, models.MonthlySale.product)).all()

@app.post("/api/v1/monthly-sales")
def save_monthly_sale(payload: MonthlySaleIn, db: Session = Depends(get_db)):
    row = db.scalar(select(models.MonthlySale).where(models.MonthlySale.symbol_ticker == payload.symbol_ticker, models.MonthlySale.period_year == payload.period_year, models.MonthlySale.period_month == payload.period_month, models.MonthlySale.product == payload.product))
    if row is None:
        row = models.MonthlySale(**payload.model_dump())
        db.add(row)
    else:
        for key, value in payload.model_dump().items(): setattr(row, key, value)
    db.commit()
    db.refresh(row)
    return row

@app.post("/api/v1/portfolios")
def create_portfolio(payload: PortfolioIn, db: Session = Depends(get_db)):
    row = models.Portfolio(**payload.model_dump())
    db.add(row)
    db.commit()
    db.refresh(row)
    return row

@app.get("/api/v1/portfolios")
def list_portfolios(db: Session = Depends(get_db)):
    return db.scalars(select(models.Portfolio).options(selectinload(models.Portfolio.holdings)).order_by(models.Portfolio.id)).all()

@app.post("/api/v1/portfolios/{portfolio_id}/holdings")
def add_holding(portfolio_id: int, payload: HoldingIn, db: Session = Depends(get_db)):
    portfolio = db.get(models.Portfolio, portfolio_id)
    if not portfolio: raise HTTPException(404, "سبد پیدا نشد")
    row = db.scalar(select(models.PortfolioHolding).where(models.PortfolioHolding.portfolio_id == portfolio_id, models.PortfolioHolding.symbol_ticker == payload.symbol_ticker))
    if row is None:
        row = models.PortfolioHolding(portfolio_id=portfolio_id, **payload.model_dump())
        db.add(row)
    else:
        row.quantity, row.average_cost = payload.quantity, payload.average_cost
    db.commit()
    db.refresh(row)
    return row

@app.get("/api/v1/portfolios/{portfolio_id}/analysis")
def analyze_portfolio(portfolio_id: int, db: Session = Depends(get_db)):
    portfolio = db.scalar(select(models.Portfolio).options(selectinload(models.Portfolio.holdings)).where(models.Portfolio.id == portfolio_id))
    if not portfolio: raise HTTPException(404, "سبد پیدا نشد")
    total_cost = sum((h.quantity * h.average_cost for h in portfolio.holdings if h.average_cost is not None), Decimal(0))
    total_qty = sum((h.quantity for h in portfolio.holdings), Decimal(0))
    holdings = []
    for h in portfolio.holdings:
        weight = (h.quantity * h.average_cost / total_cost) if h.average_cost is not None and total_cost else None
        holdings.append({"symbol": h.symbol_ticker, "quantity": str(h.quantity), "average_cost": str(h.average_cost) if h.average_cost is not None else None, "cost_weight": str(weight) if weight is not None else None})
    known = [Decimal(x["cost_weight"]) for x in holdings if x["cost_weight"] is not None]
    hhi = sum((w*w for w in known), Decimal(0)) if known else None
    return {"portfolio_id": portfolio_id, "name": portfolio.name, "positions": holdings, "total_cost_basis": str(total_cost), "position_count": len(holdings), "cost_weight_hhi": str(hhi) if hhi is not None else None, "warning": "وزن‌ها بر مبنای بهای تمام‌شده هستند؛ تا اتصال قیمت زنده، بازده و ریسک بازار محاسبه نمی‌شود."}

@app.post("/api/v1/scenarios/project")
def create_projection(payload: ScenarioIn, db: Session = Depends(get_db)):
    try: result = project(payload.base_value, payload.annual_growth, payload.periods)
    except ValueError as exc: raise HTTPException(422, str(exc))
    row = models.ForecastScenario(symbol_ticker=payload.symbol_ticker, metric=payload.metric, base_value=payload.base_value, annual_growth=payload.annual_growth, periods=payload.periods)
    db.add(row)
    db.commit()
    result.update({"scenario_id": row.id, "symbol": payload.symbol_ticker, "metric": payload.metric})
    return result

@app.get("/api/v1/subscription-plans")
def subscription_plans():
    return [
        {"code": "fundamental", "name": "Fundamental", "durations_months": [3, 6, 12], "features": ["صورت‌های مالی", "نسبت‌های بنیادی"]},
        {"code": "fundamental_plus", "name": "Fundamental Plus", "durations_months": [3, 6, 12], "features": ["همه امکانات Fundamental", "گزارش‌های ماهانه و صنعت"]},
        {"code": "fundamental_pro", "name": "Fundamental Pro", "durations_months": [3, 6, 12], "features": ["همه امکانات Plus", "سناریوهای پیش‌بینی و داده‌های پیشرفته"]},
        {"code": "portfolio_intelligence", "name": "Portfolio Intelligence", "durations_months": [3, 6, 12], "features": ["تحلیل سبد", "تمرکز و پایش ریسک"]},
    ]

@app.get("/api/v1/tsetmc/search")
async def search_tsetmc(q: str = Query(min_length=2)):
    try:
        return await TsetmcClient().instrument_search(q)
    except Exception as exc:
        raise HTTPException(502, f"جست‌وجوی TSETMC ناموفق بود: {type(exc).__name__}: {exc}") from exc
