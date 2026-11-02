"""Citirea facturilor XML UBL si PDF."""

import io
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from pypdf import PdfReader

from .normalize import normalize_cui, normalize_iban, normalize_invoice_number, parse_date, parse_number


class ExtractionError(Exception):
    """Documentul este ilizibil (A-R01); mesajul spune de ce."""


@dataclass
class InvoiceLine:
    quantity: Decimal
    unit_price: Decimal
    amount: Decimal
    vat_rate: Decimal
    item_code: str | None = None


@dataclass
class Invoice:
    invoice_number: str
    issue_date: date
    due_date: date
    delivery_date: date | None
    supplier_cui: str
    iban: str
    currency: str
    po_number: str | None
    lines: list[InvoiceLine]
    net_total: Decimal
    vat_total: Decimal
    gross_total: Decimal


@dataclass
class _Format:
    decimal_sep: str
    thousands_sep: str | None
    date_format: str


_MANDATORY = ("number", "issue_date", "due_date", "supplier_cui", "iban", "currency",
              "net_total", "vat_total", "gross_total")


def extract_invoice(content: bytes) -> Invoice:
    if content.lstrip().startswith(b"%PDF"):
        fields, lines, number_format = _read_pdf(content)
    else:
        fields, lines, number_format = _read_xml(content)
    return _build_invoice(fields, lines, number_format)


_UBL = {
    "cac": "urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2",
    "cbc": "urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2",
}
# Căi relative la Invoice. TVA-ul vine din singurul TaxTotal, nu din TaxSubtotal.
_XML_FIELDS = {
    "number": "cbc:ID",
    "issue_date": "cbc:IssueDate",
    "due_date": "cbc:DueDate",
    "currency": "cbc:DocumentCurrencyCode",
    "po_number": "cac:OrderReference/cbc:ID",
    "delivery_date": "cac:Delivery/cbc:ActualDeliveryDate",
    "supplier_cui": "cac:AccountingSupplierParty/cac:Party/cac:PartyTaxScheme/cbc:CompanyID",
    "iban": "cac:PaymentMeans/cac:PayeeFinancialAccount/cbc:ID",
    "vat_total": "cac:TaxTotal/cbc:TaxAmount",
    "net_total": "cac:LegalMonetaryTotal/cbc:TaxExclusiveAmount",
    "gross_total": "cac:LegalMonetaryTotal/cbc:TaxInclusiveAmount",
}
_XML_LINE = ("cbc:InvoicedQuantity", "cac:Price/cbc:PriceAmount", "cbc:LineExtensionAmount",
             "cac:Item/cac:ClassifiedTaxCategory/cbc:Percent", "cac:Item/cac:SellersItemIdentification/cbc:ID")
_XML_FORMAT = _Format(".", None, "%Y-%m-%d")


def _xml_text(element, path):
    found = element.find(path, _UBL)
    if found is None or found.text is None:
        return None
    return found.text.strip() or None


def _read_xml(content):
    try:
        root = ET.fromstring(content)
    except ET.ParseError as err:
        raise ExtractionError(f"XML invalid ({err})") from None
    fields = {key: _xml_text(root, path) for key, path in _XML_FIELDS.items()}
    lines = [tuple(_xml_text(line, path) for path in _XML_LINE) for line in root.findall("cac:InvoiceLine", _UBL)]
    return fields, lines, _XML_FORMAT


_PDF_TEMPLATES = {
    "FACTURA": (_Format(",", ".", "%d.%m.%Y"), {
        "Numar factura": "number", "Data emiterii": "issue_date", "Data scadentei": "due_date",
        "Data livrarii": "delivery_date", "CUI furnizor": "supplier_cui", "IBAN furnizor": "iban",
        "Comanda": "po_number", "Moneda": "currency", "Total fara TVA": "net_total",
        "Total TVA": "vat_total", "Total de plata": "gross_total",
    }),
    "INVOICE": (_Format(".", ",", "%Y-%m-%d"), {
        "Invoice number": "number", "Issue date": "issue_date", "Due date": "due_date",
        "Delivery date": "delivery_date", "Supplier VAT ID": "supplier_cui", "Supplier IBAN": "iban",
        "Purchase order": "po_number", "Currency": "currency", "Net total": "net_total",
        "VAT total": "vat_total", "Total due": "gross_total",
    }),
    "FACTURA / INVOICE": (_Format(".", ",", "%d.%m.%Y"), {
        "Numar / Number": "number", "Data / Date": "issue_date", "Scadenta / Due": "due_date",
        "Livrare / Delivery": "delivery_date", "CUI / VAT ID": "supplier_cui", "IBAN": "iban",
        "Comanda / PO": "po_number", "Moneda / Currency": "currency", "Total net / Net total": "net_total",
        "TVA / VAT": "vat_total", "Total / Total due": "gross_total",
    }),
}
_NOTES_LABELS = {"Mentiuni", "Notes", "Mentiuni / Notes"}
_PARTY_LABELS = ("Supplier: ", "Supplier VAT ID: ", "Supplier IBAN: ", "Customer: ", "Customer VAT ID: ")


