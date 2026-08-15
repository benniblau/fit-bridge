#!/usr/bin/env python3
"""
Garmin side of the bridge: authenticate and upload a FIT file.

Authentication reuses garmin-mcp's `authenticate()` verbatim — resume the
saved garth session, probe it with a real request, fall back to a login — so
there is exactly one Garmin credential store on the machine and one session
file to keep alive.

Upload goes to the import endpoint, /upload-service/upload/fit, rather than
anything the app uses for its own recordings. Third-party lore (documented in
garminconnect's source) holds that activities arriving this way are not
re-exported to connected services, which is what keeps these uploads from
duplicating into Strava. That claim is unverified here; the verification steps
in README.md cover it.

Classifying the answer is the fiddly part:

  2xx with successes            -> uploaded, keep internalId
  409                           -> Garmin already has this activity
  2xx with a duplicate failure  -> same thing, said differently
  anything else                 -> failed, with Garmin's own message

garth raises on non-2xx, so a duplicate arrives as an exception whose status
code has to be dug out of the wrapped response.
"""

import os
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

DEFAULT_GARMIN_MCP_PATH = Path(__file__).parent.parent / "garmin-mcp"

UPLOAD_PATH = "/upload-service/upload/fit"

# Garmin's web client sends these on uploads; without them the endpoint
# answers 403.
UPLOAD_HEADERS = {"NK": "NT", "origin": "https://sso.garmin.com"}

_export_fit = None


class SinkError(RuntimeError):
    """Raised when Garmin cannot be authenticated or reached at all."""


@dataclass
class UploadResult:
    """Outcome of one upload, already mapped onto a bridge activity status."""
    status: str                       # uploaded | duplicate | failed
    upload_id: Optional[str] = None
    activity_id: Optional[str] = None
    message: Optional[str] = None
    http_status: Optional[int] = None


def _load_export_fit():
    """Import garmin-mcp's export_fit module, once."""
    global _export_fit
    if _export_fit is not None:
        return _export_fit

    path = Path(os.getenv("GARMIN_MCP_PATH", str(DEFAULT_GARMIN_MCP_PATH))).resolve()
    if not (path / "export_fit.py").exists():
        raise SinkError(
            f"Could not find export_fit.py under {path}. "
            "Set GARMIN_MCP_PATH to your garmin-mcp checkout."
        )
    sys.path.insert(0, str(path))
    import export_fit                                             # noqa: PLC0415

    _export_fit = export_fit
    return _export_fit


def authenticate() -> None:
    """
    Establish a Garmin session, reusing garmin-mcp's saved one.

    GARTH_SESSION_PATH selects the session directory; unset, it resolves to
    garmin-mcp/.garth, which is the session the daily sync already maintains.
    """
    try:
        # The import is inside the try because export_fit exits the process at
        # import time when garth is missing, and authenticate() exits when
        # credentials are. Either would otherwise take the whole run down as a
        # SystemExit, past every handler, leaving the run row open.
        _load_export_fit().authenticate()
    except SystemExit as e:
        raise SinkError(
            "Garmin authentication failed: garth is not installed, or there is "
            "no usable session in "
            f"{os.getenv('GARTH_SESSION_PATH', str(DEFAULT_GARMIN_MCP_PATH / '.garth'))} "
            "and GARMIN_EMAIL / GARMIN_PASSWORD are not set"
        ) from e
    except SinkError:
        raise
    except Exception as e:                                        # noqa: BLE001
        raise SinkError(f"Garmin authentication failed: {e}") from e


def _http_status(err: Exception) -> Optional[int]:
    """
    Dig the HTTP status out of a garth exception.

    GarthHTTPError wraps a requests.HTTPError in `.error`, whose `.response`
    carries the status. Older shapes put the response on the exception itself.
    """
    for candidate in (getattr(getattr(err, "error", None), "response", None),
                      getattr(err, "response", None)):
        status = getattr(candidate, "status_code", None)
        if status is not None:
            return status
    return None


