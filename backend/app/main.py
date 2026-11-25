from contextlib import asynccontextmanager
from datetime import date
from decimal import Decimal
from pathlib import Path
from urllib.parse import parse_qs
import base64, hashlib, hmac, time
from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
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

app = FastAPI(title=settings.app_name, version="0.4.0", description="Iranian equity research platform", lifespan=lifespan)

ADMIN_COOKIE = "isi_admin_session"
LOGIN_ATTEMPTS: dict[str, list[float]] = {}

def _admin_session_token(username: str, expires_at: int) -> str:
    payload = f"{username}:{expires_at}".encode()
    signature = hmac.new(settings.admin_session_secret.encode(), payload, hashlib.sha256).digest()
    part = lambda value: base64.urlsafe_b64encode(value).decode().rstrip("=")
    return part(payload) + "." + part(signature)

def _valid_admin_session(token: str | None) -> bool:
    if not token or not settings.admin_username or not settings.admin_password or not settings.admin_session_secret: return False
    try:
        payload_part, signature_part = token.split(".", 1)
        decode = lambda value: base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
        payload, signature = decode(payload_part), decode(signature_part)
        username, expiry = payload.rsplit(b":", 1)
        expected = hmac.new(settings.admin_session_secret.encode(), payload, hashlib.sha256).digest()
        return hmac.compare_digest(signature, expected) and hmac.compare_digest(username.decode(), settings.admin_username) and int(expiry) > int(time.time())
    except (ValueError, UnicodeDecodeError): return False

@app.middleware("http")
async def protect_management_routes(request: Request, call_next):
    path = request.url.path
    is_login = path in {"/admin/login", "/admin/logout"}
    protected = path == "/admin" or path.startswith("/api/v1/admin/") or request.method in {"POST", "PUT", "PATCH", "DELETE"}
    if protected and not is_login and not _valid_admin_session(request.cookies.get(ADMIN_COOKIE)):
        if path.startswith("/api/") or path.startswith("/docs") or path.startswith("/openapi"): return JSONResponse({"detail": "احراز هویت مدیر لازم است"}, status_code=401)
        return RedirectResponse("/admin/login", status_code=303)
    if protected and request.method in {"POST", "PUT", "PATCH", "DELETE"} and not is_login:
        origin = request.headers.get("origin")
        if origin and origin.rstrip("/") != str(request.base_url).rstrip("/"): return JSONResponse({"detail": "درخواست cross-origin مجاز نیست"}, status_code=403)
    return await call_next(request)

@app.get("/admin/login", response_class=HTMLResponse)
def admin_login_page():
    if not settings.admin_username or not settings.admin_password or not settings.admin_session_secret:
        return HTMLResponse("<h2>ورود مدیر پیکربندی نشده است</h2><p>ADMIN_USERNAME، ADMIN_PASSWORD و ADMIN_SESSION_SECRET را در .env تنظیم کنید.</p>", status_code=503)
    return HTMLResponse("""<!doctype html><html lang="fa" dir="rtl"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>ورود مدیر</title><style>body{font-family:Tahoma;background:#f3f6fa;display:grid;place-items:center;min-height:100vh;margin:0}.box{background:white;padding:28px;border:1px solid #dce4ee;border-radius:14px;width:min(360px,88vw)}input,button{box-sizing:border-box;width:100%;padding:12px;margin:8px 0;border:1px solid #ccd7e4;border-radius:8px}button{background:#1769c2;color:white;border:0;cursor:pointer}</style><div class="box"><h2>ورود به پنل مدیریت</h2><form method="post"><label>نام کاربری</label><input name="username" autocomplete="username" required><label>گذرواژه</label><input name="password" type="password" autocomplete="current-password" required><button>ورود امن</button></form></div></html>""")

