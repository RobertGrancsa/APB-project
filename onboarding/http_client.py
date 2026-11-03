from __future__ import annotations

import http.client
import json
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable


CLIENT_TIMEOUT_SECONDS = 10.0
MAX_ATTEMPTS = 3
MAX_RATE_LIMIT_WAITS = 10
DEFAULT_RETRY_AFTER_SECONDS = 1.0
RETRYABLE_HTTP_STATUSES = frozenset({500, 502, 503, 504})


class HubError(Exception):
    """Base exception for hub communication errors."""


class FatalHubError(HubError):
    """Unrecoverable hub error requiring immediate non-zero exit without further writes."""


class ProvisioningStepError(HubError):
    """Permanent HTTP 500 failure on one of the four provisioning writes (rule B-R13)."""


class IdempotencyKeyReusedError(HubError):
    """HTTP 422 IDEMPOTENCY_KEY_REUSED returned when a key was already bound to another body."""


def _parse_retry_after(headers: Any) -> float:
    if headers is None:
        return DEFAULT_RETRY_AFTER_SECONDS
    raw = headers.get("Retry-After")
    if raw is None:
        return DEFAULT_RETRY_AFTER_SECONDS
    try:
        val = float(str(raw).strip())
        if val >= 0:
            return val
    except ValueError:
        pass
    return DEFAULT_RETRY_AFTER_SECONDS


def _parse_error_payload(raw_bytes: bytes) -> tuple[str, str]:
    if not raw_bytes:
        return "", ""
    try:
        parsed = json.loads(raw_bytes.decode("utf-8", errors="replace"))
        if isinstance(parsed, dict):
            err = parsed.get("error")
            if isinstance(err, dict):
                return str(err.get("code") or ""), str(err.get("message") or "")
    except ValueError:
        pass
    return "", raw_bytes.decode("utf-8", errors="replace")[:300]


class HubClient:
    """Resilient HTTP client implementing the APB retry, timeout, and idempotency policies."""

    def __init__(
        self,
        base_url: str,
        logger: Callable[[str], None] | None = None,
        sleep_fn: Callable[[float], None] = time.sleep,
        timeout: float = CLIENT_TIMEOUT_SECONDS,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.logger = logger or (lambda msg: print(msg, flush=True))
        self.sleep_fn = sleep_fn
        self.timeout = timeout

    def get(self, path: str, params: dict[str, str] | None = None) -> Any:
        full_path = path
        if params:
            encoded = urllib.parse.urlencode(params)
            if encoded:
                sep = "&" if "?" in path else "?"
                full_path = f"{path}{sep}{encoded}"
        return self._request("GET", full_path)

    def post(
        self,
        path: str,
        body: dict[str, Any],
        idempotency_key: str,
        *,
        allow_provisioning_failure: bool = False,
    ) -> Any:
        if not idempotency_key:
            raise ValueError("Idempotency-Key is mandatory on all POST requests")
        return self._request(
            "POST",
            path,
            body=body,
            idempotency_key=idempotency_key,
            allow_provisioning_failure=allow_provisioning_failure,
        )

    def _request(
        self,
        method: str,
        path: str,
        *,
        body: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
        allow_provisioning_failure: bool = False,
    ) -> Any:
        url = f"{self.base_url}{path}"
        headers: dict[str, str] = {
            "Accept": "application/json",
            "User-Agent": "apb-project-b-phase1/1.0",
        }
        payload_bytes: bytes | None = None
        if body is not None:
            payload_bytes = json.dumps(body, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if idempotency_key is not None:
            headers["Idempotency-Key"] = idempotency_key

        attempt = 1
        rate_limit_waits = 0

        while True:
            req = urllib.request.Request(url, data=payload_bytes, headers=headers, method=method)
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    raw = resp.read()
                    if not raw:
                        return None
                    return json.loads(raw.decode("utf-8"))
            except urllib.error.HTTPError as err:
                raw_err = err.read()
                err_code, err_msg = _parse_error_payload(raw_err)
                wait_s = _parse_retry_after(err.headers)

                if err.code == 429:
                    if err_code == "RUN_REQUEST_LIMIT":
                        raise FatalHubError(
                            f"429 RUN_REQUEST_LIMIT on {method} {path}: {err_msg}"
                        ) from None
                    if rate_limit_waits < MAX_RATE_LIMIT_WAITS:
                        rate_limit_waits += 1
                        self.logger(
                            f"RETRY {method} {path} status=429 code={err_code or 'RATE_LIMITED'} "
                            f"rate_wait={rate_limit_waits}/{MAX_RATE_LIMIT_WAITS} sleep={wait_s}s"
                        )
                        self.sleep_fn(wait_s)
                        continue
                    raise FatalHubError(
                        f"429 RATE_LIMITED exceeded {MAX_RATE_LIMIT_WAITS} waits on {method} {path}"
                    ) from None

                if err.code == 422 and err_code == "IDEMPOTENCY_KEY_REUSED":
                    raise IdempotencyKeyReusedError(
                        f"422 IDEMPOTENCY_KEY_REUSED on {method} {path} key={idempotency_key}: {err_msg}"
                    ) from None

                if err.code in RETRYABLE_HTTP_STATUSES:
                    if attempt < MAX_ATTEMPTS:
                        next_attempt = attempt + 1
                        self.logger(
                            f"RETRY {method} {path} status={err.code} code={err_code or 'HTTP_5XX'} "
                            f"attempt={next_attempt}/{MAX_ATTEMPTS} sleep={wait_s}s"
                        )
                        self.sleep_fn(wait_s)
                        attempt = next_attempt
                        continue
                    if allow_provisioning_failure and err.code == 500:
                        raise ProvisioningStepError(
                            f"{method} {path} failed permanently after {MAX_ATTEMPTS} attempts "
                            f"with HTTP 500 ({err_code})"
                        ) from None
                    raise FatalHubError(
                        f"{method} {path} failed after {MAX_ATTEMPTS} attempts "
                        f"with HTTP {err.code} ({err_code}: {err_msg})"
                    ) from None

                raise FatalHubError(
                    f"Non-retryable HTTP {err.code} on {method} {path} ({err_code}: {err_msg})"
                ) from None

            except (
                socket.timeout,
                TimeoutError,
                urllib.error.URLError,
                http.client.HTTPException,
                ConnectionError,
                OSError,
            ) as exc:
                if attempt < MAX_ATTEMPTS:
                    next_attempt = attempt + 1
                    self.logger(
                        f"RETRY {method} {path} error={type(exc).__name__} "
                        f"attempt={next_attempt}/{MAX_ATTEMPTS} sleep={DEFAULT_RETRY_AFTER_SECONDS}s"
                    )
                    self.sleep_fn(DEFAULT_RETRY_AFTER_SECONDS)
                    attempt = next_attempt
                    continue
                raise FatalHubError(
                    f"Network/timeout error on {method} {path} after {MAX_ATTEMPTS} attempts: {exc}"
                ) from None
