"""Normalizarea identificatorilor si conversiile numerice."""

import re
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal

CENT = Decimal("0.01")
_NUMBER = re.compile(r"[0-9]+(\.[0-9]+)?")


def normalize_cui(value: str) -> str:
    compact = re.sub(r"\s+", "", value).upper()
    return compact[2:] if compact.startswith("RO") else compact


def normalize_invoice_number(value: str) -> str:
    """Elimina separatorii, apoi zerourile initiale din fiecare grup de cifre."""
    compact = re.sub(r"[^A-Z0-9]", "", value.upper())
    return re.sub(r"[0-9]+", lambda digits: str(int(digits.group())), compact)


def normalize_iban(value: str) -> str:
    return re.sub(r"\s+", "", value).upper()


def parse_number(text: str, decimal_sep: str = ".", thousands_sep: str | None = None) -> Decimal:
    cleaned = text.strip()
    if thousands_sep:
        cleaned = cleaned.replace(thousands_sep, "")
    cleaned = cleaned.replace(decimal_sep, ".")
    if not _NUMBER.fullmatch(cleaned):
        raise ValueError(f"nu este numar: {text!r}")
    return Decimal(cleaned)


def parse_date(text: str, date_format: str) -> date:
    return datetime.strptime(text.strip(), date_format).date()


def iso_date(text: str) -> date:
    return date.fromisoformat(text[:10])


def to_decimal(value) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(str(value))


def round2(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def money(value: Decimal) -> str:
    return str(round2(value))
