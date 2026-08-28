#!/usr/bin/env python3
"""
The source side of a bridge: list new activities and fetch their FIT files.

Every source is an MCP service on this machine exposing the same two routes:

    list  ──► GET /api/v1/activities/live?start_day=&end_day=
    fetch ──► GET /api/v1/activities/{id}/file

Both answer from the upstream itself rather than that project's local mirror,
which a downloader refreshes on its own schedule: an activity recorded in the
last hour — exactly what an hourly run exists to catch — is not in the mirror
yet. This module therefore holds no upstream credentials and no upstream API
logic; each MCP owns its own.

What differs between sources is only the *shape of a list item*, because those
routes pass the upstream's payload through unchanged, deliberately. Normalising
it is this module's whole job, and it happens in one place per source:

    summarize()    raw list item      -> the fields the bridge reasons about
    file_params()  normalised summary -> the query the file route needs

Adding a source means adding one entry to SOURCES. It does not mean touching
bridge.py.
"""

import hashlib
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

import requests

# A FIT file carries its signature at bytes 8..12. The MCPs check this too; it
# is repeated here because the bytes crossed a network in between.
FIT_SIGNATURE = b".FIT"

LIVE_PATH = "/api/v1/activities/live"
FILE_PATH = "/api/v1/activities/{activity_id}/file"
HEALTH_PATH = "/api/v1/health"

# Canonical sport names. Sources describe sport differently — COROS by integer
# code, Zwift by an uppercase string — and the device an activity is attributed
# to is chosen by sport, so both have to land in the same vocabulary.
RUNNING, CYCLING, SWIMMING = "running", "cycling", "swimming"
WALKING, GYM, SNOW, WATER, CLIMB, OTHER = (
    "walking", "gym", "snow", "water", "climb", "other")


class SourceError(RuntimeError):
    """Raised when a source cannot be reached or answers with something odd."""


# ---------------------------------------------------------------------------
# COROS
# ---------------------------------------------------------------------------

# COROS groups its integer sport codes by hundred, with two exceptions at the
# top of the run block. Taken from coros-mcp's seeded `sport_types` table.
_COROS_CATEGORY = {1: RUNNING, 2: CYCLING, 3: SWIMMING,
                   4: GYM, 5: SNOW, 7: WATER, 9: WALKING}
_COROS_EXACT = {104: WALKING,      # Hike
                105: CLIMB, 106: CLIMB}


