"""Codal Decision page workbook extraction with sheet and table lineage."""
import hashlib
import re
import time
from urllib.parse import parse_qs, urlparse
import httpx
from sqlalchemy import select
from app import models

BASE = "https://www.codal.ir"
EXCEL_BASE = "https://excel.codal.ir/service/Excel/GetAll/"

def _serial(payload):
    payload = payload or {}
    for key in ("LetterSerial", "letterSerial", "letter_serial"):
        if payload.get(key):
            return str(payload[key])
    for key, value in payload.items():
        if str(key).lower() in ("url", "excelurl", "pdfurl", "htmlurl") and value:
            query = parse_qs(urlparse(str(value)).query)
            for name, values in query.items():
                if name.lower() == "letterserial" and values:
                    return values[0]
    return None

def _letter_type(payload):
    payload = payload or {}
    for key in ("LetterType", "letterType", "LetterTypeId", "letter_type"):
        value = payload.get(key)
        if value not in (None, ""):
            try:
                return int(value)
            except (TypeError, ValueError):
                pass
    for value in payload.values():
        if isinstance(value, str) and "LetterSerial=" in value:
            query = parse_qs(urlparse(value).query)
            for name, values in query.items():
                if name.lower() == "let" and values:
                    try:
                        return int(values[0])
                    except ValueError:
                        pass
    return 6

def discover_sheets(html):
    found = {}
    patterns = [r"[?&]sheetId=([0-9]+)", r"sheetId[^0-9]{0,10}([0-9]+)", r"SheetId[^0-9]{0,10}([0-9]+)"]
    for pattern in patterns:
        for value in re.findall(pattern, html, re.I): found[int(value)] = True
    if not found: found[1] = True
    return sorted(found)

def parse_html_tables(html):
    from html.parser import HTMLParser
    class Tables(HTMLParser):
        def __init__(self):
            super().__init__(); self.tables=[]; self.table=None; self.row=None; self.cell=None; self.caption=None; self.in_caption=False
        def handle_starttag(self, tag, attrs):
            if tag.lower()=="table": self.table={"caption":None,"rows":[]}; self.tables.append(self.table)
            elif self.table is not None and tag.lower()=="tr": self.row=[]; self.table["rows"].append(self.row)
            elif self.table is not None and tag.lower() in ("td","th"): self.cell=[]
            elif self.table is not None and tag.lower()=="caption": self.in_caption=True; self.caption=[]
        def handle_endtag(self, tag):
            if tag.lower() in ("td","th") and self.cell is not None:
                self.row.append(" ".join("".join(self.cell).split())); self.cell=None
            elif tag.lower()=="caption" and self.in_caption:
                self.table["caption"]=" ".join("".join(self.caption).split()); self.in_caption=False
            elif tag.lower()=="table": self.table=None
        def handle_data(self,data):
            if self.cell is not None: self.cell.append(data)
            if self.in_caption and self.caption is not None: self.caption.append(data)
    parser=Tables(); parser.feed(html)
    output=[]
    for table in parser.tables:
        rows=[r for r in table["rows"] if any(str(c).strip() for c in r)]
        if rows: output.append({"caption":table["caption"],"headers":rows[0],"rows":rows[1:] if len(rows)>1 else [],"row_count":len(rows),"column_count":max(map(len,rows),default=0)})
    return output

def extract_announcement(db, announcement_id, client=None, delay_seconds=2.0):
    announcement=db.get(models.CodalAnnouncement,announcement_id)
    if not announcement: raise ValueError("announcement not found")
    payload = announcement.raw_payload or {}
    serial=_serial(payload)
    if not serial: raise ValueError("LetterSerial is not present in announcement payload; re-sync this filing to persist its serial")
    letter_type = _letter_type(payload)
    own=client is None
    client=client or httpx.Client(timeout=45,follow_redirects=True,headers={"User-Agent":"Mozilla/5.0 IranStockIntelligence/1.0"})
    total={"letter_serial":serial,"sheets_found":0,"sheets_saved":0,"tables_saved":0,"rows_saved":0}
    try:
        decision=f"{BASE}/Reports/Decision.aspx"
        response=client.get(decision,params={"LetterSerial":serial,"rt":0,"let":letter_type,"ct":0,"ft":-1,"sheetId":1})
        response.raise_for_status()
        sheet_ids=discover_sheets(response.text)
        total["sheets_found"]=len(sheet_ids)
        for idx,sheet_id in enumerate(sheet_ids):
            url=f"{decision}?LetterSerial={serial}&rt=0&let={letter_type}&ct=0&ft=-1&sheetId={sheet_id}"
            if sheet_id == 1:
                page=response
            else:
                time.sleep(delay_seconds)
                page=client.get(url); page.raise_for_status()
            tables=parse_html_tables(page.text)
            if not tables:
                workbook=client.get(f"{EXCEL_BASE}{serial}/0")
                if workbook.status_code == 200:
                    fallback_tables=parse_html_tables(workbook.text)
                    if fallback_tables:
                        tables=fallback_tables
                        page=workbook
            digest=hashlib.sha256(page.content).hexdigest()
            sheet=db.scalar(select(models.CodalReportSheet).where(models.CodalReportSheet.announcement_id==announcement_id,models.CodalReportSheet.sheet_id==sheet_id))
            if sheet is None:
                sheet=models.CodalReportSheet(announcement_id=announcement_id,sheet_id=sheet_id,sheet_title=f"Sheet {sheet_id}",source_url=url,content_hash=digest,status="extracted")
                db.add(sheet); db.flush(); total["sheets_saved"]+=1
            elif sheet.content_hash!=digest:
                sheet.content_hash=digest; sheet.source_url=url; sheet.status="updated"
                db.query(models.CodalReportTable).filter_by(sheet_record_id=sheet.id).delete(synchronize_session=False)
            for table_index, table in enumerate(tables, start=1):
                existing=db.scalar(select(models.CodalReportTable).where(models.CodalReportTable.sheet_record_id==sheet.id,models.CodalReportTable.table_index==table_index))
                if existing is None:
                    existing=models.CodalReportTable(sheet_record_id=sheet.id,table_index=table_index,caption=table["caption"],headers=table["headers"],rows=table["rows"],row_count=table["row_count"],column_count=table["column_count"])
                    db.add(existing)
                else:
                    existing.caption=table["caption"]
                    existing.headers=table["headers"]
                    existing.rows=table["rows"]
                    existing.row_count=table["row_count"]
                    existing.column_count=table["column_count"]
                total["tables_saved"]+=1
                total["rows_saved"]+=table["row_count"]
        db.commit()
        return total
    except Exception:
        db.rollback(); raise
    finally:
        if own: client.close()
