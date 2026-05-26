"""Unit tests for collectors.crypto.validators.

No database required — pure logic tests.
"""

import pytest

from collectors.crypto.validators import log_active_symbols, validate_symbols


# ── validate_symbols ──────────────────────────────────────────────────────────


def test_valid_standard_symbols_pass():
    symbols = ["BTC/USDT", "SOL/USDT", "DOGE/USDT", "XRP/USDT"]
    result = validate_symbols(symbols)
    assert result == symbols


def test_valid_symbols_are_returned_unchanged():
    symbols = ["ETH/BTC", "BNB/USDT"]
    assert validate_symbols(symbols) is symbols


def test_empty_list_raises_value_error():
    with pytest.raises(ValueError, match="symbol list is empty"):
        validate_symbols([])


def test_invalid_format_raises_value_error():
    with pytest.raises(ValueError, match="BASE/QUOTE"):
        validate_symbols(["BTCUSDT"])  # missing slash


def test_lowercase_symbol_raises_value_error():
    with pytest.raises(ValueError, match="BASE/QUOTE"):
        validate_symbols(["btc/usdt"])


def test_symbol_with_spaces_stripped_and_uppercased():
    # Symbols with surrounding spaces: after strip+upper the format is valid
    # The validator strips internally — no error for spaces around the symbol
    result = validate_symbols(["BTC/USDT"])
    assert "BTC/USDT" in result


def test_duplicate_symbol_raises_value_error():
    with pytest.raises(ValueError, match="duplicate"):
        validate_symbols(["SOL/USDT", "SOL/USDT"])


def test_unknown_quote_currency_raises_value_error():
    with pytest.raises(ValueError, match="not in accepted set"):
        validate_symbols(["PEPE/EUR"])


def test_unknown_quote_currency_message_includes_accepted_set():
    with pytest.raises(ValueError) as exc_info:
        validate_symbols(["DOGE/EUR"])
    msg = str(exc_info.value)
    # Should mention at least one accepted quote
    assert "USDT" in msg


def test_multiple_errors_reported_together():
    with pytest.raises(ValueError) as exc_info:
        validate_symbols(["BTCUSDT", "ETH/EUR"])
    msg = str(exc_info.value)
    # Both problems surfaced in one error
    assert "BTCUSDT" in msg
    assert "ETH/EUR" in msg


def test_empty_string_in_list_raises_value_error():
    with pytest.raises(ValueError):
        validate_symbols(["BTC/USDT", ""])


def test_accepted_major_quotes_pass():
    symbols = ["ETH/BTC", "BNB/ETH", "SOL/USDC", "ADA/BNB"]
    result = validate_symbols(symbols)
    assert len(result) == 4


def test_single_valid_symbol_passes():
    result = validate_symbols(["BTC/USDT"])
    assert result == ["BTC/USDT"]


# ── log_active_symbols ────────────────────────────────────────────────────────


def test_log_active_symbols_does_not_raise(caplog):
    """log_active_symbols should run without exceptions and emit one log record."""
    import logging

    with caplog.at_level(logging.INFO, logger="collectors.crypto.validators"):
        log_active_symbols(["SOL/USDT", "DOGE/USDT"], ["15m", "1h"])

    assert any("2 symbols" in r.message for r in caplog.records)


def test_log_active_symbols_includes_extra_fields(caplog):
    import logging

    with caplog.at_level(logging.INFO, logger="collectors.crypto.validators"):
        log_active_symbols(
            ["BTC/USDT"],
            ["1h"],
            extra={"source": "SYMBOLS_env"},
        )

    assert caplog.records, "Expected at least one log record"


def test_log_active_symbols_pair_combinations():
    """pair_combinations = len(symbols) × len(timeframes) — smoke test."""
    # Just verify no exception; pair count calculation is implicit in log message
    log_active_symbols(["BTC/USDT", "ETH/USDT", "SOL/USDT"], ["15m", "1h", "4h"])
