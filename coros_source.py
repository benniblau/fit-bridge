#!/usr/bin/env python3
"""
COROS side of the bridge: list new activities and fetch their FIT files.

Nothing here re-implements the COROS API. `CorosClient` from the coros-mcp
checkout already owns regional host discovery, token caching, the 1019
re-login path and per-request retries — this module imports it and adds only
what the bridge needs on top: pagination over the activity list, and turning a
signed export URL into bytes.

The token cache is shared with coros-mcp on purpose, so the hourly bridge and
the daily sync do not each hold their own session.
"""

import hashlib
import os
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests

# Same convention migrate_to_coros.py uses for FIT_EDITOR_PATH: a sensible
# sibling-directory default, overridable by environment variable.
DEFAULT_COROS_MCP_PATH = Path(__file__).parent.parent / "coros-mcp"

# COROS export file types; 4 is the original FIT recording.
FILE_TYPE_FIT = 4

# The list endpoint rejects sizes above 200.
PAGE_SIZE = 200

# A FIT file carries its signature at bytes 8..12. Signed COROS URLs answer
# with XML on error, so this is what tells a real file from an error page.
FIT_SIGNATURE = b".FIT"

_client_module = None


def _load_coros_module():
    """Import coros_downloader from the coros-mcp checkout, once."""
    global _client_module
    if _client_module is not None:
        return _client_module

    path = Path(os.getenv("COROS_MCP_PATH", str(DEFAULT_COROS_MCP_PATH))).resolve()
    if not (path / "coros_downloader.py").exists():
        raise SourceError(
            f"Could not find coros_downloader.py under {path}. "
            "Set COROS_MCP_PATH to your coros-mcp checkout."
        )
    sys.path.insert(0, str(path))
    import coros_downloader                                       # noqa: PLC0415

    _client_module = coros_downloader
    return _client_module


class SourceError(RuntimeError):
    """Raised when COROS cannot be reached or answers with something unusable."""


def connect() -> Any:
    """Return an authenticated CorosClient."""
    module = _load_coros_module()
    try:
        client = module.CorosClient()
        client.authenticate()
    except module.CorosError as e:
        raise SourceError(str(e)) from e
    return client


@contextmanager
def _translated():
    """
    Present COROS's own exception as a SourceError.

    The bridge catches one error type per hop and decides from that whether an
    activity failed or the run should stop; leaking CorosError through would
    land in the catch-all handler and print a traceback into the cron log.
    """
    try:
        yield
    except _load_coros_module().CorosError as e:
        raise SourceError(str(e)) from e


def list_activities(client: Any, start_day: int, end_day: int) -> List[Dict[str, Any]]:
    """
    Every activity COROS reports in [start_day, end_day], as raw list items.

    Dates are COROS's integer YYYYMMDD. The list endpoint pages; a run that
    stops early would silently miss activities, so all pages are read.
    """
    activities: List[Dict[str, Any]] = []
    page = 1

    while True:
        with _translated():
            data = client.query_activities(page=page, size=PAGE_SIZE,
                                           start_day=start_day, end_day=end_day)
        if not (data.get("count") or 0):
            # COROS omits dataList and totalPage entirely when nothing matches.
            break

        batch = data.get("dataList") or []
        if not batch:
            break
        activities.extend(batch)

        total_pages = data.get("totalPage") or 1
        if page >= total_pages:
            break
        page += 1

    return activities


def summarize(activity: Dict[str, Any]) -> Dict[str, Any]:
    """The subset of a list item the bridge stores and reasons about."""
    return {
        "label_id": str(activity["labelId"]),
        "date": activity.get("date"),
        "start_time": activity.get("startTime"),
        "name": activity.get("name"),
        "sport_type": activity.get("sportType"),
        "distance": activity.get("distance"),
        "device": activity.get("device"),
    }


def download_fit(client: Any, label_id: str, sport_type: int,
                 timeout: int = 120) -> Tuple[bytes, str]:
    """
    Download one activity's original FIT file.

    Returns (bytes, sha256). COROS answers /activity/detail/download with a
    short-lived signed URL rather than the file itself, so this is two hops.
    """
    with _translated():
        data = client.download_url(label_id, sport_type, FILE_TYPE_FIT)
    url: Optional[str] = data.get("fileUrl")
    if not url:
        raise SourceError(f"COROS returned no fileUrl for {label_id}")

    try:
        resp = requests.get(url, timeout=timeout)
    except requests.RequestException as e:
        raise SourceError(f"Could not fetch the FIT file for {label_id}: {e}") from e

    if resp.status_code != 200:
        raise SourceError(
            f"Signed URL for {label_id} returned HTTP {resp.status_code}")

    content = resp.content
    if len(content) < 14 or content[8:12] != FIT_SIGNATURE:
        raise SourceError(
            f"Downloaded file for {label_id} is not a FIT file "
            f"({len(content)} bytes, starts {content[:16]!r})")

    return content, hashlib.sha256(content).hexdigest()
