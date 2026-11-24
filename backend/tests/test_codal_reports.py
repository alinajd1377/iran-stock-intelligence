from app.services.codal_reports import discover_sheets, parse_html_tables


def test_discover_all_sheet_ids_from_decision_page():
    html = '<a href="Decision.aspx?LetterSerial=abc&sheetId=1">one</a><a href="?sheetId=3">three</a><script>var sheetId=2;</script>'
    assert discover_sheets(html) == [1, 2, 3]


def test_parse_html_tables_preserves_headers_and_rows():
    html = """
    <table><caption>صورت سود و زیان</caption>
      <tr><th>شرح</th><th>مبلغ</th></tr>
      <tr><td>فروش</td><td>۱۲۳۴</td></tr>
      <tr><td>بهای تمام‌شده</td><td>(۴۵۶)</td></tr>
    </table>
    <table><tr><td>یادداشت</td><td>۲</td></tr></table>
    """
    tables = parse_html_tables(html)
    assert len(tables) == 2
    assert tables[0]["caption"] == "صورت سود و زیان"
    assert tables[0]["headers"] == ["شرح", "مبلغ"]
    assert tables[0]["rows"][0] == ["فروش", "۱۲۳۴"]
    assert tables[0]["rows"][1] == ["بهای تمام‌شده", "(۴۵۶)"]
    assert tables[1]["rows"] == []