@app.post("/admin/login")
async def admin_login(request: Request):
    if not settings.admin_username or not settings.admin_password or not settings.admin_session_secret: raise HTTPException(503, "تنظیمات ورود مدیر کامل نیست")
    client = request.client.host if request.client else "unknown"
    now = time.time()
    attempts = [t for t in LOGIN_ATTEMPTS.get(client, []) if now - t < 900]
    if len(attempts) >= 8: raise HTTPException(429, "تعداد تلاش ناموفق زیاد است؛ ۱۵ دقیقه بعد تلاش کنید")
    form = parse_qs((await request.body()).decode("utf-8", "replace"))
    username, password = form.get("username", [""])[0], form.get("password", [""])[0]
    ok = hmac.compare_digest(username, settings.admin_username) and hmac.compare_digest(password, settings.admin_password)
    if not ok:
        attempts.append(now); LOGIN_ATTEMPTS[client] = attempts
        raise HTTPException(401, "نام کاربری یا گذرواژه نادرست است")
    LOGIN_ATTEMPTS.pop(client, None)
    response = RedirectResponse("/admin", status_code=303)
    response.set_cookie(ADMIN_COOKIE, _admin_session_token(username, int(now) + settings.admin_session_ttl_seconds), max_age=settings.admin_session_ttl_seconds, httponly=True, secure=settings.admin_cookie_secure, samesite="strict", path="/")
    return response

@app.post("/admin/logout")
def admin_logout():
    response = RedirectResponse("/admin/login", status_code=303)
    response.delete_cookie(ADMIN_COOKIE, path="/", httponly=True, secure=settings.admin_cookie_secure, samesite="strict")
    return response

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
    return {"name": settings.app_name, "version": "0.4.0", "docs": "/docs"}

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


class SymbolIn(BaseModel):
    ticker: str = Field(min_length=1, max_length=32)
    name: str = Field(min_length=1, max_length=255)

@app.post("/api/v1/symbols")
def add_symbol(payload: SymbolIn, db: Session = Depends(get_db)):
    existing = db.scalar(select(models.Symbol).where(models.Symbol.ticker == payload.ticker))
    if existing:
        raise HTTPException(409, "این نماد قبلاً ثبت شده است")
    row = models.Symbol(**payload.model_dump())
    db.add(row); db.commit(); db.refresh(row)
    return row

@app.get("/api/v1/codal/archive/items")
def codal_archive_items(announcement_id: int, limit: int = Query(default=500, ge=1, le=5000),
                        offset: int = Query(default=0, ge=0), q: str | None = Query(default=None, max_length=200),
                        db: Session = Depends(get_db)):
    stmt = select(models.DocumentContentItem).join(models.DocumentVersion).where(
        models.DocumentVersion.announcement_id == announcement_id)
    if q: stmt = stmt.where(models.DocumentContentItem.content_text.ilike(f"%{q}%"))
    rows = db.scalars(stmt.order_by(models.DocumentContentItem.id).offset(offset).limit(limit)).all()
    return [{"id": x.id, "locator": x.locator, "kind": x.content_kind, "text": x.content_text,
             "document_version_id": x.document_version_id} for x in rows]

@app.get("/api/v1/codal/reports/{announcement_id}/cells")
def codal_report_cells(announcement_id: int, limit: int = Query(default=500, ge=1, le=2000),
                       offset: int = Query(default=0, ge=0), q: str | None = Query(default=None, max_length=200),
                       db: Session = Depends(get_db)):
    if db.get(models.CodalAnnouncement, announcement_id) is None: raise HTTPException(404, "گزارش پیدا نشد")
    doc_stmt = select(models.DocumentContentItem, models.DocumentVersion).join(
        models.DocumentVersion, models.DocumentContentItem.document_version_id == models.DocumentVersion.id
    ).where(models.DocumentVersion.announcement_id == announcement_id)
    html_stmt = select(models.CodalReportCell).where(models.CodalReportCell.announcement_id == announcement_id)
    if q:
        pattern = f"%{q}%"
        doc_stmt = doc_stmt.where(models.DocumentContentItem.content_text.ilike(pattern))
        html_stmt = html_stmt.where(models.CodalReportCell.cell_text.ilike(pattern))
    doc_rows = db.execute(doc_stmt.order_by(models.DocumentVersion.version_no, models.DocumentContentItem.id)).all()
    html_rows = db.scalars(html_stmt.order_by(models.CodalReportCell.sheet_id, models.CodalReportCell.table_index, models.CodalReportCell.row_no, models.CodalReportCell.column_no)).all()
    items = [{"locator": item.locator, "kind": item.content_kind, "text": item.content_text,
              "file_name": version.file_name, "version_no": version.version_no} for item, version in doc_rows]
    items.extend([{"locator": item.locator, "kind": item.content_kind, "text": item.cell_text,
                   "file_name": "Codal Decision HTML", "version_no": None} for item in html_rows])
    items.sort(key=lambda item: (item["locator"], item["file_name"] or ""))
    total = len(items)
    return {"announcement_id": announcement_id, "total": total, "limit": limit, "offset": offset,
            "items": items[offset:offset + limit]}

