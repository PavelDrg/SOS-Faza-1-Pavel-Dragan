"""Configuratia regulilor, citita din hub."""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from .normalize import iso_date, to_decimal


@dataclass
class VatRate:
    rate: Decimal
    valid_from: date | None
    valid_to: date | None

    def accepts(self, rate: Decimal, day: date) -> bool:
        return (self.rate == rate
                and (self.valid_from is None or self.valid_from <= day)
                and (self.valid_to is None or day <= self.valid_to))


@dataclass
class Config:
    auto_approval_limit: dict[str, Decimal]
    amount_tolerance: Decimal
    vat_rates: list[VatRate]
    notification_matrix: dict[str, list[dict]]
    recipients: dict[str, str]
    three_way_match: bool = False

    @classmethod
    def from_json(cls, data: dict) -> "Config":
        return cls(
            auto_approval_limit={currency: to_decimal(limit) for currency, limit in data["auto_approval_limit"].items()},
            amount_tolerance=to_decimal(data["amount_tolerance"]),
            vat_rates=[VatRate(to_decimal(entry["rate"]), _optional_date(entry.get("valid_from")),
                               _optional_date(entry.get("valid_to"))) for entry in data["vat_rates"]],
            notification_matrix=data.get("notification_matrix") or {},
            recipients=data.get("recipients") or {},
            three_way_match=(data.get("features") or {}).get("three_way_match") is True,
        )


def _optional_date(value):
    return iso_date(value) if value else None
