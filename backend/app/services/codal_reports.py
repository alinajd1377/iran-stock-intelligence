"""Codal Decision page workbook extraction with sheet and table lineage."""
import hashlib
import json
import re
import time
from urllib.parse import urljoin
import httpx
from sqlalchemy import select
from app import models

BASE = "https://www.codal.ir"
EXCEL_BASE = "https://excel.codal.ir/service/Excel/GetAll/"

def _serial(payload):
    for key, value in (payload or {}).items():
        if str(key).lower() in ("url", "excelurl", "pdfurl", "htmlurl") and value:
            match = re.search(r"(?:LetterSerial|letterSerial)=([^&]+)", str(value), re.I)
            if match:
                from urllib.parse import unquote
                return unquote(match.group(1))
    for key in ("LetterSerial", "letterSerial", "letter_serial"):
        if payload.get(key): return str(payload[key])
    return None

def discover_sheets(html):
    found = {}
    patterns = [r"[?&]sheetId=(\\d+)", r"sheetId\\s*[:=]\\s*['\\\"]?(\\d+)", r"SheetId\\s*[:=]\\s*['\\\"]?(\\d+)"]
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
    serial=_serial(announcement.raw_payload or {})
    if not serial: raise ValueError("LetterSerial is not present in announcement payload")
    own=client is None
    client=client or httpx.Client(timeout=45,follow_redirects=True,headers={"User-Agent":"Mozilla/5.0 IranStockIntelligence/1.0"})
    total={"letter_serial":serial,"sheets_found":0,"sheets_saved":0,"tables_saved":0,"rows_saved":0}
    try:
        decision=f"{BASE}/Reports/Decision.aspx"
        response=client.get(decision,params={"LetterSerial":serial,"rt":0,"let":6,"ct":0,"ft":-1,"sheetId":1})
        response.raise_for_status()
        sheet_ids=discover_sheets(response.text)
        total["sheets_found"]=len(sheet_ids)
        for idx,sheet_id in enumerate(sheet_ids):
            if idx: time.sleep(delay_seconds)
            url=f"{decision}?LetterSerial={serial}&rt=0&let=6&ct=0&ft=-1&sheetId={sheet_id}"
            page=client.get(url); page.raise_for_status()
            tables=parse_html_tables(page.text)
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
                    db.add(existing); total["tables_saved"]+=1; total["rows_saved"]+=table["row_count"]
        db.commit()
        return total
    except Exception:
        db.rollback(); raise
    finally:
        if own: client.close()