@app.get("/api/v1/research/sarom")
def sarom_research():
    """Return the curated source-linked Sarom research snapshot."""
    import json
    path = Path(__file__).parent / "data" / "sarom_snapshot.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise HTTPException(500, f"Sarom research data unavailable: {exc}") from exc


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


@app.get("/api/v1/codal/archive/status")
def codal_archive_status(symbol: str = Query(default="ساروم"), db: Session = Depends(get_db)):
    """Return persisted archive coverage counts for a ticker."""
    announcements = db.execute(text("SELECT count(*) FROM codal_announcements WHERE symbol_ticker=:s"), {"s": symbol}).scalar_one()
    versions = db.execute(text("SELECT count(*) FROM document_versions v JOIN codal_announcements a ON a.id=v.announcement_id WHERE a.symbol_ticker=:s"), {"s": symbol}).scalar_one()
    facts = db.execute(text("SELECT count(*) FROM financial_facts f JOIN codal_announcements a ON a.id=f.announcement_id WHERE a.symbol_ticker=:s"), {"s": symbol}).scalar_one()
    latest = db.execute(text("SELECT max(fetched_at) FROM codal_announcements WHERE symbol_ticker=:s"), {"s": symbol}).scalar_one()
    report_tables = db.execute(text("SELECT count(*) FROM codal_report_tables t JOIN codal_report_sheets s ON s.id=t.sheet_record_id JOIN codal_announcements a ON a.id=s.announcement_id WHERE a.symbol_ticker=:s"), {"s": symbol}).scalar_one()
    report_sheets = db.execute(text("SELECT count(*) FROM codal_report_sheets s JOIN codal_announcements a ON a.id=s.announcement_id WHERE a.symbol_ticker=:s"), {"s": symbol}).scalar_one()
    content_items = db.execute(text("SELECT count(*) FROM document_content_items i JOIN document_versions v ON v.id=i.document_version_id JOIN codal_announcements a ON a.id=v.announcement_id WHERE a.symbol_ticker=:s"), {"s": symbol}).scalar_one()
    html_cells = db.execute(text("SELECT count(*) FROM codal_report_cells c JOIN codal_announcements a ON a.id=c.announcement_id WHERE a.symbol_ticker=:s"), {"s": symbol}).scalar_one()
    content_items += html_cells
    extracted_versions = db.execute(text("SELECT count(DISTINCT v.id) FROM document_versions v JOIN codal_announcements a ON a.id=v.announcement_id JOIN document_content_items i ON i.document_version_id=v.id WHERE a.symbol_ticker=:s"), {"s": symbol}).scalar_one()
    cell_kinds = db.execute(text("SELECT i.content_kind, count(*) FROM document_content_items i JOIN document_versions v ON v.id=i.document_version_id JOIN codal_announcements a ON a.id=v.announcement_id WHERE a.symbol_ticker=:s GROUP BY i.content_kind"), {"s": symbol}).all()
    coverage = round((extracted_versions / versions) * 100, 1) if versions else 0
    return {"symbol": symbol, "announcements": announcements, "document_versions": versions,
            "financial_facts": facts, "report_sheets": report_sheets, "report_tables": report_tables,
            "content_items": content_items, "extracted_versions": extracted_versions,
            "extraction_coverage_percent": coverage, "content_kinds": {kind: count for kind, count in cell_kinds},
            "latest_fetched_at": latest, "worker": "docker compose logs codal-archive",
            "note": "محتوای سلول‌ها و متن با locator و نسخه فایل ذخیره می‌شود؛ نتایج OCR باید با سند اصلی بازبینی شوند."}


