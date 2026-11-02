"""Regulile A-R02 - A-R14."""

from datetime import date
from decimal import Decimal

from .config import Config
from .extraction import Invoice
from .normalize import iso_date, normalize_cui, normalize_iban, round2, to_decimal

REJECTED_CODES = frozenset({
    "SUPPLIER_INACTIVE", "SUPPLIER_BLOCKED", "PO_SUPPLIER_MISMATCH", "PO_CLOSED",
    "CURRENCY_MISMATCH", "INVOICE_DATE_IN_FUTURE", "DUE_DATE_BEFORE_ISSUE", "VAT_RATE_INVALID",
})


def check_rules(invoice: Invoice, supplier: dict | None, po: dict | None, later_approved_net: Decimal,
                business_date: date, config: Config) -> list[str]:
    codes = set()

    if supplier is None:
        codes.add("SUPPLIER_UNKNOWN")
    else:
        if supplier["status"] == "INACTIVE":
            codes.add("SUPPLIER_INACTIVE")
        elif supplier["status"] == "BLOCKED":
            codes.add("SUPPLIER_BLOCKED")
        if invoice.iban != normalize_iban(supplier["iban"]):
            codes.add("IBAN_MISMATCH")

    if invoice.po_number is None:
        codes.add("PO_MISSING")
    elif po is None:
        codes.add("PO_NOT_FOUND")
    else:
        if supplier is not None and normalize_cui(po["supplier_cui"]) != invoice.supplier_cui:
            codes.add("PO_SUPPLIER_MISMATCH")
        if po["status"] == "CLOSED":
            codes.add("PO_CLOSED")
        if invoice.currency != po["currency"]:
            codes.add("CURRENCY_MISMATCH")
        elif invoice.net_total > to_decimal(po["remaining_balance"]) + later_approved_net:
            codes.add("PO_BALANCE_EXCEEDED")
        if invoice.issue_date < iso_date(po["order_date"]):
            codes.add("INVOICE_BEFORE_PO")

    if invoice.issue_date > business_date:
        codes.add("INVOICE_DATE_IN_FUTURE")
    if invoice.due_date < invoice.issue_date:
        codes.add("DUE_DATE_BEFORE_ISSUE")

    if has_total_mismatch(invoice, config.amount_tolerance):
        codes.add("TOTAL_MISMATCH")

    if has_invalid_vat_rate(invoice, config):
        codes.add("VAT_RATE_INVALID")

    limit = config.auto_approval_limit.get(invoice.currency)
    if limit is not None and invoice.gross_total > limit:
        codes.add("ABOVE_APPROVAL_LIMIT")

    return sorted(codes)


def recalculate(invoice: Invoice) -> tuple[list[Decimal], Decimal, Decimal, Decimal]:
    line_nets = [round2(line.quantity * line.unit_price) for line in invoice.lines]
    line_vats = [round2(net * line.vat_rate / 100) for net, line in zip(line_nets, invoice.lines)]
    net_total = sum(line_nets, Decimal(0))
    vat_total = sum(line_vats, Decimal(0))
    return line_nets, net_total, vat_total, net_total + vat_total


def has_total_mismatch(invoice: Invoice, tolerance: Decimal) -> bool:
    line_nets, net_total, vat_total, gross_total = recalculate(invoice)
    pairs = [(line.amount, net) for line, net in zip(invoice.lines, line_nets)]
    pairs += [(invoice.net_total, net_total), (invoice.vat_total, vat_total), (invoice.gross_total, gross_total)]
    return any(abs(declared - calculated) > tolerance for declared, calculated in pairs)


def has_invalid_vat_rate(invoice: Invoice, config: Config) -> bool:
    day = invoice.delivery_date or invoice.issue_date
    return any(not any(rate.accepts(line.vat_rate, day) for rate in config.vat_rates) for line in invoice.lines)


def outcome_for(codes: list[str]) -> str:
    if any(code in REJECTED_CODES for code in codes):
        return "REJECTED"
    return "MANUAL_REVIEW" if codes else "APPROVED"