def _response_body(err: Exception) -> Any:
    """The JSON body of a failed response, if there is one."""
    for candidate in (getattr(getattr(err, "error", None), "response", None),
                      getattr(err, "response", None)):
        if candidate is None:
            continue
        try:
            return candidate.json()
        except Exception:                                         # noqa: BLE001
            text = getattr(candidate, "text", None)
            if text:
                return text[:300]
    return None


def _failure_message(body: Any) -> Optional[str]:
    """
    Garmin's own words for why an upload was rejected.

    The envelope is detailedImportResult.failures[0].messages[0], where the
    message is itself an object with a `content` field.
    """
    if not isinstance(body, dict):
        return str(body)[:300] if body else None

    result = body.get("detailedImportResult") or {}
    failures = result.get("failures") or []
    if not failures:
        return body.get("message") or None

    messages = failures[0].get("messages") or []
    if not messages:
        return str(failures[0])[:300]

    first = messages[0]
    if isinstance(first, dict):
        return first.get("content") or str(first)[:300]
    return str(first)[:300]


def upload_fit(data: bytes, filename: str) -> UploadResult:
    """
    Upload one FIT file to Garmin Connect.

    garth reads `fp.name` when building the multipart part, so the payload is
    written to a real file rather than handed over as a BytesIO.
    """
    import garth                                                  # noqa: PLC0415

    with tempfile.TemporaryDirectory(prefix="bridge_upload_") as tmpdir:
        path = Path(tmpdir) / filename
        path.write_bytes(data)

        try:
            with open(path, "rb") as fp:
                resp = garth.client.post(
                    "connectapi", UPLOAD_PATH,
                    files={"file": (filename, fp, "application/octet-stream")},
                    headers=UPLOAD_HEADERS,
                    api=True,
                )
        except Exception as e:                                    # noqa: BLE001
            status = _http_status(e)
            if status == 409:
                # Garmin already holds this activity. Not an error — it is the
                # expected answer to re-processing something.
                return UploadResult(status="duplicate", http_status=409,
                                    message="Garmin reported a duplicate (HTTP 409)")
            body = _response_body(e)
            return UploadResult(
                status="failed", http_status=status,
                message=_failure_message(body) or f"{type(e).__name__}: {e}")

    return _classify(resp)


def _classify(resp: Any) -> UploadResult:
    """Turn a 2xx upload response into an outcome."""
    try:
        body = resp.json()
    except Exception:                                             # noqa: BLE001
        return UploadResult(status="uploaded",
                            http_status=getattr(resp, "status_code", None),
                            message="Accepted, but the response was not JSON")

    result = (body or {}).get("detailedImportResult") or {}
    upload_id = result.get("uploadId")
    successes = result.get("successes") or []
    failures = result.get("failures") or []
    http_status = getattr(resp, "status_code", None)

    if successes:
        return UploadResult(
            status="uploaded",
            upload_id=str(upload_id) if upload_id is not None else None,
            activity_id=str(successes[0].get("internalId"))
            if successes[0].get("internalId") is not None else None,
            http_status=http_status,
        )

    message = _failure_message(body)
    if message and "duplicate" in message.lower():
        # Garmin sometimes accepts the request and reports the duplicate in the
        # body instead of answering 409.
        return UploadResult(status="duplicate",
                            upload_id=str(upload_id) if upload_id is not None else None,
                            message=message, http_status=http_status)

    if failures:
        return UploadResult(status="failed", message=message,
                            upload_id=str(upload_id) if upload_id is not None else None,
                            http_status=http_status)

    # Neither a success nor a failure: Garmin queued it without saying so.
    return UploadResult(status="uploaded",
                        upload_id=str(upload_id) if upload_id is not None else None,
                        message="Accepted with no import result reported",
                        http_status=http_status)