@app.get("/api/v1/codal/archive/announcements")
def codal_archive_announcements(symbol: str = Query(default="ساروم"), limit: int = Query(default=100, ge=1, le=500), db: Session = Depends(get_db)):
    rows = db.scalars(select(models.CodalAnnouncement).where(models.CodalAnnouncement.symbol_ticker == symbol)
                      .order_by(models.CodalAnnouncement.publish_at.desc().nullslast(), models.CodalAnnouncement.id.desc())
                      .limit(limit)).all()
    return [{"id": x.id, "tracing_no": x.tracing_no, "title": x.title, "publish_at": x.publish_at,
             "period_end": x.period_end, "report_type": x.report_type, "is_audited": x.is_audited,
             "source_url": x.source_url, "versions": len(x.versions)} for x in rows]

@app.post("/api/v1/codal/archive/reprocess/{symbol}")
def reprocess_codal_archive(symbol: str, limit: int = Query(default=5000, ge=1, le=50000)):
    """Re-extract cell/text content from already downloaded raw files."""
    try:
        from .jobs.codal_archive import reprocess_symbol
        return reprocess_symbol(symbol, limit=limit)
    except Exception as exc:
        raise HTTPException(502, f"بازاستخراج محتوای آرشیو ناموفق بود: {type(exc).__name__}: {exc}") from exc


@app.post("/api/v1/codal/archive/sync/{symbol}")
def manual_codal_archive_sync(symbol: str, pages: int = Query(default=3, ge=1, le=1000)):
    """Run the same archive-and-download pipeline used by the background worker."""
    try:
        from .jobs.codal_archive import sync_symbol
        return sync_symbol(symbol, pages, True)
    except Exception as exc:
        raise HTTPException(502, f"آرشیو دستی کدال ناموفق بود: {type(exc).__name__}: {exc}") from exc



# Reporting and management surfaces (membership checkout intentionally not implemented).
@app.get("/admin", response_class=HTMLResponse)
def admin_panel():
    return (Path(__file__).parent / "admin.html").read_text(encoding="utf-8")

@app.get("/api/v1/admin/overview")
def admin_overview(db: Session = Depends(get_db)):
    return {
        "symbols": db.scalar(select(text("count(*)")).select_from(models.Symbol)) or 0,
        "announcements": db.scalar(select(text("count(*)")).select_from(models.CodalAnnouncement)) or 0,
        "document_versions": db.scalar(select(text("count(*)")).select_from(models.DocumentVersion)) or 0,
        "report_sheets": db.scalar(select(text("count(*)")).select_from(models.CodalReportSheet)) or 0,
        "report_tables": db.scalar(select(text("count(*)")).select_from(models.CodalReportTable)) or 0,
        "financial_facts": db.scalar(select(text("count(*)")).select_from(models.FinancialFact)) or 0,
        "fx_rates": db.scalar(select(text("count(*)")).select_from(models.DailyFxRate)) or 0,
    }

@app.get("/api/v1/reports/monthly")
def monthly_reports(symbol: str = Query(default="ساروم"), limit: int = Query(default=200, ge=1, le=1000), db: Session = Depends(get_db)):
    rows=db.scalars(select(models.CodalAnnouncement).where(models.CodalAnnouncement.symbol_ticker==symbol).order_by(models.CodalAnnouncement.period_end.desc().nullslast(),models.CodalAnnouncement.publish_at.desc().nullslast()).limit(limit)).all()
    return [{"id":x.id,"tracing_no":x.tracing_no,"title":x.title,"period_end":x.period_end,"publish_at":x.publish_at,"report_type":x.report_type,"is_audited":x.is_audited,"versions":len(x.versions)} for x in rows if any(k in ((x.title or '')+' '+(x.report_type or '')).lower() for k in ('ماهانه','فعالیت','monthly','month'))]

