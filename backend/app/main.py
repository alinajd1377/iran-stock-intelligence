from contextlib import asynccontextmanager
from fastapi import Depends, FastAPI, HTTPException, Query
from sqlalchemy import select, text
from sqlalchemy.orm import Session
from .config import settings
from .db import Base, engine, get_db
from . import models
from .services.codal import CodalClient

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Bootstrap convenience for the first local MVP. Use Alembic migrations
    # before production deployments.
    Base.metadata.create_all(bind=engine)
    with Session(engine) as db:
        if not db.scalar(select(models.Symbol).where(models.Symbol.ticker == "ساروم")):
            db.add(models.Symbol(ticker="ساروم", name="سیمان ارومیه"))
            db.commit()
    yield

app = FastAPI(title=settings.app_name, version="0.2.0", description="Iranian equity research platform", lifespan=lifespan)

@app.get("/")
def root():
    return {"name": settings.app_name, "version": "0.2.0", "docs": "/docs"}

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
def list_announcements(
    symbol: str = Query(default="ساروم"),
    limit: int = Query(default=50, ge=1, le=200),
    db: Session = Depends(get_db),
):
    stmt = select(models.CodalAnnouncement).where(
        models.CodalAnnouncement.symbol_ticker == symbol
    ).order_by(models.CodalAnnouncement.publish_at.desc()).limit(limit)
    return db.scalars(stmt).all()

@app.post("/api/v1/sync/codal/{symbol}")
async def sync_codal(symbol: str, pages: int = Query(default=1, ge=1, le=10), db: Session = Depends(get_db)):
    client = CodalClient()
    inserted = 0
    seen = 0
    try:
        for page in range(1, pages + 1):
            payload = await client.search(symbol=symbol, page=page)
            rows = payload.get("Letters") or payload.get("letters") or payload.get("Data") or payload.get("data") or []
            if isinstance(rows, dict):
                rows = rows.get("Letters") or rows.get("letters") or []
            if not isinstance(rows, list):
                raise ValueError("ساختار پاسخ کدال با قالب مورد انتظار سازگار نیست")
            if not rows:
                break
            for row in rows:
                if not isinstance(row, dict):
                    continue
                tracing = row.get("TracingNo") or row.get("tracingNo") or row.get("tracing_no")
                if not tracing:
                    continue
                seen += 1
                exists = db.scalar(select(models.CodalAnnouncement).where(models.CodalAnnouncement.tracing_no == str(tracing)))
                if exists:
                    continue
                title = row.get("Title") or row.get("title") or row.get("LetterCodeTitle") or "بدون عنوان"
                url = row.get("Url") or row.get("url") or row.get("ExcelUrl") or row.get("PdfUrl")
                db.add(models.CodalAnnouncement(
                    tracing_no=str(tracing), symbol_ticker=symbol, title=str(title),
                    source_url=str(url) if url else None, raw_payload=row
                ))
                inserted += 1
            db.commit()
    except Exception as exc:
        db.rollback()
        raise HTTPException(status_code=502, detail=f"دریافت کدال ناموفق بود: {type(exc).__name__}: {exc}") from exc
    return {"symbol": symbol, "rows_seen": seen, "inserted": inserted, "note": "تاریخ‌ها و نوع گزارش در این نسخه هنوز از همه قالب‌های کدال استخراج نمی‌شوند."}
