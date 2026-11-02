"""Pornirea procesarii facturilor."""

import os
import sys

from invoices.hub import HubClient, HubError
from invoices.log import log
from invoices.processor import InvoiceProcessor


def main() -> int:
    sys.stdout.reconfigure(errors="replace")
    base_url = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("BPA_BASE_URL")
    if not base_url:
        log("Utilizare: python solutie.py BASE_URL (sau variabila de mediu BPA_BASE_URL)")
        return 2
    try:
        InvoiceProcessor(HubClient(base_url)).run()
    except HubError as err:
        log(f"[EROARE] {err}. Ma opresc fara alte scrieri.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
