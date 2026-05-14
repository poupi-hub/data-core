from decimal import Decimal

from app.modules.ecommerce.normalizers.product_normalizer import _parse_decimal


def test_parse_decimal_handles_brl_thousands_without_cents():
    assert _parse_decimal("R$ 1.234") == Decimal("1234")


def test_parse_decimal_handles_brl_thousands_with_cents():
    assert _parse_decimal("R$ 1.234,56") == Decimal("1234.56")


def test_parse_decimal_keeps_dot_decimal_prices():
    assert _parse_decimal("12.99") == Decimal("12.99")