def _read_pdf(content):
    try:
        reader = PdfReader(io.BytesIO(content))
        text = "\n".join(page.extract_text() or "" for page in reader.pages)
    except Exception as err:
        raise ExtractionError(f"PDF ilizibil ({err})") from None
    rows = [row for row in text.splitlines() if row.strip()]
    if not rows:
        raise ExtractionError("PDF fara text")
    title = " ".join(rows[0].split())
    if title not in _PDF_TEMPLATES:
        raise ExtractionError(f"sablon PDF necunoscut: {title!r}")
    number_format, labels = _PDF_TEMPLATES[title]

    fields, lines = {}, []
    for row in rows[1:]:
        if " | " in row:
            cells = [cell.strip() for cell in row.split(" | ")]
            if cells[0].isdigit():
                if len(cells) != 7:
                    raise ExtractionError(f"rand de linie cu {len(cells)} celule in loc de 7")
                lines.append((cells[3], cells[4], cells[5], cells[6].replace("%", "").strip(), cells[1]))
            continue
        if row.split(": ", 1)[0].strip() in _NOTES_LABELS:
            continue
        for part in _split_columns(row):
            label, separator, value = part.partition(": ")
            key = labels.get(label.strip())
            if separator and key and key not in fields:
                fields[key] = value.strip() or None
    return fields, lines, number_format


def _split_columns(row):
    """Separa coloanele PDF si cand extractorul comprima spatiile."""
    parts = []
    for segment in re.split(r"\s{2,}", row.strip()):
        cuts = [position for position in (segment.find(label, 1) for label in _PARTY_LABELS) if position > 0]
        if cuts:
            parts += [segment[:min(cuts)].strip(), segment[min(cuts):].strip()]
        else:
            parts.append(segment)
    return parts


def _build_invoice(fields, lines, number_format):
    missing = [key for key in _MANDATORY if not fields.get(key)]
    if missing:
        raise ExtractionError("lipsesc campurile: " + ", ".join(missing))
    if not lines:
        raise ExtractionError("factura nu are nicio linie")

    def number(text, what):
        if not text:
            raise ExtractionError(f"lipseste {what}")
        try:
            return parse_number(text, number_format.decimal_sep, number_format.thousands_sep)
        except ValueError:
            raise ExtractionError(f"{what} nu este numar: {text!r}") from None

    def day(text, what):
        try:
            return parse_date(text, number_format.date_format)
        except ValueError:
            raise ExtractionError(f"{what} nu este o data valida: {text!r}") from None

    supplier_cui = normalize_cui(fields["supplier_cui"])
    if not re.fullmatch(r"[0-9]{2,10}", supplier_cui):
        raise ExtractionError(f"CUI-ul furnizorului nu poate fi citit: {fields['supplier_cui']!r}")

    invoice_lines = []
    for index, (quantity, unit_price, amount, vat_rate, item_code) in enumerate(lines, start=1):
        invoice_lines.append(InvoiceLine(
            quantity=number(quantity, f"cantitatea liniei {index}"),
            unit_price=number(unit_price, f"pretul unitar al liniei {index}"),
            amount=number(amount, f"valoarea liniei {index}"),
            vat_rate=number(vat_rate, f"cota TVA a liniei {index}"),
            item_code=item_code,
        ))

    delivery = fields.get("delivery_date")
    return Invoice(
        invoice_number=normalize_invoice_number(fields["number"]),
        issue_date=day(fields["issue_date"], "data emiterii"),
        due_date=day(fields["due_date"], "data scadentei"),
        delivery_date=day(delivery, "data livrarii") if delivery else None,
        supplier_cui=supplier_cui,
        iban=normalize_iban(fields["iban"]),
        currency=fields["currency"].strip().upper(),
        po_number=fields.get("po_number") or None,
        lines=invoice_lines,
        net_total=number(fields["net_total"], "totalul net"),
        vat_total=number(fields["vat_total"], "totalul TVA"),
        gross_total=number(fields["gross_total"], "totalul de plata"),
    )
