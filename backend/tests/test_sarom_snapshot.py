import json
from pathlib import Path


def test_sarom_snapshot_reconciles_product_sales():
    snapshot_path = Path(__file__).parents[1] / "app" / "data" / "sarom_snapshot.json"
    data = json.loads(snapshot_path.read_text(encoding="utf-8"))
    product_total = sum(item["sales_amount"] for item in data["six_month_product_revenue"])
    assert product_total == data["financials_6m_1405_audited"]["revenue"]
    assert data["sales_channels_6m"]["domestic_sales"] + data["sales_channels_6m"]["export_sales"] == data["sales_channels_6m"]["total_sales"]


def test_sarom_audited_and_unaudited_filings_remain_distinct():
    snapshot_path = Path(__file__).parents[1] / "app" / "data" / "sarom_snapshot.json"
    data = json.loads(snapshot_path.read_text(encoding="utf-8"))
    assert data["financials_6m_1405_audited"]["tracing_no"] != data["financials_6m_1405_unaudited"]["tracing_no"]
    assert data["financials_6m_1405_audited"]["net_income"] != data["financials_6m_1405_unaudited"]["net_income"]
