#!/usr/bin/env python3
"""
Garmin side of the bridge: upload a FIT file, over HTTP to garmin-mcp.

The bridge used to import garmin-mcp's `authenticate()`, hold its own garth
session and classify Garmin's answers itself. All three now live behind
garmin-mcp's `POST /api/v1/upload/fit` (`garmin_files.py` there), for the same
reason the FIT editor lives behind fit-manager: one process owns the
credentials, one process holds the session, and the bridge carries no copy of
either.

What that buys, concretely: garth is no longer a dependency here, the hourly
cron run cannot race the daily sync over the same session file, and Garmin's
upload semantics — which took a while to pin down — are written down once.

The service answers 200 for anything Garmin actually decided, with the verdict
in `status`:

    uploaded    Garmin took it
    duplicate   Garmin already has this activity
    failed      Garmin refused it, `message` says why

A 4xx means the request was wrong and a 5xx means Garmin never answered. Only
the latter is worth retrying, and only the latter aborts a run.
"""

from dataclasses import dataclass
from typing import Any, Dict, Optional

import requests

UPLOAD_PATH = "/api/v1/upload/fit"
HEALTH_PATH = "/api/v1/upload/health"

# Garmin's import endpoint answers 202 and decides asynchronously, so the
# service is normally quick — but it may be renewing an OAuth token underneath.
UPLOAD_TIMEOUT = 180


class SinkError(RuntimeError):
    """Raised when the upload service, or Garmin behind it, cannot be reached."""


@dataclass
class UploadResult:
    """Outcome of one upload, already mapped onto a bridge activity status."""
    status: str                       # uploaded | duplicate | failed
    upload_id: Optional[str] = None
    activity_id: Optional[str] = None
    message: Optional[str] = None
    http_status: Optional[int] = None


def _headers(api_token: Optional[str]) -> Dict[str, str]:
    return {"Authorization": f"Bearer {api_token}"} if api_token else {}


def _error_text(resp: Any) -> str:
    try:
        return str((resp.json() or {}).get("error") or resp.text[:200])
    except ValueError:
        return resp.text[:200]


def health(base_url: str, api_token: Optional[str] = None,
           timeout: int = 30) -> dict:
    """
    Check that an upload would reach Garmin, before a run does any work.

    This probes the Garmin session, not just the service, which is why it can
    take a moment on the first call after a restart. Failing here costs an
    activity nothing; failing per-activity would burn a retry attempt on every
    candidate because a session had expired.
    """
    try:
        resp = requests.get(base_url.rstrip("/") + HEALTH_PATH,
                            headers=_headers(api_token), timeout=timeout)
    except requests.RequestException as e:
        raise SinkError(f"garmin-mcp unreachable at {base_url}: {e}") from e

    if resp.status_code == 401:
        raise SinkError(
            f"garmin-mcp rejected the token at {base_url} — check "
            "BRIDGE_GARMIN_API_TOKEN against GARMIN_MCP_AUTH_TOKEN")
    if resp.status_code != 200:
        raise SinkError(f"garmin-mcp is not ready to upload: "
                        f"HTTP {resp.status_code}: {_error_text(resp)}")
    try:
        return resp.json()
    except ValueError:
        raise SinkError("garmin-mcp health check did not answer with JSON")


def authenticate(base_url: str, api_token: Optional[str] = None) -> None:
    """
    Preflight the Garmin path.

    Kept under the old name because it plays the same part in a run: prove the
    Garmin side works before the first activity is touched. The credentials
    themselves now live in garmin-mcp, so there is nothing to authenticate
    here — only something to confirm.
    """
    health(base_url, api_token)


def upload_fit(data: bytes, filename: str, base_url: str,
               api_token: Optional[str] = None,
               timeout: int = UPLOAD_TIMEOUT) -> UploadResult:
    """
    Upload one FIT file to Garmin Connect, via garmin-mcp.

    Raises SinkError when the service or Garmin never answered — that is a
    verdict on the run, not on the file. Everything Garmin decided comes back
    as an UploadResult, including a refusal.
    """
    try:
        resp = requests.post(
            base_url.rstrip("/") + UPLOAD_PATH,
            files={"file": (filename, data, "application/octet-stream")},
            headers=_headers(api_token),
            timeout=timeout,
        )
    except requests.RequestException as e:
        raise SinkError(f"garmin-mcp unreachable while uploading "
                        f"{filename}: {e}") from e

    if resp.status_code >= 500:
        # The service faulted, or Garmin did not answer it. Nothing is known
        # about the file, so the run stops rather than marking it failed.
        raise SinkError(f"garmin-mcp returned HTTP {resp.status_code}: "
                        f"{_error_text(resp)}")

    if resp.status_code != 200:
        # A 4xx is a verdict on this request — a malformed file, a bad token.
        # It will not improve on retry, so it belongs to the activity.
        return UploadResult(status="failed", http_status=resp.status_code,
                            message=f"garmin-mcp rejected the request: "
                                    f"{_error_text(resp)}")

    try:
        body = resp.json() or {}
    except ValueError:
        raise SinkError("garmin-mcp did not answer an upload with JSON")

    status = body.get("status")
    if status not in ("uploaded", "duplicate", "failed"):
        raise SinkError(f"garmin-mcp reported an unknown status {status!r}")

    return UploadResult(
        status=status,
        upload_id=body.get("upload_id"),
        activity_id=body.get("activity_id"),
        message=body.get("message"),
        http_status=body.get("http_status"),
    )
