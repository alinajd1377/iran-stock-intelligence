"""TGJU daily USD/IRR history adapter. No OCR or inferred prices are used."""
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
import re
import httpx

TGJU_HISTORY_URL = "https://api.tgju.org/v1/market/indicator/summary-table-data/price_dollar_rl?lang=fa&order_dir=asc"
TGJU_PROFILE_URL = "https://www.tgju.org/profile/price_dollar_rl"


def _number(value):
    if value is None:
        return None
    raw = str(value).strip().replace(",", "").replace("٬", "").replace("٫", ".")
    raw = re.sub(r"[^0-9.\-]", "", raw)
    if not raw:
        return None
    try:
        return Decimal(raw)
    except InvalidOperation:
        return None


def _date(value):
    if isinstance(value, date):
        return value
    raw = str(value or "").strip()
    for fmt in ("%Y/%m/%d", "%Y-%m-%d", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(raw[:19], fmt).date()
        except ValueError:
            pass
    return None


def _records(payload):
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        value = payload.get("data") or payload.get("Data")
        if isinstance(value, list):
            return value
    return []


def normalize_history(payload):
    """TGJU row order: open, low, high, close, change, change%, Gregorian date, Jalali date."""
    output = []
    for row in _records(payload):
        if isinstance(row, (list, tuple)):
            if len(row) < 8:
                continue
            raw = {"open": row[0], "low": row[1], "high": row[2], "close": row[3],
                   "change": row[4], "change_percent": row[5], "date": row[6], "jalali_date": row[7]}
        elif isinstance(row, dict):
            raw = row
        else:
            continue
        day = _date(raw.get("date") or raw.get("Date") or raw.get("gregorian_date") or raw.get("Gregorian_Date"))
        open_price = _number(raw.get("open") or raw.get("Open"))
        low = _number(raw.get("low") or raw.get("Low"))
        high = _number(raw.get("high") or raw.get("High"))
        close = _number(raw.get("close") or raw.get("Close") or raw.get("price") or raw.get("value"))
        if day is None or close is None or close <= 0:
            continue
        output.append({"rate_date": day, "rate": close, "open": open_price, "low": low, "high": high,
                       "close": close, "jalali_date": str(raw.get("jalali_date") or raw.get("Persian_Date") or ""),
                       "raw": raw})
    by_day = {item["rate_date"]: item for item in output}
    return [by_day[k] for k in sorted(by_day)]


async def fetch_history(timeout=60):
    headers = {"User-Agent": "Mozilla/5.0 IranStockIntelligence/1.0", "Referer": TGJU_PROFILE_URL}
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True, headers=headers) as client:
        response = await client.get(TGJU_HISTORY_URL)
        response.raise_for_status()
        payload = response.json()
    rows = normalize_history(payload)
    if not rows:
        raise ValueError("TGJU پاسخ داد، اما ساختار ردیف‌های تاریخچه قابل تشخیص نبود؛ داده‌ای ذخیره نشد.")
    return rows, payload
