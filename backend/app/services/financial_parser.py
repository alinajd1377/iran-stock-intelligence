"""Conservative parsers for raw Codal workbooks and PDFs."""
from pathlib import Path
from typing import Any
import re

LABELS = {
    "درآمدهای عملیاتی": "revenue", "درآمد عملیاتی": "revenue",
    "سود (زیان) خالص": "net_income", "سود خالص": "net_income",
    "جمع حقوق مالکانه": "equity", "جمع حقوق صاحبان سهام": "equity",
    "جمع دارایی‌ها": "total_assets", "جمع دارایی ها": "total_assets",
    "جمع بدهی‌ها": "total_liabilities", "جمع بدهی ها": "total_liabilities",
}

def _number(value: Any) -> float | None:
    if value is None: return None
    raw = str(value).replace(",", "").replace("٬", "").replace("−", "-").strip()
    if raw in {"", "-", "—", "None"}: return None
    negative = raw.startswith("(") and raw.endswith(")")
    raw = raw.strip("()")
    raw = re.sub(r"[^0-9.\\-]", "", raw)
    try:
        result = float(raw)
        return -result if negative else result
    except ValueError:
        return None

def parse_xlsx(path: str | Path) -> list[dict]:
    from openpyxl import load_workbook
    workbook = load_workbook(path, read_only=True, data_only=True)
    facts = []
    for sheet in workbook.worksheets:
        for row in sheet.iter_rows(values_only=True):
            if not row: continue
            label = str(row[0]).strip() if row[0] is not None else ""
            concept = LABELS.get(label)
            if not concept: continue
            for cell in row[1:]:
                value = _number(cell)
                if value is not None:
                    facts.append({"concept": concept, "value": value, "source_label": label, "sheet": sheet.title})
                    break
    return facts

def parse_pdf(path: str | Path) -> list[dict]:
    import pdfplumber
    facts = []
    with pdfplumber.open(path) as pdf:
        for page in pdf.pages:
            page_text = page.extract_text() or ""
            for line in page_text.splitlines():
                for label, concept in LABELS.items():
                    if label in line:
                        numbers = re.findall(r"\\(?-?[\\d,٬]+(?:\\.\\d+)?\\)?", line.replace(" ", ""))
                        if numbers:
                            value = _number(numbers[-1])
                            if value is not None:
                                facts.append({"concept": concept, "value": value, "source_label": label, "page": page.page_number})
                        break
    return facts

def parse_document(path: str | Path) -> list[dict]:
    suffix = Path(path).suffix.lower()
    if suffix in {".xlsx", ".xlsm"}: return parse_xlsx(path)
    if suffix == ".pdf": return parse_pdf(path)
    return []
