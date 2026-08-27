#!/usr/bin/env python3
"""
COROS side of the bridge: list new activities and fetch their FIT files.

The two halves reach COROS by deliberately different routes.

**Listing** goes to COROS directly, through `CorosClient` from the coros-mcp
checkout — which owns regional host discovery, token caching, the 1019
re-login path and per-request retries. It has to be live: coros-mcp's REST API
serves its local SQLite mirror, populated by a downloader on its own schedule,
so an activity recorded in the last hour is not in it yet. The bridge runs
hourly precisely to catch those, and routing the listing through the mirror
would put a sync's worth of latency in front of every upload and make the
bridge fail whenever that sync did.

**Fetching the file** goes over HTTP to coros-mcp's
`GET /api/v1/activities/{label_id}/file`, which is freshness-independent — it
asks COROS for a signed export URL and follows it, whatever the mirror knows.
The sport type is passed explicitly for exactly that reason: it comes from the
live listing above, so an activity the mirror has never seen still exports.

That leaves one copy of the two-hop export dance, in the project that owns the
COROS API, rather than a second one here — the same reasoning that keeps the
FIT editor in fit-manager and reaches it over HTTP.
"""

import os
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests

# Same convention migrate_to_coros.py uses for FIT_EDITOR_PATH: a sensible
# sibling-directory default, overridable by environment variable.
DEFAULT_COROS_MCP_PATH = Path(__file__).parent.parent / "coros-mcp"

# The list endpoint rejects sizes above 200.
PAGE_SIZE = 200

# A FIT file carries its signature at bytes 8..12. coros-mcp checks this too;
# it is repeated here because the bytes crossed a network in between.
FIT_SIGNATURE = b".FIT"

FILE_PATH = "/api/v1/activities/{label_id}/file"
HEALTH_PATH = "/api/v1/health"

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
    """Return an authenticated CorosClient, for listing."""
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


def _headers(api_token: Optional[str]) -> Dict[str, str]:
    return {"Authorization": f"Bearer {api_token}"} if api_token else {}


def health(base_url: str, api_token: Optional[str] = None,
           timeout: int = 10) -> dict:
    """
    Check the coros-mcp service before a run tries to fetch anything.

    Unauthenticated on the server side, so this says the service is up, not
    that the token is right — a wrong token surfaces on the first fetch.
    """
    try:
        resp = requests.get(base_url.rstrip("/") + HEALTH_PATH, timeout=timeout)
    except requests.RequestException as e:
        raise SourceError(f"coros-mcp unreachable at {base_url}: {e}") from e
    if resp.status_code != 200:
        raise SourceError(
            f"coros-mcp health check returned HTTP {resp.status_code}")
    try:
        return resp.json()
    except ValueError:
        raise SourceError("coros-mcp health check did not answer with JSON")


def download_fit(label_id: str, sport_type: int, base_url: str,
                 api_token: Optional[str] = None,
                 timeout: int = 180) -> Tuple[bytes, str]:
    """
    Download one activity's original FIT file. Returns (bytes, sha256).

    The sha256 comes from the service's X-Coros-Sha256 header when it is
    present and is verified against the bytes that arrived, so a truncated
    response is caught here rather than becoming a mysterious conversion
    failure one hop later.
    """
    import hashlib

    url = (base_url.rstrip("/")
           + FILE_PATH.format(label_id=label_id))
    try:
        resp = requests.get(url, params={"sport_type": sport_type,
                                         "file_type": "fit"},
                            headers=_headers(api_token), timeout=timeout)
    except requests.RequestException as e:
        raise SourceError(f"coros-mcp unreachable while fetching "
                          f"{label_id}: {e}") from e

    if resp.status_code != 200:
        message = None
        try:
            message = (resp.json() or {}).get("error")
        except ValueError:
            message = resp.text[:200]
        raise SourceError(
            f"coros-mcp returned HTTP {resp.status_code} for {label_id}: "
            f"{message}")

    content = resp.content
    if len(content) < 14 or content[8:12] != FIT_SIGNATURE:
        raise SourceError(
            f"coros-mcp returned something that is not a FIT file for "
            f"{label_id} ({len(content)} bytes, starts {content[:16]!r})")

    digest = hashlib.sha256(content).hexdigest()
    claimed = resp.headers.get("X-Coros-Sha256")
    if claimed and claimed != digest:
        raise SourceError(
            f"FIT file for {label_id} arrived corrupted: coros-mcp sent "
            f"{claimed}, {len(content)} bytes hash to {digest}")

    return content, digest
