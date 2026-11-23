"""Transparent deterministic scenario projections; not an ML forecast."""
def project(base_value: float, annual_growth: float, periods: int = 3) -> dict:
    if periods < 1 or periods > 10: raise ValueError("periods must be between 1 and 10")
    if annual_growth <= -1: raise ValueError("annual_growth must be greater than -1")
    values, current = [], float(base_value)
    for year in range(1, periods + 1):
        current *= 1 + annual_growth
        values.append({"period": year, "value": round(current, 4)})
    return {"base_value": base_value, "annual_growth": annual_growth, "periods": values,
            "disclaimer": "سناریوی مکانیکی بر اساس نرخ رشد ورودی است؛ پیش‌بینی آماری یا توصیه سرمایه‌گذاری نیست."}
