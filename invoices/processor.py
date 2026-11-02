"""Procesarea facturilor, notificarile si raportul."""

from dataclasses import dataclass
from decimal import Decimal

from .config import Config
from .extraction import ExtractionError, Invoice, extract_invoice
from .hub import HubError, RetriesExhausted, path_part
from .log import log
from .normalize import iso_date, money, to_decimal
from .rules import check_rules, invoice_quantities, outcome_for

MAX_REPORT_KEYS = 50


@dataclass
class Decision:
    document_id: str
    outcome: str
    reason_codes: list[str]
    invoice: Invoice | None
    supplier: dict | None = None
    note: str = ""

    def body(self) -> dict:
        invoice = self.invoice
        return {
            "document_id": self.document_id,
            "outcome": self.outcome,
            "reason_codes": self.reason_codes,
            "supplier_cui": invoice.supplier_cui if invoice else None,
            "invoice_number": invoice.invoice_number if invoice else None,
            "po_number": invoice.po_number if invoice else None,
            "currency": invoice.currency if invoice else None,
            "net_amount": money(invoice.net_total) if invoice else None,
            "gross_amount": money(invoice.gross_total) if invoice else None,
        }


class InvoiceProcessor:
    def __init__(self, client):
        self.client = client

    def run(self) -> None:
        self.business_date = iso_date(self.client.get_json("/context")["business_date"])
        self.config = Config.from_json(self.client.get_json("/config"))
        inbox = self.client.get_json("/inbox")
        history = self.client.get_json("/history")
        self.positions = {item["document_id"]: index for index, item in enumerate(inbox)}
        self.this_run = [decision for decision in history if decision["source"] == "THIS_RUN"]
        self.quantity_cache = {}
        log(f"[START] data de lucru {self.business_date}, {len(inbox)} documente in inbox, "
            f"{len(history)} decizii in istoric ({len(self.this_run)} din aceasta rulare)")

        report = [self.process(item["document_id"], index) for index, item in enumerate(inbox)]
        self.send_report(report)


    def process(self, document_id: str, index: int) -> dict:
        recorded = self.client.get_json("/history", params={"document_id": document_id})
        earlier = [decision for decision in recorded if decision["source"] == "EARLIER_RUN"]
        if earlier:
            return self.finish(document_id, "ALREADY_PROCESSED", earlier[0]["outcome"], earlier[0]["reason_codes"], [])
        this_run = [decision for decision in recorded if decision["source"] == "THIS_RUN"]
        if this_run:
            return self.complete_recorded(this_run[0])

        decision = self.decide(document_id, index)
        status = self.record(decision)
        if status == "KEY_REUSED":
            this_run = [d for d in self.client.get_json("/history", params={"document_id": document_id})
                        if d["source"] == "THIS_RUN"]
            if not this_run:
                raise HubError(f"POST /decisions pentru {document_id}: cheie refolosita, dar decizia lipseste din istoric")
            return self.complete_recorded(this_run[0])
        if status == "FAILED":
            return self.finish(document_id, "FAILED", decision.outcome, decision.reason_codes, [],
                               "POST /decisions a raspuns 500 la toate cele 3 incercari")
        notified = self.send_notifications(document_id, decision.outcome, decision.reason_codes, decision.supplier)
        return self.finish(document_id, "RECORDED", decision.outcome, decision.reason_codes, notified, decision.note)

    def decide(self, document_id: str, index: int) -> Decision:
        content = self.client.get_bytes(f"/documents/{path_part(document_id)}")
        try:
            invoice = extract_invoice(content)
        except ExtractionError as err:
            return Decision(document_id, "MANUAL_REVIEW", ["EXTRACTION_FAILED"], None, note=str(err))

        if self.is_duplicate(document_id, index, invoice):
            supplier = self.find_supplier(invoice.supplier_cui) if self.notifies_supplier("DUPLICATE") else None
            return Decision(document_id, "DUPLICATE", ["DUPLICATE_INVOICE"], invoice, supplier)

        supplier = self.find_supplier(invoice.supplier_cui)
        po = self.find_po(invoice.po_number) if invoice.po_number else None
        later_net = self.later_approved_net(invoice.po_number, index)
        available = None
        if self.config.three_way_match and po is not None:
            available = self.available_quantities(invoice.po_number, index)
            if available is None:
                po = None
        codes = check_rules(invoice, supplier, po, later_net, self.business_date, self.config, available)
        return Decision(document_id, outcome_for(codes), codes, invoice, supplier)

    def is_duplicate(self, document_id: str, index: int, invoice: Invoice) -> bool:
        """Ignora deciziile THIS_RUN ulterioare pozitiei curente."""
        matches = self.client.get_json("/history", params={"supplier_cui": invoice.supplier_cui,
                                                           "invoice_number": invoice.invoice_number})
        return any(decision["document_id"] != document_id
                   and decision["outcome"] != "REJECTED"
                   and not (decision["source"] == "THIS_RUN" and self.is_after(decision["document_id"], index))
                   for decision in matches)

    def later_approved_net(self, po_number: str | None, index: int) -> Decimal:
        """Reface soldul de la pozitia curenta, fara aprobarile ulterioare."""
        return sum((to_decimal(decision["net_amount"]) for decision in self.this_run
                    if decision["outcome"] == "APPROVED" and decision["po_number"] == po_number
                    and self.is_after(decision["document_id"], index)), Decimal(0))

    def is_after(self, document_id: str, index: int) -> bool:
        return self.positions.get(document_id, -1) > index

    def available_quantities(self, po_number: str, index: int) -> dict[str, Decimal] | None:
        """Reface cantitatile disponibile de la pozitia curenta."""
        later = {}
        for decision in self.this_run:
            if (decision["outcome"] != "APPROVED" or decision["po_number"] != po_number
                    or not self.is_after(decision["document_id"], index)):
                continue
            document_id = decision["document_id"]
            if document_id not in self.quantity_cache:
                # Istoricul nu conține liniile; recitim factura aprobată, fără altă decizie.
                content = self.client.get_bytes(f"/documents/{path_part(document_id)}")
                try:
                    self.quantity_cache[document_id] = invoice_quantities(extract_invoice(content))
                except ExtractionError as err:
                    raise HubError(f"Nu pot reciti cantitatile facturii aprobate {document_id}: {err}") from None
            for code, quantity in self.quantity_cache[document_id].items():
                later[code] = later.get(code, Decimal(0)) + quantity

        receipts = self.client.get_json("/goods-receipts", params={"po_number": po_number}, allow_404=True)
        if receipts is None:
            return None
        # Hub-ul întoarce {po_number, items: [...]}; acceptăm și lista descrisă în enunț.
        if isinstance(receipts, dict):
            receipts = receipts.get("items")
        if not isinstance(receipts, list):
            raise HubError("GET /goods-receipts: raspuns invalid, lipseste lista de articole")
        available = {entry["item_code"]: to_decimal(entry["received_quantity"]) - to_decimal(entry["invoiced_quantity"])
                     for entry in receipts}
        for code, quantity in later.items():
            available[code] = available.get(code, Decimal(0)) + quantity
        return available

    def find_supplier(self, cui: str) -> dict | None:
        return self.client.get_json(f"/suppliers/{path_part(cui)}", allow_404=True)

    def find_po(self, po_number: str) -> dict | None:
        return self.client.get_json(f"/purchase-orders/{path_part(po_number)}", allow_404=True)


    def record(self, decision: Decision) -> str:
        try:
            status, code = self.client.post("/decisions", decision.body(), key=f"{decision.document_id}:decision")
        except RetriesExhausted as err:
            if err.all_500:
                return "FAILED"
            raise  # un 503 sau un timeout poate însemna că scrierea s-a făcut: ne oprim, repornirea termină
        if status == 201:
            return "RECORDED"
        if status == 422 and code == "IDEMPOTENCY_KEY_REUSED":
            return "KEY_REUSED"
        raise HubError(f"POST /decisions pentru {decision.document_id}: HTTP {status} {code or ''}")

    def complete_recorded(self, decision: dict) -> dict:
        document_id = decision["document_id"]
        sent = {n["recipient_role"] for n in self.client.get_json("/notifications", params={"document_id": document_id})}
        supplier = None
        if decision["supplier_cui"] and "SUPPLIER" not in sent and self.notifies_supplier(decision["outcome"]):
            supplier = self.find_supplier(decision["supplier_cui"])
        notified = self.send_notifications(document_id, decision["outcome"], decision["reason_codes"], supplier, sent)
        return self.finish(document_id, "RECORDED", decision["outcome"], decision["reason_codes"], notified,
                           "decizie din pornirea anterioara")

    def notifies_supplier(self, outcome: str) -> bool:
        return any(entry["recipient_role"] == "SUPPLIER" for entry in self.config.notification_matrix.get(outcome) or [])

    def send_notifications(self, document_id: str, outcome: str, reason_codes: list[str], supplier: dict | None,
                           already_sent: set[str] = frozenset()) -> list[str]:
        notified = []
        for entry in self.config.notification_matrix.get(outcome) or []:
            role = entry["recipient_role"]
            if role in already_sent:
                continue
            if role == "SUPPLIER":
                if supplier is None:
                    continue
                recipient = supplier["email"]
            else:
                recipient = self.config.recipients[role]
            body = {"document_id": document_id, "recipient_role": role, "recipient": recipient,
                    "template": entry["template"], "reason_codes": list(reason_codes)}
            status, code = self.client.post("/notifications", body, key=f"{document_id}:notification:{role}")
            if status != 201 and not (status == 422 and code == "IDEMPOTENCY_KEY_REUSED"):
                raise HubError(f"POST /notifications pentru {document_id} / {role}: HTTP {status} {code or ''}")
            notified.append(role)
        return notified

    def send_report(self, items: list[dict]) -> None:
        for number in range(1, MAX_REPORT_KEYS + 1):
            key = "report" if number == 1 else f"report-{number}"
            status, code = self.client.post("/report", {"items": items}, key=key)
            if status == 200:
                log(f"[RAPORT] trimis cu cheia {key}: {len(items)} intrari")
                return
            if not (status == 422 and code == "IDEMPOTENCY_KEY_REUSED"):
                raise HubError(f"POST /report cu cheia {key}: HTTP {status} {code or ''}")
        raise HubError(f"POST /report: toate cele {MAX_REPORT_KEYS} chei au fost refolosite")


    def finish(self, document_id: str, status: str, outcome: str, reason_codes: list[str], notified: list[str],
               note: str = "") -> dict:
        line = (f"[DOCUMENT] {document_id}: {status} {outcome} coduri=[{', '.join(reason_codes)}] "
                f"notificari=[{', '.join(notified)}]")
        log(line + (f" ({note})" if note else ""))
        return {"document_id": document_id, "status": status, "outcome": outcome, "reason_codes": list(reason_codes)}