def _coros_sport(raw_sport: Any) -> str:
    try:
        code = int(raw_sport)
    except (TypeError, ValueError):
        return OTHER
    if code in _COROS_EXACT:
        return _COROS_EXACT[code]
    return _COROS_CATEGORY.get(code // 100, OTHER)


def _coros_summarize(a: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "activity_id": str(a["labelId"]),
        "date": a.get("date"),                       # already YYYYMMDD
        "start_time": a.get("startTime"),            # unix seconds
        "name": a.get("name"),
        "distance": a.get("distance"),
        "sport": _coros_sport(a.get("sportType")),
        "raw_sport": a.get("sportType"),
    }


def _coros_file_params(s: Dict[str, Any]) -> Dict[str, Any]:
    # coros-mcp needs the sport type to ask COROS for an export URL, and takes
    # it explicitly so an activity its mirror has not synced still exports.
    return {"sport_type": s["raw_sport"], "file_type": "fit"}


# ---------------------------------------------------------------------------
# Zwift
# ---------------------------------------------------------------------------

_ZWIFT_SPORT = {"CYCLING": CYCLING, "RUNNING": RUNNING}


def _zwift_summarize(a: Dict[str, Any]) -> Dict[str, Any]:
    # Zwift timestamps are UTC, but an activity belongs to the calendar day the
    # rider was living in — a 00:30 CEST ride is "yesterday" in UTC. zwift-mcp
    # applies the same offset when it decides what is in a window, so applying
    # it here too keeps the bridge's floor and the service's filter agreeing.
    start = _epoch(a.get("startDate"))
    offset = a.get("utcOffsetMinutes")
    local = start + int(offset) * 60 if (start and offset is not None) else start

    return {
        "activity_id": str(a.get("id")),
        "date": int(datetime.fromtimestamp(local, timezone.utc).strftime("%Y%m%d"))
        if local else None,
        "start_time": start,
        "name": a.get("name"),
        "distance": a.get("distanceInMeters"),
        "sport": _ZWIFT_SPORT.get(str(a.get("sport") or "").upper(), OTHER),
        "raw_sport": a.get("sport"),
        # Carried so the file route can reach an activity the mirror has never
        # seen — the same role sport_type plays for COROS.
        "_bucket": a.get("fitFileBucket"),
        "_key": a.get("fitFileKey"),
    }


def _zwift_file_params(s: Dict[str, Any]) -> Dict[str, Any]:
    params: Dict[str, Any] = {}
    if s.get("_bucket") and s.get("_key"):
        params["bucket"] = s["_bucket"]
        params["key"] = s["_key"]
    return params


def _epoch(value: Any) -> Optional[int]:
    """Unix seconds from whatever a source calls a timestamp."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value).strip().replace("Z", "+00:00")
    # Zwift writes milliseconds and a +0000 offset with no colon, which
    # fromisoformat rejects before Python 3.11.
    if len(text) >= 5 and (text[-5] in "+-") and text[-3] != ":":
        text = text[:-2] + ":" + text[-2:]
    try:
        return int(datetime.fromisoformat(text).timestamp())
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# The registry
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Source:
    """One upstream, reached through its MCP service."""
    name: str
    url_var: str                 # explicit override, e.g. BRIDGE_COROS_API_URL
    port_var: str                # e.g. COROS_MCP_PORT
    default_port: int
    token_vars: Tuple[str, ...]  # tried in order
    summarize: Callable[[Dict[str, Any]], Dict[str, Any]]
    file_params: Callable[[Dict[str, Any]], Dict[str, Any]]


SOURCES: Dict[str, Source] = {
    "coros": Source(
        name="coros",
        url_var="BRIDGE_COROS_API_URL",
        port_var="COROS_MCP_PORT",
        default_port=8080,
        token_vars=("BRIDGE_COROS_API_TOKEN", "COROS_MCP_AUTH_TOKEN"),
        summarize=_coros_summarize,
        file_params=_coros_file_params,
    ),
    "zwift": Source(
        name="zwift",
        url_var="BRIDGE_ZWIFT_API_URL",
        port_var="ZWIFT_MCP_PORT",
        default_port=8087,
        token_vars=("BRIDGE_ZWIFT_API_TOKEN", "ZWIFT_MCP_AUTH_TOKEN"),
        summarize=_zwift_summarize,
        file_params=_zwift_file_params,
    ),
}


def get(name: str) -> Source:
    """The named source, or a SystemExit naming the ones that exist."""
    try:
        return SOURCES[name]
    except KeyError:
        raise SystemExit(
            f"❌ BRIDGE_SOURCE must be one of {', '.join(sorted(SOURCES))}, "
            f"got {name!r}")


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

def _headers(api_token: Optional[str]) -> Dict[str, str]:
    return {"Authorization": f"Bearer {api_token}"} if api_token else {}


def _fetch(url: str, base_url: str, api_token: Optional[str], timeout: int,
           params: Optional[Dict[str, Any]] = None, what: str = "data",
           service: str = "the source service"):
    """One GET against a source service, with failures phrased as SourceError."""
    try:
        resp = requests.get(url, params=params, headers=_headers(api_token),
                            timeout=timeout)
    except requests.RequestException as e:
        raise SourceError(f"{service} unreachable at {base_url} "
                          f"while fetching {what}: {e}") from e

    if resp.status_code != 200:
        try:
            message = (resp.json() or {}).get("error")
        except ValueError:
            message = resp.text[:200]
        raise SourceError(f"{service} returned HTTP {resp.status_code} "
                          f"for {what}: {message}")
    return resp


def health(source: Source, base_url: str, api_token: Optional[str] = None,
           timeout: int = 10) -> dict:
    """
    Check a source service before a run tries to fetch anything.

    Unauthenticated on the server side, so this says the service is up, not
    that the token is right — a wrong token surfaces on the first real call.
    """
    resp = _fetch(base_url.rstrip("/") + HEALTH_PATH, base_url, None, timeout,
                  what="health", service=f"{source.name}-mcp")
    try:
        return resp.json()
    except ValueError:
        raise SourceError(f"{source.name}-mcp health check did not answer "
                          "with JSON")


def list_activities(source: Source, start_day: int, end_day: int, base_url: str,
                    api_token: Optional[str] = None,
                    timeout: int = 180) -> List[Dict[str, Any]]:
    """
    Every activity the source reports in [start_day, end_day], normalised.

    Dates are integer YYYYMMDD. Paging is the service's problem: it reads every
    page before answering, so a short list here means the upstream had nothing
    more, never that the walk stopped early.
    """
    resp = _fetch(base_url.rstrip("/") + LIVE_PATH, base_url, api_token, timeout,
                  params={"start_day": start_day, "end_day": end_day},
                  what=f"the activity list {start_day}..{end_day}",
                  service=f"{source.name}-mcp")
    try:
        body = resp.json() or {}
    except ValueError:
        raise SourceError(f"{source.name}-mcp did not answer the activity list "
                          "with JSON")

    activities = body.get("activities")
    if activities is None:
        raise SourceError(f"{source.name}-mcp answered the activity list "
                          "without an `activities` field")
    return [source.summarize(a) for a in activities]


def download_fit(source: Source, summary: Dict[str, Any], base_url: str,
                 api_token: Optional[str] = None,
                 timeout: int = 180) -> Tuple[bytes, str]:
    """
    Download one activity's original FIT file. Returns (bytes, sha256).

    The digest is verified against whichever X-*-Sha256 header the service
    sends, so a truncated response is caught here rather than becoming a
    mysterious conversion failure one hop later.
    """
    activity_id = summary["activity_id"]
    resp = _fetch(
        base_url.rstrip("/") + FILE_PATH.format(activity_id=activity_id),
        base_url, api_token, timeout,
        params=source.file_params(summary),
        what=f"the FIT file for {activity_id}",
        service=f"{source.name}-mcp")

    content = resp.content
    if len(content) < 14 or content[8:12] != FIT_SIGNATURE:
        raise SourceError(
            f"{source.name}-mcp returned something that is not a FIT file for "
            f"{activity_id} ({len(content)} bytes, starts {content[:16]!r})")

    digest = hashlib.sha256(content).hexdigest()
    claimed = next((v for k, v in resp.headers.items()
                    if k.lower().endswith("-sha256")), None)
    if claimed and claimed != digest:
        raise SourceError(
            f"FIT file for {activity_id} arrived corrupted: {source.name}-mcp "
            f"sent {claimed}, {len(content)} bytes hash to {digest}")

    return content, digest
