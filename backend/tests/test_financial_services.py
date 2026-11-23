from app.services.financial_parser import _number
from app.services.scenario import project

def test_number_parses_persian_financial_separators():
    assert _number("1,234,567") == 1234567
    assert _number("(12,500)") == -12500
    assert _number("-250") == -250
    assert _number("-") is None

def test_scenario_projection_is_deterministic():
    result = project(100, 0.1, 2)
    assert result["periods"] == [{"period": 1, "value": 110.0}, {"period": 2, "value": 121.0}]

def test_scenario_rejects_invalid_horizon():
    try:
        project(100, 0.05, 0)
    except ValueError:
        return
    assert False, "Expected invalid horizon to raise ValueError"