@app.get("/api/v1/reports/quarterly")
def quarterly_reports(symbol: str = Query(default="ساروم"), limit: int = Query(default=200, ge=1, le=1000), db: Session = Depends(get_db)):
    rows=db.scalars(select(models.CodalAnnouncement).where(models.CodalAnnouncement.symbol_ticker==symbol).order_by(models.CodalAnnouncement.period_end.desc().nullslast(),models.CodalAnnouncement.publish_at.desc().nullslast()).limit(limit)).all()
    return [{"id":x.id,"tracing_no":x.tracing_no,"title":x.title,"period_end":x.period_end,"publish_at":x.publish_at,"report_type":x.report_type,"is_audited":x.is_audited,"versions":len(x.versions)} for x in rows if any(k in ((x.title or '')+' '+(x.report_type or '')).lower() for k in ('میان‌دوره','میاندوره','۳ ماه','۶ ماه','۹ ماه','۱۲ ماه','quarter','interim','financial'))]

@app.get("/api/v1/fx/daily")
def list_fx_rates(limit: int = Query(default=365, ge=1, le=3000), db: Session = Depends(get_db)):
    rows=db.execute(select(models.DailyFxRate, models.DailyFxRateBar).outerjoin(models.DailyFxRateBar, models.DailyFxRateBar.rate_id == models.DailyFxRate.id).order_by(models.DailyFxRate.rate_date.desc()).limit(limit)).all()
    return [{"id":x.id,"rate_date":x.rate_date,"currency_pair":x.currency_pair,"rate":str(x.rate),"unit":x.unit,"source":x.source,
             "open":str(bar.open) if bar and bar.open is not None else None,
             "low":str(bar.low) if bar and bar.low is not None else None,
             "high":str(bar.high) if bar and bar.high is not None else None,
             "close":str(bar.close) if bar else str(x.rate),
             "jalali_date":bar.jalali_date if bar else None} for x,bar in rows]

class FxRateIn(BaseModel):
    rate_date: date
    rate: Decimal = Field(gt=0)
    source: str = "manual"
    currency_pair: str = "USD/IRR"

@app.post("/api/v1/fx/daily")
def save_fx_rate(payload: FxRateIn, db: Session = Depends(get_db)):
    row=db.scalar(select(models.DailyFxRate).where(models.DailyFxRate.rate_date==payload.rate_date,models.DailyFxRate.currency_pair==payload.currency_pair,models.DailyFxRate.source==payload.source))
    old_rate = row.rate if row is not None else None
    if row is None:
        row=models.DailyFxRate(rate_date=payload.rate_date,rate=payload.rate,source=payload.source,currency_pair=payload.currency_pair); db.add(row)
        action = "insert"
    else:
        row.rate=payload.rate
        action = "update"
    db.flush()
    db.add(models.FxRateAudit(rate_id=row.id, rate_date=payload.rate_date, currency_pair=payload.currency_pair,
        old_rate=old_rate, new_rate=payload.rate, source=payload.source, action=action,
        actor="admin" if payload.source == "manual" else "system"))
    db.commit(); db.refresh(row)
    return {"id":row.id,"rate_date":row.rate_date,"rate":str(row.rate),"source":row.source}

@app.get("/api/v1/analytics/catalog")
def analytics_catalog():
    return {"available_when_inputs_exist":["EPS","P/E","P/E TTM","Forward P/E","P/B","P/S","PEG","EV/EBITDA","EV/Sales","ROE","ROA","حاشیه سود ناخالص","حاشیه سود عملیاتی","حاشیه سود خالص","نسبت جاری","نسبت سریع","بدهی به حقوق صاحبان سهام","بازده سود نقدی","بازده جریان نقد آزاد"],"calculation_policy":"Only source-linked, period-aligned facts and point-in-time market data are eligible. Missing inputs are not estimated."}

