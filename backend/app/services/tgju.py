"""TGJU daily USD/IRR history adapter. No OCR or inferred prices are used."""
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
import re
import httpx

TGJU_HISTORY_URL = "https://api.tgju.org/v1/market/indicator/summary-table-data/price_dollar_rl"
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
    if isinstance(value, (int, float)):
        # TGJU payloads may use Unix seconds or milliseconds.
        stamp = float(value)
        if stamp > 10_000_000_000:
            stamp /= 1000
        return datetime.fromtimestamp(stamp).date()
    raw = str(value or "").strip()
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y-%m-%dT%H:%M:%S", "%Y/%m/%d %H:%M:%S"):
        try:
            return datetime.strptime(raw[:19], fmt).date()
        except ValueError:
            pass
    return None


def _records(payload):
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("data", "Data", "items", "Items", "history", "History", "result", "Result"):
            value = payload.get(key)
            if isinstance(value, list):
                return value
            if isinstance(value, dict):
                nested = _records(value)
                if nested:
                    return nested
    return []


def normalize_history(payload):
    output = []
    for row in _records(payload):
        if isinstance(row, (list, tuple)):
            # Known archive table order: date, open, low, high, close.
            if len(row) < 5:
                continue
            raw = {"date": row[0], "open": row[1], "low": row[2], "high": row[3], "close": row[4]}
        elif isinstance(row, dict):
            raw = row
        else:
            continue
        day = _date(raw.get("date") or raw.get("Date") or raw.get("gregorian_date") or raw.get("timestamp") or raw.get("time"))
        close = _number(raw.get("close") or raw.get("Close") or raw.get("price") or raw.get("value"))
        if day is None or close is None or close <= 0:
            continue
        output.append({
            "rate_date": day,
            "rate": close,
            "open": _number(raw.get("open") or raw.get("Open")),
            "low": _number(raw.get("low") or raw.get("Low")),
            "high": _number(raw.get("high") or raw.get("High")),
            "raw": raw,
        })
    # Deduplicate by date, preferring the last observation in the provider response.
    by_day = {item["rate_date"]: item for item in output}
    return [by_day[k] for k in sorted(by_day)]


async def fetch_history(timeout=45):
    headers = {"User-Agent": "Mozilla/5.0 IranStockIntelligence/1.0", "Referer": TGJU_PROFILE_URL}
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True, headers=headers) as client:
        response = await client.get(TGJU_HISTORY_URL)
        response.raise_for_status()
        payload = response.json()
    rows = normalize_history(payload)
    if not rows:
        raise ValueError("TGJU پاسخ داد، اما ساختار تاریخچه قابل تشخیص نبود؛ داده‌ای ذخیره نشد.")
    return rows, payload
