import pytest
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


class TestThousandsSeparators:
    """Gli importi hanno due decimali: un solo separatore + 3 cifre sono le migliaia."""

    @pytest.mark.parametrize("text,expected", [
        ("50.000", "50000"), ("€50.000", "50000"), ("1.250", "1250"), ("999,000", "999000"),
        ("1.000.000", "1000000"), ("1,000,000", "1000000"), ("49.000", "49000"),
    ])
    def test_single_separator_with_three_digits_is_thousands(self, text, expected):
        assert parse_amount(text).data == Decimal(expected)

    @pytest.mark.parametrize("text,expected", [
        ("0.10", "0.10"), ("0,10", "0.10"), ("114,36", "114.36"), ("1.234,56", "1234.56"),
        ("5,000.00", "5000.00"), ("99.998,00", "99998.00"), ("50000.00", "50000.00"), ("0.125", "0.125"),
        ("0,125", "0.125"), ("10.5", "10.5"), ("12.345.67", "12345.67"),
    ])
    def test_everything_else_is_unchanged(self, text, expected):
        assert parse_amount(text).data == Decimal(expected)


@pytest.mark.parametrize("text,expected", [
    ("49 999.90", "49999.90"), ("50 000.00", "50000.00"), ("6 405 921.88", "6405921.88"),
    ("49\u00a0999,90 EUR", "49999.90"), ("\u20ac 1 250", "1250"), ("-49 999.90", "-49999.90"),
    ("S0 000.00", "50000.00"),           # correzione OCR S->5 dentro l'importo con spazio
    ("0.10 0.20", "0.10"), ("10 25", "10"), ("1234 567", "1234"),
])
def test_thousands_separated_by_a_space(text, expected):
    result = parse_amount(text)
    assert result.success and result.data == Decimal(expected)
