"""Codal archive worker. Keeps raw source payloads and content-addressed files."""
import argparse
import hashlib
import json
import logging
import os
import re
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin, urlparse, parse_qs

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session
from app import models
from app.config import settings
from app.db import Base, engine
from app.services.financial_parser import parse_document

LOG = logging.getLogger("codal_archive")
SEARCH_URL = settings.codal_base_url.rstrip("/") + "/api/search/v2/q"
RAW = Path(os.getenv("RAW_DATA_DIR", settings.raw_data_dir)) / "codal"
SYMBOLS = [x.strip() for x in os.getenv("CODAL_SYMBOLS", "").split(",") if x.strip()]
PAGE_SIZE = int(os.getenv("CODAL_PAGE_SIZE", "100"))
INTERVAL = int(os.getenv("CODAL_POLL_INTERVAL_SECONDS", "3600"))

def pick(row, *names):
    data = {str(k).lower(): v for k, v in row.items()}
    for name in names:
        value = data.get(name.lower())
        if value not in (None, ""):
            return value
    return None

def parse_rows(response):
    try:
        data = response.json()
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            for key in ("Letters", "letters", "Data", "data", "Results", "results"):
                rows = data.get(key)
                if isinstance(rows, list):
                    return rows
                if isinstance(rows, dict):
                    for sub in ("Letters", "letters", "Data", "data"):
                        if isinstance(rows.get(sub), list):
                            return rows[sub]
    except ValueError:
        pass
    import xml.etree.ElementTree as ET
    root = ET.fromstring(response.text)
    found = []
    for node in root.iter():
        children = list(node)
        if not children:
            continue
        item = {c.tag.split("}")[-1]: (c.text or "").strip() for c in children if not list(c)}
        if pick(item, "TracingNo", "tracingNo", "TracingNumber"):
            found.append(item)
    return found