@app.get("/api/v1/codal/reports/{announcement_id}/tables")
def report_tables(announcement_id: int, db: Session = Depends(get_db)):
    sheets=db.scalars(select(models.CodalReportSheet).where(models.CodalReportSheet.announcement_id==announcement_id).order_by(models.CodalReportSheet.sheet_id)).all()
    output=[]
    for sheet in sheets:
        tables=db.scalars(select(models.CodalReportTable).where(models.CodalReportTable.sheet_record_id==sheet.id).order_by(models.CodalReportTable.table_index)).all()
        output.append({"sheet_id":sheet.sheet_id,"title":sheet.sheet_title,"status":sheet.status,"tables":[{"table_index":t.table_index,"caption":t.caption,"headers":t.headers,"rows":t.rows,"row_count":t.row_count,"column_count":t.column_count} for t in tables]})
    return {"announcement_id":announcement_id,"sheets":output}

@app.post("/api/v1/codal/reports/{announcement_id}/extract")
def extract_codal_report(announcement_id: int, db: Session = Depends(get_db)):
    try:
        from .services.codal_reports import extract_announcement
        return extract_announcement(db,announcement_id)
    except ValueError as exc: raise HTTPException(422,str(exc)) from exc
    except Exception as exc: raise HTTPException(502,f"استخراج جداول کدال ناموفق بود: {type(exc).__name__}: {exc}") from exc

@app.get("/api/v1/admin/symbols")
def admin_symbols(db: Session = Depends(get_db)):
    return db.scalars(select(models.Symbol).order_by(models.Symbol.ticker)).all()

@app.post("/api/v1/codal/reports/extract-batch/{symbol}")
def extract_codal_report_batch(symbol: str, limit: int = Query(default=50, ge=1, le=200), only_missing: bool = True, db: Session = Depends(get_db)):
    from .services.codal_reports import extract_announcement
    stmt=select(models.CodalAnnouncement).where(models.CodalAnnouncement.symbol_ticker==symbol).order_by(models.CodalAnnouncement.publish_at.desc().nullslast()).limit(limit)
    announcements=db.scalars(stmt).all(); results=[]
    for item in announcements:
        if only_missing and db.scalar(select(models.CodalReportSheet.id).where(models.CodalReportSheet.announcement_id==item.id).limit(1)):
            continue
        try: results.append({"announcement_id":item.id,"title":item.title,**extract_announcement(db,item.id)})
        except Exception as exc:
            db.rollback(); results.append({"announcement_id":item.id,"title":item.title,"error":f"{type(exc).__name__}: {exc}"})
    return {"symbol":symbol,"checked":len(announcements),"processed":len(results),"results":results}


# Historical instrument catalog: keep delisted, suspended and renamed tickers as records.
class SymbolHistoryIn(BaseModel):
    ticker: str = Field(min_length=1, max_length=32)
    company_name: str = Field(min_length=1, max_length=255)
    company_key: str | None = None
    isin: str | None = None
    market: str | None = None
    industry: str | None = None
    instrument_type: str = "stock"
    status: str = "unknown"
    valid_from: date | None = None
    valid_to: date | None = None
    source: str = "manual"
    source_url: str | None = None
    raw_payload: dict | None = None


class SymbolHistoryBatchIn(BaseModel):
    items: list[SymbolHistoryIn] = Field(min_length=1, max_length=10000)


@app.get("/api/v1/symbol-history")
def list_symbol_history(q: str | None = None, status: str | None = None,
                        limit: int = Query(default=500, ge=1, le=5000), offset: int = Query(default=0, ge=0),
                        db: Session = Depends(get_db)):
    stmt = select(models.SymbolHistory)
    if q:
        pattern = f"%{q}%"
        stmt = stmt.where((models.SymbolHistory.ticker.ilike(pattern)) | (models.SymbolHistory.company_name.ilike(pattern)) | (models.SymbolHistory.isin.ilike(pattern)))
    if status:
        stmt = stmt.where(models.SymbolHistory.status == status)
    rows = db.scalars(stmt.order_by(models.SymbolHistory.ticker, models.SymbolHistory.valid_from).offset(offset).limit(limit)).all()
    return [{"id":x.id,"ticker":x.ticker,"company_name":x.company_name,"company_key":x.company_key,"isin":x.isin,
             "market":x.market,"industry":x.industry,"instrument_type":x.instrument_type,"status":x.status,
             "valid_from":x.valid_from,"valid_to":x.valid_to,"source":x.source} for x in rows]


