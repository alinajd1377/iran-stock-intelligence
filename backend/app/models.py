from datetime import date, datetime
from sqlalchemy import BigInteger, Boolean, Date, DateTime, ForeignKey, Integer, Numeric, String, Text, UniqueConstraint, func, JSON
from sqlalchemy.orm import Mapped, mapped_column, relationship
from .db import Base

class Symbol(Base):
    __tablename__ = "symbols"
    id: Mapped[int] = mapped_column(primary_key=True)
    ticker: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(255))
    isin: Mapped[str | None] = mapped_column(String(32), nullable=True)

class CodalAnnouncement(Base):
    __tablename__ = "codal_announcements"
    id: Mapped[int] = mapped_column(primary_key=True)
    tracing_no: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    symbol_ticker: Mapped[str] = mapped_column(String(32), index=True)
    title: Mapped[str] = mapped_column(Text)
    publish_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    period_end: Mapped[date | None] = mapped_column(Date, nullable=True)
    report_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    is_audited: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    source_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    raw_payload: Mapped[dict | None] = mapped_column(JSON, nullable=True)\n    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    versions: Mapped[list["DocumentVersion"]] = relationship(back_populates="announcement", cascade="all, delete-orphan")

class DocumentVersion(Base):
    __tablename__ = "document_versions"
    __table_args__ = (
        UniqueConstraint("announcement_id", "version_no"),
        UniqueConstraint("announcement_id", "file_hash"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    announcement_id: Mapped[int] = mapped_column(ForeignKey("codal_announcements.id", ondelete="CASCADE"))
    version_no: Mapped[int] = mapped_column(Integer, default=1)
    file_hash: Mapped[str] = mapped_column(String(64))
    file_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    storage_path: Mapped[str] = mapped_column(Text)
    content_type: Mapped[str | None] = mapped_column(String(128), nullable=True)
    downloaded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    announcement: Mapped[CodalAnnouncement] = relationship(back_populates="versions")

class FinancialFact(Base):
    __tablename__ = "financial_facts"
    __table_args__ = (UniqueConstraint("announcement_id", "concept", "period_end", "unit"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    announcement_id: Mapped[int] = mapped_column(ForeignKey("codal_announcements.id", ondelete="CASCADE"), index=True)
    concept: Mapped[str] = mapped_column(String(128), index=True)
    period_end: Mapped[date] = mapped_column(Date, index=True)
    value: Mapped[Numeric] = mapped_column(Numeric(28, 6))
    unit: Mapped[str] = mapped_column(String(32), default="IRR")
    currency: Mapped[str] = mapped_column(String(8), default="IRR")
    source_label: Mapped[str | None] = mapped_column(String(255), nullable=True)
    extracted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

class DailyPrice(Base):
    __tablename__ = "daily_prices"
    __table_args__ = (UniqueConstraint("symbol_ticker", "trade_date"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    symbol_ticker: Mapped[str] = mapped_column(String(32), index=True)
    trade_date: Mapped[date] = mapped_column(Date)
    close_price: Mapped[Numeric] = mapped_column(Numeric(24, 4))
    volume: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    source: Mapped[str] = mapped_column(String(32), default="tsetmc")
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