def jalali_date(value):
    """Convert a Jalali YYYY/MM/DD date to Gregorian without guessing invalid dates."""
    from datetime import date
    match = re.search(r"(1[34][0-9]{2})[/-]([0-9]{1,2})[/-]([0-9]{1,2})", str(value or ""))
    if not match:
        return None
    jy, jm, jd = map(int, match.groups())
    if not 1 <= jm <= 12 or not 1 <= jd <= 31:
        return None
    jy -= 979
    days = 365 * jy + (jy // 33) * 8 + ((jy % 33 + 3) // 4)
    days += (jm - 1) * 31 if jm <= 6 else 186 + (jm - 7) * 30
    days += jd - 1 + 79
    gy = 1600 + 400 * (days // 146097)
    days %= 146097
    leap = True
    if days >= 36525:
        days -= 1
        gy += 100 * (days // 36524)
        days %= 36524
        if days >= 365:
            days += 1
        else:
            leap = False
    gy += 4 * (days // 1461)
    days %= 1461
    if days >= 366:
        leap = False
        days -= 1
        gy += days // 365
        days %= 365
    mdays = [31, 29 if leap else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]
    month = 1
    while month <= 12 and days >= mdays[month - 1]:
        days -= mdays[month - 1]
        month += 1
    try:
        return date(gy, month, days + 1)
    except ValueError:
        return None

def metadata(row, symbol):
    tracing = pick(row, "TracingNo", "tracingNo", "tracing_no", "TracingNumber")
    published = pick(row, "PublishDateTime", "publishDateTime", "PublishDate")
    try:
        published = datetime.fromisoformat(str(published).replace("Z", "+00:00")) if published else None
    except ValueError:
        published = None
    links = []
    serial = pick(row, "LetterSerial", "letterSerial", "letter_serial")
    for key in ("Url", "PdfUrl", "ExcelUrl", "HtmlUrl", "AttachmentUrl"):
        value = pick(row, key)
        if value:
            value = str(value)
            if value.startswith("//"):
                value = "https:" + value
            elif value.startswith("/"):
                value = urljoin("https://www.codal.ir", value)
            elif not value.startswith("http"):
                value = urljoin("https://www.codal.ir/", value)
            if value not in links:
                links.append(value)
            if not serial:
                query = parse_qs(urlparse(value).query)
                serial = next((values[0] for key, values in query.items() if key.lower() == "letterserial" and values), None)
    if serial:
        excel_url = f"https://excel.codal.ir/service/Excel/GetAll/{serial}/0"
        if excel_url not in links:
            links.append(excel_url)
    audited = pick(row, "IsAudited", "Audited")
    return {
        "tracing": str(tracing) if tracing else None,
        "letter_serial": str(serial) if serial else None,
        "title": str(pick(row, "Title", "LetterCodeTitle") or "بدون عنوان"),
        "period_end": jalali_date(pick(row, "PeriodEndToDate", "PeriodEndDate")),
        "publish_at": published,
        "report_type": str(pick(row, "LetterCode", "LetterCodeTitle") or "")[:64] or None,
        "is_audited": (str(audited).lower() in ("true", "1", "yes")) if audited is not None else None,
        "links": links,
        "symbol": symbol,
    }

def save_file(client, url):
    response = client.get(url)
    response.raise_for_status()
    data = response.content
    digest = hashlib.sha256(data).hexdigest()
    content_type = response.headers.get("content-type", "").lower()
    suffix = Path(url.split("?")[0]).suffix.lower()
    if data[:2] == b"PK":
        suffix = ".xlsx"
    elif data[:8] == bytes.fromhex("D0CF11E0A1B11AE1"):
        suffix = ".xls"
    elif data.lstrip().lower().startswith((b"<!doctype html", b"<html", b"<table")) or "text/html" in content_type:
        suffix = ".html"
    elif suffix not in (".pdf", ".xlsx", ".xls", ".xlsm", ".html", ".htm"):
        suffix = ".pdf" if "pdf" in content_type else ".xlsx" if ("excel" in content_type or "spreadsheet" in content_type) else ".bin"
    folder = RAW / digest[:2]
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / (digest + suffix)
    if not target.exists():
        target.write_bytes(data)
    return {"path": str(target), "sha256": digest, "size": len(data), "content_type": content_type or None}


def extract_content_items(path):
    """Extract non-empty spreadsheet cells and text-layer PDF tables/text; OCR is deliberately disabled."""
    suffix = Path(path).suffix.lower()
    items = []
    if suffix in (".xlsx", ".xlsm"):
        from openpyxl import load_workbook
        book = load_workbook(path, read_only=True, data_only=True)
        formulas = load_workbook(path, read_only=True, data_only=False)
        formula_sheets = {sheet.title: sheet for sheet in formulas.worksheets}
        for sheet in book.worksheets:
            formula_sheet = formula_sheets[sheet.title]
            for rno, row in enumerate(sheet.iter_rows(values_only=True), start=1):
                for cno, value in enumerate(row, start=1):
                    locator = f"{sheet.title}!R{rno}C{cno}"
                    if value is not None and str(value).strip():
                        items.append((locator, "cell", str(value)[:12000]))
                    formula = formula_sheet.cell(row=rno, column=cno).value
                    if isinstance(formula, str) and formula.startswith("="):
                        items.append((locator + ":formula", "formula", formula[:12000]))
        book.close()
        formulas.close()
    elif suffix == ".xls":
        import xlrd
        book = xlrd.open_workbook(path, on_demand=True)
        for sheet in book.sheets():
            for rno in range(sheet.nrows):
                for cno in range(sheet.ncols):
                    value = sheet.cell_value(rno, cno)
                    if value is not None and str(value).strip():
                        items.append((f"{sheet.name}!R{rno+1}C{cno+1}", "cell", str(value)[:12000]))
        book.release_resources()
    elif suffix == ".pdf":
        import pdfplumber
        with pdfplumber.open(path) as pdf:
            for page_no, page in enumerate(pdf.pages, start=1):
                content = page.extract_text() or ""
                if content.strip():
                    items.append((f"page:{page_no}", "page_text", content[:50000]))
                for table_no, table in enumerate(page.extract_tables() or [], start=1):
                    for row_no, row in enumerate(table, start=1):
                        for col_no, value in enumerate(row or [], start=1):
                            if value is not None and str(value).strip():
                                items.append((f"page:{page_no}:table:{table_no}:r{row_no}:c{col_no}", "table_cell", str(value)[:12000]))
                # OCR is intentionally not used: image-only pages remain unextracted rather than guessing financial values.
    elif suffix in (".html", ".htm"):
        from html.parser import HTMLParser
        class TextCollector(HTMLParser):
            def __init__(self):
                super().__init__(); self.parts=[]
            def handle_data(self, data):
                if data.strip(): self.parts.append(data.strip())
        html = Path(path).read_text(encoding="utf-8", errors="replace")
        parser = TextCollector()
        parser.feed(html)
        for i, value in enumerate(parser.parts, start=1):
            items.append((f"html:text:{i}", "text", value[:12000]))
        try:
            from app.services.codal_reports import parse_html_tables
            for table_no, table in enumerate(parse_html_tables(html), start=1):
                all_rows = [table.get("headers") or []] + (table.get("rows") or [])
                for row_no, row in enumerate(all_rows, start=1):
                    for col_no, value in enumerate(row, start=1):
                        if value is not None and str(value).strip():
                            items.append((f"table:{table_no}:r{row_no}:c{col_no}", "table_cell", str(value)[:12000]))
        except Exception:
            LOG.exception("HTML table extraction failed for %s", path)
    return items

def archive_row(db, client, symbol, row):
    meta = metadata(row, symbol)
    if not meta["tracing"]:
        return 0, 0, 0
    announcement = db.scalar(select(models.CodalAnnouncement).where(models.CodalAnnouncement.tracing_no == meta["tracing"]))
    created = 0
    if announcement is None:
        announcement = models.CodalAnnouncement(
            tracing_no=meta["tracing"], symbol_ticker=symbol, title=meta["title"],
            publish_at=meta["publish_at"], period_end=meta["period_end"],
            report_type=meta["report_type"], is_audited=meta["is_audited"],
            source_url=meta["links"][0] if meta["links"] else None, raw_payload={**row, "LetterSerial": meta["letter_serial"]} if meta["letter_serial"] else row,
        )
        db.add(announcement)
        db.flush()
        created = 1
    else:
        announcement.raw_payload = ({**row, "LetterSerial": meta["letter_serial"]} if meta["letter_serial"] else row)
        announcement.publish_at = meta["publish_at"] or announcement.publish_at
        announcement.period_end = meta["period_end"] or announcement.period_end
        announcement.report_type = meta["report_type"] or announcement.report_type
        if meta["is_audited"] is not None:
            announcement.is_audited = meta["is_audited"]
        if not announcement.source_url and meta["links"]:
            announcement.source_url = meta["links"][0]
    files = facts_count = 0
    for url in meta["links"]:
        try:
            stored = save_file(client, url)
            existing = db.scalar(select(models.DocumentVersion).where(
                models.DocumentVersion.announcement_id == announcement.id,
                models.DocumentVersion.file_hash == stored["sha256"]))
            if existing:
                continue
            latest = db.scalar(select(models.DocumentVersion.version_no).where(
                models.DocumentVersion.announcement_id == announcement.id
            ).order_by(models.DocumentVersion.version_no.desc()).limit(1)) or 0
            version = models.DocumentVersion(
                announcement_id=announcement.id, version_no=latest + 1,
                file_hash=stored["sha256"], file_name=Path(stored["path"]).name,
                storage_path=stored["path"], content_type=stored["content_type"])
            db.add(version)
            db.flush()
            files += 1
            try:
                for locator, kind, value in extract_content_items(stored["path"]):
                    db.add(models.DocumentContentItem(document_version_id=version.id, locator=locator,
                        content_kind=kind, content_text=value))
            except Exception:
                LOG.exception("Full-content extraction failed for %s", url)
            if meta["period_end"]:
                try:
                    parsed = parse_document(stored["path"])
                except Exception:
                    LOG.exception("Parser failed for %s", url)
                    parsed = []
                for fact in parsed:
                    duplicate = db.scalar(select(models.FinancialFact).where(
                        models.FinancialFact.announcement_id == announcement.id,
                        models.FinancialFact.concept == fact["concept"],
                        models.FinancialFact.period_end == meta["period_end"],
                        models.FinancialFact.unit == "IRR"))
                    if duplicate is None:
                        db.add(models.FinancialFact(
                            announcement_id=announcement.id, concept=fact["concept"],
                            period_end=meta["period_end"], value=fact["value"],
                            unit="IRR", currency="IRR",
                            source_label=str(fact.get("source_label", ""))[:255] or None))
                        facts_count += 1
        except Exception as exc:
            LOG.warning("Could not download %s (%s): %s", meta["tracing"], url, exc)
    db.commit()
    return created, files, facts_count

def reprocess_symbol(symbol, limit=5000):
    """Re-run the latest cell/text extractor on already archived source files."""
    totals = {"symbol": symbol, "versions_checked": 0, "versions_processed": 0,
              "content_items_added": 0, "failed": 0}
    with Session(engine) as db:
        versions = db.scalars(select(models.DocumentVersion).join(models.CodalAnnouncement)
            .where(models.CodalAnnouncement.symbol_ticker == symbol)
            .order_by(models.DocumentVersion.id).limit(limit)).all()
        for version in versions:
            totals["versions_checked"] += 1
            try:
                path = Path(version.storage_path)
                if not path.exists():
                    totals["failed"] += 1
                    continue
                existing_locators = set(db.scalars(select(models.DocumentContentItem.locator)
                    .where(models.DocumentContentItem.document_version_id == version.id)).all())
                extracted = extract_content_items(path)
                added = 0
                for locator, kind, value in extracted:
                    if locator in existing_locators:
                        continue
                    db.add(models.DocumentContentItem(document_version_id=version.id,
                        locator=locator, content_kind=kind, content_text=value))
                    added += 1
                db.commit()
                totals["versions_processed"] += 1
                totals["content_items_added"] += added
            except Exception:
                db.rollback()
                totals["failed"] += 1
                LOG.exception("Reprocessing failed for document version %s", version.id)
    return totals

def sync_symbol(symbol, max_pages, backfill):
    totals = {"symbol": symbol, "pages": 0, "rows_seen": 0, "announcements_added": 0, "files_added": 0, "facts_added": 0}
    headers = {"User-Agent": "Mozilla/5.0 IranStockIntelligence/1.0"}
    with httpx.Client(timeout=45, follow_redirects=True, headers=headers) as client:
        for page in range(1, max_pages + 1):
            response = client.get(SEARCH_URL, params={"Symbol": symbol, "PageNumber": page, "PageSize": PAGE_SIZE})
            response.raise_for_status()
            rows = parse_rows(response)
            if not rows:
                break
            totals["pages"] += 1
            totals["rows_seen"] += len(rows)
            with Session(engine) as db:
                for row in rows:
                    if not isinstance(row, dict):
                        continue
                    try:
                        a, f, n = archive_row(db, client, symbol, row)
                        totals["announcements_added"] += a
                        totals["files_added"] += f
                        totals["facts_added"] += n
                    except Exception:
                        db.rollback()
                        LOG.exception("Failed to archive row for %s", symbol)
            if not backfill:
                break
            time.sleep(3.0)
    return totals

def configured_symbols():
    if SYMBOLS:
        return SYMBOLS
    with Session(engine) as db:
        tickers = set(db.scalars(select(models.Symbol.ticker)).all())
        # Historical instruments are included so delisted/renamed companies can still be backfilled.
        try:
            tickers.update(db.scalars(select(models.SymbolHistory.ticker)).all())
        except Exception:
            pass
    return sorted(tickers) or ["ساروم"]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--backfill", action="store_true")
    parser.add_argument("--pages", type=int, default=1000)
    parser.add_argument("--loop", action="store_true")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--reprocess", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(message)s")
    Base.metadata.create_all(bind=engine)
    if args.reprocess:
        symbols = configured_symbols()
        LOG.info("Codal archive symbol universe: %s symbols", len(symbols))
        for symbol in symbols:
            LOG.info("Reprocess result: %s", reprocess_symbol(symbol, limit=args.pages * PAGE_SIZE))
        return
    while True:
        # Refresh the universe each cycle so new TSETMC imports are picked up without rebuilding.
        symbols = configured_symbols()
        LOG.info("Codal archive symbol universe: %s symbols", len(symbols))
        for symbol in symbols:
            try:
                pages = args.pages if args.backfill else int(os.getenv("CODAL_INCREMENTAL_PAGES", "3"))
                LOG.info("Archive result: %s", sync_symbol(symbol, pages, args.backfill))
            except Exception:
                LOG.exception("Codal sync failed for %s", symbol)
        if args.once or args.backfill or not args.loop:
            break
        time.sleep(INTERVAL)

if __name__ == "__main__":
    main()
