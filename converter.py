#!/usr/bin/env python3
"""
HTTP client for the fit-manager conversion service.

The device rewrite lives in fit_targeted_editor.py, which the bridge reaches
over HTTP rather than importing. That keeps one copy of the editor running as
one service — already deployed, already restarted on change — instead of a
second copy pinned to whatever the bridge's venv happens to hold.

The service's /api/v1/convert answers with the FIT bytes directly and uses
status codes that separate "this file cannot be converted" (422) from "the
service is broken" (5xx), which is exactly the distinction the bridge needs to
decide whether an activity should be retried.
"""

import hashlib
from typing import Optional, Tuple

import requests

# Only /api/v1 is used. The unversioned /api/convert cannot express a
# manufacturer other than Garmin, cannot set a serial, and reports business
# failures as HTTP 500.
CONVERT_PATH = "/api/v1/convert"
HEALTH_PATH = "/api/v1/health"


class ConversionError(RuntimeError):
    """
    A conversion that did not produce a file.

    `retryable` is False when the service refused the file itself (a 4xx), and
    True when the service was unreachable or faulted — the difference between
    an activity that will never convert and one that might next hour.
    """

    def __init__(self, message: str, status: Optional[int] = None,
                 reason: Optional[str] = None, retryable: bool = True):
        super().__init__(message)
        self.status = status
        self.reason = reason
        self.retryable = retryable


def health(base_url: str, api_key: Optional[str] = None,
           timeout: int = 10) -> dict:
    """
    Check the conversion service before a run does any work.

    Failing here costs an activity nothing; failing per-activity would burn a
    retry attempt on every candidate because a service was down.
    """
    headers = {"X-API-Key": api_key} if api_key else {}
    try:
        resp = requests.get(base_url.rstrip("/") + HEALTH_PATH,
                            headers=headers, timeout=timeout)
    except requests.RequestException as e:
        raise ConversionError(f"Conversion service unreachable at {base_url}: {e}",
                              retryable=True) from e
    if resp.status_code != 200:
        raise ConversionError(
            f"Conversion service health check returned HTTP {resp.status_code}",
            status=resp.status_code, retryable=True)
    return resp.json()


def convert(data: bytes, filename: str, base_url: str, manufacturer_id: int,
            product_id: int, serial_number: Optional[int] = None,
            api_key: Optional[str] = None,
            timeout: int = 180) -> Tuple[bytes, str]:
    """
    Rewrite a FIT file's recording device. Returns (bytes, sha256).

    Args:
        data: the FIT file as downloaded from COROS
        filename: name to send, only used for the multipart part and logging
        base_url: e.g. http://127.0.0.1:7077
        manufacturer_id: FIT manufacturer id (1 = garmin)
        product_id: product id for that manufacturer (4536 = fenix 8)
        serial_number: serial to claim; preserving a real registered device's
            serial is the point of the whole exercise, so this is normally set
        api_key: sent as X-API-Key when the service requires one
    """
    form = {
        "manufacturer_id": str(manufacturer_id),
        "product_id": str(product_id),
    }
    if serial_number is not None:
        form["serial_number"] = str(serial_number)

    headers = {"X-API-Key": api_key} if api_key else {}

    try:
        resp = requests.post(
            base_url.rstrip("/") + CONVERT_PATH,
            files={"file": (filename, data, "application/octet-stream")},
            data=form,
            headers=headers,
            timeout=timeout,
        )
    except requests.RequestException as e:
        raise ConversionError(f"Conversion service unreachable: {e}",
                              retryable=True) from e

    if resp.status_code == 200:
        converted = resp.content
        if len(converted) < 14 or converted[8:12] != b".FIT":
            raise ConversionError(
                "Conversion service returned something that is not a FIT file",
                status=200, retryable=True)
        return converted, hashlib.sha256(converted).hexdigest()

    # Errors are JSON: {"error": ..., "reason": ..., "log": [...]}.
    reason = message = None
    try:
        body = resp.json()
        reason = body.get("reason")
        message = body.get("error")
        log = body.get("log") or []
        if log:
            message = f"{message} ({log[-1]})"
    except ValueError:
        message = resp.text[:200]

    raise ConversionError(
        f"HTTP {resp.status_code}: {message}",
        status=resp.status_code,
        reason=reason,
        # A 4xx is a verdict on this file, not on the service.
        retryable=resp.status_code >= 500,
    )
