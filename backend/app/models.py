from datetime import date, datetime
from decimal import Decimal
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
    raw_payload: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
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
    value: Mapped[Decimal] = mapped_column(Numeric(28, 6))
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
    close_price: Mapped[Decimal] = mapped_column(Numeric(24, 4))
    volume: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    source: Mapped[str] = mapped_column(String(32), default="tsetmc")
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class MonthlySale(Base):
    __tablename__ = "monthly_sales"
    __table_args__ = (UniqueConstraint("symbol_ticker", "period_year", "period_month", "product"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    symbol_ticker: Mapped[str] = mapped_column(String(32), index=True)
    period_year: Mapped[int] = mapped_column(Integer)
    period_month: Mapped[int] = mapped_column(Integer)
    product: Mapped[str] = mapped_column(String(128))
    quantity: Mapped[Decimal | None] = mapped_column(Numeric(28, 6), nullable=True)
    sales_amount: Mapped[Decimal | None] = mapped_column(Numeric(28, 6), nullable=True)
    unit: Mapped[str] = mapped_column(String(32), default="IRR")
    source_announcement_id: Mapped[int | None] = mapped_column(ForeignKey("codal_announcements.id"), nullable=True)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

class MarketSnapshot(Base):
    __tablename__ = "market_snapshots"
    __table_args__ = (UniqueConstraint("symbol_ticker", "as_of", "source"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    symbol_ticker: Mapped[str] = mapped_column(String(32), index=True)
    as_of: Mapped[date] = mapped_column(Date, index=True)
    close_price: Mapped[Decimal | None] = mapped_column(Numeric(24, 4), nullable=True)
    shares_outstanding: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    market_cap: Mapped[Decimal | None] = mapped_column(Numeric(30, 4), nullable=True)
    source: Mapped[str] = mapped_column(String(32), default="manual")
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

class Portfolio(Base):
    __tablename__ = "portfolios"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(128))
    base_currency: Mapped[str] = mapped_column(String(8), default="IRR")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    holdings: Mapped[list["PortfolioHolding"]] = relationship(back_populates="portfolio", cascade="all, delete-orphan")

class PortfolioHolding(Base):
    __tablename__ = "portfolio_holdings"
    __table_args__ = (UniqueConstraint("portfolio_id", "symbol_ticker"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    portfolio_id: Mapped[int] = mapped_column(ForeignKey("portfolios.id", ondelete="CASCADE"), index=True)
    symbol_ticker: Mapped[str] = mapped_column(String(32), index=True)
    quantity: Mapped[Decimal] = mapped_column(Numeric(28, 6))
    average_cost: Mapped[Decimal | None] = mapped_column(Numeric(28, 6), nullable=True)
    portfolio: Mapped[Portfolio] = relationship(back_populates="holdings")

class ForecastScenario(Base):
    __tablename__ = "forecast_scenarios"
    id: Mapped[int] = mapped_column(primary_key=True)
    symbol_ticker: Mapped[str] = mapped_column(String(32), index=True)
    metric: Mapped[str] = mapped_column(String(64))
    base_value: Mapped[Decimal] = mapped_column(Numeric(28, 6))
    annual_growth: Mapped[Decimal] = mapped_column(Numeric(12, 8))
    periods: Mapped[int] = mapped_column(Integer, default=3)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class DailyFxRate(Base):
    __tablename__ = "daily_fx_rates"
    __table_args__ = (UniqueConstraint("rate_date", "currency_pair", "source"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    rate_date: Mapped[date] = mapped_column(Date, index=True)
    currency_pair: Mapped[str] = mapped_column(String(16), default="USD/IRR")
    rate: Mapped[Decimal] = mapped_column(Numeric(24, 4))
    unit: Mapped[str] = mapped_column(String(16), default="IRR per USD")
    source: Mapped[str] = mapped_column(String(64), default="manual")
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class DocumentContentItem(Base):
    """Lossless extracted text/table cells; raw source file remains the authority."""
    __tablename__ = "document_content_items"
    __table_args__ = (UniqueConstraint("document_version_id", "locator"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    document_version_id: Mapped[int] = mapped_column(ForeignKey("document_versions.id", ondelete="CASCADE"), index=True)
    locator: Mapped[str] = mapped_column(String(255))
    content_kind: Mapped[str] = mapped_column(String(32), default="text")
    content_text: Mapped[str] = mapped_column(Text)
    extracted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class CodalReportSheet(Base):
    __tablename__ = "codal_report_sheets"
    __table_args__ = (UniqueConstraint("announcement_id", "sheet_id"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    announcement_id: Mapped[int] = mapped_column(ForeignKey("codal_announcements.id", ondelete="CASCADE"), index=True)
    sheet_id: Mapped[int] = mapped_column(Integer)
    sheet_title: Mapped[str | None] = mapped_column(String(512), nullable=True)
    source_url: Mapped[str] = mapped_column(Text)
    content_hash: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(32), default="extracted")
    extracted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

class CodalReportTable(Base):
    __tablename__ = "codal_report_tables"
    __table_args__ = (UniqueConstraint("sheet_record_id", "table_index"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    sheet_record_id: Mapped[int] = mapped_column(ForeignKey("codal_report_sheets.id", ondelete="CASCADE"), index=True)
    table_index: Mapped[int] = mapped_column(Integer)
    caption: Mapped[str | None] = mapped_column(Text, nullable=True)
    headers: Mapped[list | None] = mapped_column(JSON, nullable=True)
    rows: Mapped[list] = mapped_column(JSON, default=list)
    row_count: Mapped[int] = mapped_column(Integer, default=0)
    column_count: Mapped[int] = mapped_column(Integer, default=0)


class SymbolHistory(Base):
    """Historical instrument identity; rows are never deleted when a ticker is delisted or renamed."""
    __tablename__ = "symbol_history"
    __table_args__ = (UniqueConstraint("ticker", "valid_from", "company_name"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    ticker: Mapped[str] = mapped_column(String(32), index=True)
    company_name: Mapped[str] = mapped_column(String(255), index=True)
    company_key: Mapped[str | None] = mapped_column(String(64), index=True, nullable=True)
    isin: Mapped[str | None] = mapped_column(String(32), index=True, nullable=True)
    market: Mapped[str | None] = mapped_column(String(64), nullable=True)
    industry: Mapped[str | None] = mapped_column(String(128), nullable=True)
    instrument_type: Mapped[str] = mapped_column(String(32), default="stock")
    status: Mapped[str] = mapped_column(String(32), default="unknown", index=True)
    valid_from: Mapped[date | None] = mapped_column(Date, nullable=True)
    valid_to: Mapped[date | None] = mapped_column(Date, nullable=True)
    source: Mapped[str] = mapped_column(String(64), default="manual")
    source_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    raw_payload: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class FxRateAudit(Base):
    """Append-only audit trail for imported and manually corrected FX observations."""
    __tablename__ = "fx_rate_audit"
    id: Mapped[int] = mapped_column(primary_key=True)
    rate_id: Mapped[int | None] = mapped_column(ForeignKey("daily_fx_rates.id", ondelete="SET NULL"), nullable=True, index=True)
    rate_date: Mapped[date] = mapped_column(Date, index=True)
    currency_pair: Mapped[str] = mapped_column(String(16), default="USD/IRR")
    old_rate: Mapped[Decimal | None] = mapped_column(Numeric(24, 4), nullable=True)
    new_rate: Mapped[Decimal] = mapped_column(Numeric(24, 4))
    source: Mapped[str] = mapped_column(String(64))
    action: Mapped[str] = mapped_column(String(16), default="insert")
    actor: Mapped[str] = mapped_column(String(128), default="system")
    source_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    raw_payload: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

class CodalReportCell(Base):
    """Individual HTML Decision-page table cells with exact table/row/column coordinates."""
    __tablename__ = "codal_report_cells"
    __table_args__ = (UniqueConstraint("announcement_id", "locator"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    announcement_id: Mapped[int] = mapped_column(ForeignKey("codal_announcements.id", ondelete="CASCADE"), index=True)
    sheet_id: Mapped[int] = mapped_column(Integer)
    table_index: Mapped[int] = mapped_column(Integer)
    row_no: Mapped[int] = mapped_column(Integer)
    column_no: Mapped[int] = mapped_column(Integer)
    locator: Mapped[str] = mapped_column(String(512), index=True)
    cell_text: Mapped[str] = mapped_column(Text)
    content_kind: Mapped[str] = mapped_column(String(32), default="table_cell")
    extracted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
