"""Client HTTP cu reincercari si chei de idempotenta."""

import time
from decimal import Decimal
from urllib.parse import quote, urlencode

import requests

from .log import log

TIMEOUT_SECONDS = 10
MAX_ATTEMPTS = 3
MAX_RATE_LIMIT_WAITS = 10
RETRYABLE_STATUSES = (500, 502, 503, 504)
MIN_SECONDS_BETWEEN_REQUESTS = 0.06


class HubError(Exception):
    """Un apel a eșuat definitiv: soluția nu mai scrie nimic și iese cu cod diferit de 0."""


class RetriesExhausted(HubError):

    def __init__(self, message: str, failures: list[int | None]):
        super().__init__(message)
        self.failures = failures

    @property
    def all_500(self) -> bool:
        return len(self.failures) == MAX_ATTEMPTS and all(status == 500 for status in self.failures)


def path_part(value: str) -> str:
    return quote(value, safe="")


def error_code(response) -> str | None:
    try:
        return response.json()["error"]["code"]
    except (ValueError, KeyError, TypeError):
        return None


def _retry_after(response) -> float:
    try:
        return max(0.0, float(response.headers.get("Retry-After", "")))
    except ValueError:
        return 1.0


class HubClient:
    def __init__(self, base_url: str, session=None, sleep=time.sleep,
                 min_interval: float = MIN_SECONDS_BETWEEN_REQUESTS):
        self.base_url = base_url.rstrip("/")
        self.session = session or requests.Session()
        self.sleep = sleep
        self.min_interval = min_interval
        self._last_request = 0.0

    def get_json(self, path: str, params: dict | None = None, allow_404: bool = False):
        response = self._send("GET", path, params=params)
        if response.status_code == 404 and allow_404:
            return None
        if response.status_code != 200:
            raise HubError(f"GET {_target(path, params)}: HTTP {response.status_code} {error_code(response) or ''}")
        return response.json(parse_float=Decimal)

    def get_bytes(self, path: str) -> bytes:
        response = self._send("GET", path)
        if response.status_code != 200:
            raise HubError(f"GET {path}: HTTP {response.status_code} {error_code(response) or ''}")
        return response.content

    def post(self, path: str, body: dict, key: str) -> tuple[int, str | None]:
        response = self._send("POST", path, body=body, key=key)
        return response.status_code, error_code(response) if response.status_code >= 400 else None

    def _send(self, method, path, params=None, body=None, key=None):
        target = f"{method} {_target(path, params)}"
        headers = {"Idempotency-Key": key} if key else {}
        failures = []
        rate_limit_waits = 0
        while True:
            self._throttle()
            try:
                response = self.session.request(method, self.base_url + path, params=params, json=body,
                                                headers=headers, timeout=TIMEOUT_SECONDS)
            except requests.RequestException as err:
                failures.append(None)
                reason, wait = f"eroare de retea sau timeout ({type(err).__name__})", 1.0
            else:
                if response.status_code == 429:
                    code = error_code(response)
                    if code == "RUN_REQUEST_LIMIT":
                        raise HubError(f"{target}: 429 RUN_REQUEST_LIMIT, rularea si-a epuizat cererile")
                    if rate_limit_waits == MAX_RATE_LIMIT_WAITS:
                        raise HubError(f"{target}: 429 si dupa {MAX_RATE_LIMIT_WAITS} asteptari")
                    rate_limit_waits += 1
                    wait = _retry_after(response)
                    log(f"[REINCERCARE] {target}: 429 {code or ''}, astept {wait:g}s "
                        f"(asteptarea {rate_limit_waits} din {MAX_RATE_LIMIT_WAITS}, nu consuma o incercare)")
                    self.sleep(wait)
                    continue
                if response.status_code not in RETRYABLE_STATUSES:
                    return response
                failures.append(response.status_code)
                reason = f"HTTP {response.status_code} {error_code(response) or ''}".strip()
                wait = _retry_after(response)
            if len(failures) == MAX_ATTEMPTS:
                raise RetriesExhausted(f"{target}: toate cele {MAX_ATTEMPTS} incercari au esuat ({reason})", failures)
            log(f"[REINCERCARE] {target}: {reason}; incercarea {len(failures)} din {MAX_ATTEMPTS} a esuat, "
                f"astept {wait:g}s")
            self.sleep(wait)

    def _throttle(self):
        elapsed = time.monotonic() - self._last_request
        if elapsed < self.min_interval:
            time.sleep(self.min_interval - elapsed)
        self._last_request = time.monotonic()


def _target(path, params):
    return path + ("?" + urlencode(params) if params else "")
