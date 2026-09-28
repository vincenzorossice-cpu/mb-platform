from decimal import Decimal

from orchestrator.modules.slot_autopilot.value_parser import parse_amount


def test_currency_symbol_and_spaces():
    result = parse_amount("€ 114,36")
    assert result.success
    assert result.data == Decimal("114.36")


def test_american_thousands_format():
    result = parse_amount("5,000.00")
    assert result.success
    assert result.data == Decimal("5000.00")


def test_european_thousands_format():
    result = parse_amount("1.234,56")
    assert result.success
    assert result.data == Decimal("1234.56")


def test_ocr_confusion_o_for_zero():
    result = parse_amount("O.50")
    assert result.success
    assert result.data == Decimal("0.50")


def test_ocr_confusion_l_for_one():
    result = parse_amount("l0,00")
    assert result.success
    assert result.data == Decimal("10.00")


def test_ocr_confusion_s_for_five_with_currency_suffix():
    result = parse_amount("S.00 EUR")
    assert result.success
    assert result.data == Decimal("5.00")


def test_negative_amount():
    result = parse_amount("-2,50")
    assert result.success
    assert result.data == Decimal("-2.50")


def test_no_digits_fails():
    result = parse_amount("SALDO")
    assert not result.success
    assert result.error_code == "AMOUNT_NOT_FOUND"


def test_empty_string_fails():
    result = parse_amount("")
    assert not result.success
    assert result.error_code == "AMOUNT_NOT_FOUND"


def test_single_dot_ambiguous_case():
    result = parse_amount("1.50")
    assert result.success
    assert result.data == Decimal("1.50")


def test_garbage_fails_gracefully():
    result = parse_amount("???")
    assert not result.success
    assert result.error_code == "AMOUNT_NOT_FOUND"
