from fastapi import Depends, FastAPI
from sqlalchemy import text
from sqlalchemy.orm import Session
from .config import settings
from .db import get_db

app = FastAPI(title=settings.app_name, version="0.1.0", description="Iranian equity research platform")

@app.get("/health")
def health():
    return {"status": "ok", "environment": settings.app_env}

@app.get("/health/db")
def database_health(db: Session = Depends(get_db)):
    db.execute(text("SELECT 1"))
    return {"database": "ok"}

@app.get("/api/v1/symbols")
def list_seed_symbols():
    return [{"ticker": "ساروم", "name": "سیمان ارومیه"}]

@app.get("/")
def root():
    return {"name": settings.app_name, "docs": "/docs"}
