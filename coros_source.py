#!/usr/bin/env python3
"""
COROS side of the bridge: list new activities and fetch their FIT files.

Both hops are HTTP calls to coros-mcp, which owns the COROS API — regional host
discovery, token caching, the 1019 re-login path, per-request retries and the
two-hop signed-URL export. None of that is reimplemented here, and neither are
COROS credentials held here: this module speaks only to a service on this
machine.

    list  ──► GET /api/v1/activities/live?start_day=&end_day=
    fetch ──► GET /api/v1/activities/{label_id}/file?sport_type=

Both are answered from COROS itself rather than coros-mcp's local mirror, which
matters: that mirror is refreshed by a downloader on its own schedule, so an
activity recorded in the last hour — exactly what an hourly run exists to
catch — is not in it yet. `/activities/live` exists for that reason, and the
file route takes an explicit `sport_type` for the same one, so an activity the
mirror has never seen still exports.
"""

import hashlib
from typing import Any, Dict, List, Optional, Tuple

import requests

# A FIT file carries its signature at bytes 8..12. coros-mcp checks this too;
# it is repeated here because the bytes crossed a network in between.
FIT_SIGNATURE = b".FIT"

LIVE_PATH = "/api/v1/activities/live"
FILE_PATH = "/api/v1/activities/{label_id}/file"
HEALTH_PATH = "/api/v1/health"


class SourceError(RuntimeError):
    """Raised when COROS cannot be reached or answers with something unusable."""


def _headers(api_token: Optional[str]) -> Dict[str, str]:
    return {"Authorization": f"Bearer {api_token}"} if api_token else {}


def _get(url: str, base_url: str, api_token: Optional[str], timeout: int,
         params: Optional[Dict[str, Any]] = None, what: str = "COROS"):
    """One GET against coros-mcp, with its failures phrased as SourceError."""
    try:
        resp = requests.get(url, params=params, headers=_headers(api_token),
                            timeout=timeout)
    except requests.RequestException as e:
        raise SourceError(f"coros-mcp unreachable at {base_url} "
                          f"while fetching {what}: {e}") from e

    if resp.status_code != 200:
        try:
            message = (resp.json() or {}).get("error")
        except ValueError:
            message = resp.text[:200]
        raise SourceError(f"coros-mcp returned HTTP {resp.status_code} "
                          f"for {what}: {message}")
    return resp


def health(base_url: str, api_token: Optional[str] = None,
           timeout: int = 10) -> dict:
    """
    Check the coros-mcp service before a run tries to fetch anything.

    Unauthenticated on the server side, so this says the service is up, not
    that the token is right — a wrong token surfaces on the first real call.
    """
    resp = _get(base_url.rstrip("/") + HEALTH_PATH, base_url, None, timeout,
                what="health")
    try:
        return resp.json()
    except ValueError:
        raise SourceError("coros-mcp health check did not answer with JSON")


def list_activities(start_day: int, end_day: int, base_url: str,
                    api_token: Optional[str] = None,
                    timeout: int = 120) -> List[Dict[str, Any]]:
    """
    Every activity COROS reports in [start_day, end_day], as raw list items.

    Dates are COROS's integer YYYYMMDD. Paging is the service's problem: it
    reads every page before answering, so a short list here means COROS had
    nothing more, never that the walk stopped early.
    """
    resp = _get(base_url.rstrip("/") + LIVE_PATH, base_url, api_token, timeout,
                params={"start_day": start_day, "end_day": end_day},
                what=f"the activity list {start_day}..{end_day}")
    try:
        body = resp.json() or {}
    except ValueError:
        raise SourceError("coros-mcp did not answer the activity list with JSON")

    activities = body.get("activities")
    if activities is None:
        raise SourceError("coros-mcp answered the activity list without an "
                          "`activities` field")
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


def download_fit(label_id: str, sport_type: int, base_url: str,
                 api_token: Optional[str] = None,
                 timeout: int = 180) -> Tuple[bytes, str]:
    """
    Download one activity's original FIT file. Returns (bytes, sha256).

    The sha256 is verified against the service's X-Coros-Sha256 header when it
    is present, so a truncated response is caught here rather than becoming a
    mysterious conversion failure one hop later.
    """
    resp = _get(base_url.rstrip("/") + FILE_PATH.format(label_id=label_id),
                base_url, api_token, timeout,
                params={"sport_type": sport_type, "file_type": "fit"},
                what=f"the FIT file for {label_id}")

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