@app.post("/api/v1/admin/symbol-history/import")
def import_symbol_history(payload: SymbolHistoryBatchIn, db: Session = Depends(get_db)):
    inserted = updated = 0
    for item in payload.items:
        data = item.model_dump()
        existing = db.scalar(select(models.SymbolHistory).where(
            models.SymbolHistory.ticker == item.ticker,
            models.SymbolHistory.valid_from == item.valid_from,
            models.SymbolHistory.company_name == item.company_name))
        if existing is None:
            db.add(models.SymbolHistory(**data)); inserted += 1
        else:
            for key, value in data.items():
                setattr(existing, key, value)
            updated += 1
    db.commit()
    return {"received":len(payload.items),"inserted":inserted,"updated":updated,"note":"سوابق تاریخی نگهداری می‌شوند و رکورد حذف‌شده‌ای پاک نمی‌شود."}


@app.get("/api/v1/admin/fx/audit")
def fx_audit(limit: int = Query(default=200, ge=1, le=2000), db: Session = Depends(get_db)):
    rows = db.scalars(select(models.FxRateAudit).order_by(models.FxRateAudit.created_at.desc()).limit(limit)).all()
    return [{"id":x.id,"rate_date":x.rate_date,"old_rate":str(x.old_rate) if x.old_rate is not None else None,
             "new_rate":str(x.new_rate),"source":x.source,"action":x.action,"actor":x.actor,"created_at":x.created_at}
            for x in rows]


@app.post("/api/v1/admin/fx/tgju-sync")
async def sync_tgju_fx(db: Session = Depends(get_db)):
    """Fetch TGJU historical USD/IRR observations and upsert them without estimating missing dates."""
    from .services.tgju import fetch_history, TGJU_HISTORY_URL
    try:
        rows, raw_payload = await fetch_history()
    except Exception as exc:
        raise HTTPException(502, f"دریافت تاریخچه TGJU ناموفق بود: {type(exc).__name__}: {exc}") from exc
    inserted = updated = 0
    for item in rows:
        day, rate = item["rate_date"], item["rate"]
        row = db.scalar(select(models.DailyFxRate).where(
            models.DailyFxRate.rate_date == day,
            models.DailyFxRate.currency_pair == "USD/IRR",
            models.DailyFxRate.source == "tgju"))
        old_rate = row.rate if row is not None else None
        if row is None:
            row = models.DailyFxRate(rate_date=day, rate=rate, source="tgju", currency_pair="USD/IRR")
            db.add(row); action = "insert"; inserted += 1
        else:
            if row.rate == rate:
                action = None
            else:
                row.rate = rate; action = "update"; updated += 1
        db.flush()
        if action:
            db.add(models.FxRateAudit(rate_id=row.id, rate_date=day, currency_pair="USD/IRR",
                old_rate=old_rate, new_rate=rate, source="tgju", action=action, actor="tgju-scraper",
                source_url=TGJU_HISTORY_URL, raw_payload={"row": item["raw"]}))
        bar = db.scalar(select(models.DailyFxRateBar).where(models.DailyFxRateBar.rate_id == row.id))
        if bar is None:
            bar = models.DailyFxRateBar(rate_id=row.id, close=item["close"])
            db.add(bar)
        bar.open = item["open"]; bar.low = item["low"]; bar.high = item["high"]
        bar.close = item["close"]; bar.jalali_date = item["jalali_date"] or None
        bar.source_url = TGJU_HISTORY_URL
    db.commit()
    return {"source":"tgju","rows_received":len(rows),"inserted":inserted,"updated":updated,
            "first_date":rows[0]["rate_date"],"last_date":rows[-1]["rate_date"],
            "unit":"IRR per USD","note":"تاریخ‌های بدون رکورد در منبع، حدس یا پر نمی‌شوند."}


@app.post("/api/v1/admin/symbol-history/sync-tsetmc")
async def sync_tsetmc_symbol_catalog(db: Session = Depends(get_db)):
    """Import the current TSETMC market-watch universe; prior historical rows are never removed."""
    try:
        payload = await TsetmcClient().market_watch()
    except Exception as exc:
        raise HTTPException(502, f"دریافت فهرست نمادها از TSETMC ناموفق بود: {type(exc).__name__}: {exc}") from exc
    rows = None
    for key in ("marketwatch", "marketWatch", "MarketWatch", "data", "Data"):
        value = payload.get(key)
        if isinstance(value, list):
            rows = value
            break
        if isinstance(value, dict):
            for nested_key in ("marketwatch", "marketWatch", "items", "Items"):
                if isinstance(value.get(nested_key), list):
                    rows = value[nested_key]
                    break
        if rows is not None:
            break
    if not rows:
        raise HTTPException(502, "پاسخ TSETMC فهرست قابل تشخیص نمادها نداشت؛ هیچ داده‌ای ذخیره نشد.")
    inserted = updated = 0
    for raw in rows:
        if not isinstance(raw, dict):
            continue
        instrument = raw.get("instrument") or raw.get("Instrument") or raw.get("instrumentInfo") or raw
        if not isinstance(instrument, dict):
            continue
        data = {str(k).lower(): v for k, v in instrument.items()}
        ticker = data.get("lval18afc") or data.get("ticker") or data.get("symbol")
        company_name = data.get("lval30") or data.get("name") or data.get("companyname")
        ins_code = data.get("inscode") or data.get("inscode1") or data.get("instrumentcode")
        isin = data.get("cisin") or data.get("isin")
        if not ticker or not company_name:
            continue
        ticker, company_name = str(ticker).strip(), str(company_name).strip()
        market = data.get("flowtitle") or data.get("market") or data.get("flow")
        industry = data.get("cs") or data.get("industry") or data.get("sector")
        existing = db.scalar(select(models.SymbolHistory).where(
            models.SymbolHistory.ticker == ticker,
            models.SymbolHistory.company_name == company_name,
            models.SymbolHistory.valid_from.is_(None)))
        if existing is None:
            db.add(models.SymbolHistory(ticker=ticker, company_name=company_name,
                company_key=str(ins_code) if ins_code else None, isin=str(isin) if isin else None,
                market=str(market) if market is not None else None,
                industry=str(industry) if industry is not None else None,
                instrument_type="stock", status="active", source="tsetmc",
                source_url="https://cdn.tsetmc.com/api/ClosingPrice/GetMarketWatch",
                raw_payload=raw))
            inserted += 1
        else:
            existing.company_key = str(ins_code) if ins_code else existing.company_key
            existing.isin = str(isin) if isin else existing.isin
            existing.market = str(market) if market is not None else existing.market
            existing.industry = str(industry) if industry is not None else existing.industry
            existing.status = "active"
            existing.source = "tsetmc"
            existing.raw_payload = raw
            updated += 1
        current = db.scalar(select(models.Symbol).where(models.Symbol.ticker == ticker))
        if current is None:
            db.add(models.Symbol(ticker=ticker, name=company_name, isin=str(isin) if isin else None))
        else:
            current.name = company_name
            if isin:
                current.isin = str(isin)
    db.commit()
    return {"source":"tsetmc","rows_received":len(rows),"inserted":inserted,"updated":updated,
            "note":"این همگام‌سازی فهرست جاری بازار را می‌آورد؛ نمادهای تاریخی حذف‌شده باید از منبع تاریخی/فایل فهرست وارد شوند و هیچ سابقه‌ای پاک نمی‌شود."}
