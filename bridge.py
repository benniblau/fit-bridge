#!/usr/bin/env python3
"""
COROS → Garmin activity bridge.

Pulls each new COROS activity as a FIT file, rewrites its recording-device
identity to a Garmin device the account owns, and uploads it to Garmin Connect
so the record keeps advancing after the Garmin watch is retired. One-way,
hourly from cron.

    list  ──► COROS /activity/query           (CorosClient, from coros-mcp)
    fetch ──► coros-mcp   /api/v1/activities/{id}/file
    edit  ──► fit-manager /api/v1/convert
    push  ──► garmin-mcp  /api/v1/upload/fit
    log   ──► bridge.db

Three of the four hops are HTTP calls to services that already run on this
machine, so the bridge holds no COROS export logic, no FIT editor and no
Garmin session of its own. Only the listing reaches an API directly, because
it must be live: coros-mcp's REST API serves a mirror that a downloader
refreshes on its own schedule, and an activity recorded in the last hour —
exactly what this run exists to catch — is not in it yet.

Every hop is idempotent and every outcome is written down before the next
activity starts, so a killed run costs nothing and nothing is uploaded twice.

Safety, in the order it bites:

    BRIDGE_ENABLED=false     every scheduled run is a no-op. This is how the
                             bridge stays paused while cron is already
                             installed.
    BRIDGE_START_DATE        hard floor. Nothing older is ever uploaded, which
                             is what stops the bridge backfilling Garmin's own
                             history back into Garmin.
    BRIDGE_MAX_PER_RUN       bounds the blast radius of a bug.
    terminal states          uploaded / duplicate / skipped are never retried.

Usage:
    python bridge.py                      # a normal hourly run
    python bridge.py --dry-run            # list candidates, touch nothing
    python bridge.py --only <label_id>    # exactly one activity
    python bridge.py --status             # what the database knows
"""

import argparse
import os
import sqlite3
import sys
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from dotenv import load_dotenv

import converter
import coros_source
import garmin_sink

load_dotenv()

BASE = Path(__file__).parent
SCHEMA_PATH = BASE / "schema" / "schema_bridge.sql"
DEFAULT_DB_PATH = BASE / "bridge.db"

# Never reprocessed. See schema/schema_bridge.sql for what each one means.
TERMINAL_STATES = ("uploaded", "duplicate", "skipped")

# Benni's Fenix 8. The serial is preserved so Garmin sees a device that is
# actually registered to the account; the manufacturer/product pair is what
# makes it a Garmin recording at all.
DEFAULT_MANUFACTURER_ID = 1        # garmin
DEFAULT_PRODUCT_ID = 4536          # fenix_8


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _env_int(name: str, default: Optional[int]) -> Optional[int]:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError:
        raise SystemExit(f"❌ {name} must be an integer, got {raw!r}")


def _env_int_set(name: str) -> Set[int]:
    raw = os.getenv(name, "")
    values = set()
    for part in raw.replace(";", ",").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            values.add(int(part))
        except ValueError:
            raise SystemExit(f"❌ {name} must be a comma-separated list of "
                             f"sport type ids, got {part!r}")
    return values


def _service_url(explicit: str, host_var: str, port_var: str,
                 default_port: int) -> str:
    """
    Where a sibling service lives.

    An explicit BRIDGE_*_API_URL wins. Failing that the address is assembled
    from MCP_HOST and the service's own port variable, which the sibling
    projects already define and this machine's .env already carries — so the
    two servers do not have to be spelled out twice in two different forms.
    """
    url = os.getenv(explicit, "").strip()
    if url:
        return url
    host = os.getenv(host_var, "").strip() or "127.0.0.1"
    port = os.getenv(port_var, "").strip() or str(default_port)
    return f"http://{host}:{port}"


def _yyyymmdd(d: date) -> int:
    return int(d.strftime("%Y%m%d"))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class Config:
    """Everything the run is allowed to do, resolved from the environment."""
    enabled: bool
    start_date: date
    db_path: Path
    converter_url: str
    converter_api_key: Optional[str]
    coros_url: str
    coros_api_token: Optional[str]
    garmin_url: str
    garmin_api_token: Optional[str]
    manufacturer_id: int
    product_id: int
    serial_number: Optional[int]
    max_per_run: int
    max_attempts: int
    lookback_days: int
    skip_sports: Set[int] = field(default_factory=set)
    sleep_between: float = 2.0

    @classmethod
    def from_env(cls) -> "Config":
        raw_start = os.getenv("BRIDGE_START_DATE", "").strip()
        if not raw_start:
            raise SystemExit(
                "❌ BRIDGE_START_DATE is required (YYYY-MM-DD).\n"
                "   It is the hard floor for what may be uploaded; without it "
                "the bridge could push years of imported Garmin history back "
                "into Garmin."
            )
        try:
            start_date = date.fromisoformat(raw_start)
        except ValueError:
            raise SystemExit(f"❌ BRIDGE_START_DATE must be YYYY-MM-DD, "
                             f"got {raw_start!r}")

        return cls(
            enabled=_env_bool("BRIDGE_ENABLED", False),
            start_date=start_date,
            db_path=Path(os.getenv("BRIDGE_DB_PATH", str(DEFAULT_DB_PATH))),
            converter_url=os.getenv("BRIDGE_CONVERTER_URL", "http://127.0.0.1:7077"),
            converter_api_key=(os.getenv("BRIDGE_CONVERTER_API_KEY")
                               or os.getenv("FIT_API_KEY")),
            coros_url=_service_url("BRIDGE_COROS_API_URL", "MCP_HOST",
                                   "COROS_MCP_PORT", 8080),
            coros_api_token=(os.getenv("BRIDGE_COROS_API_TOKEN")
                             or os.getenv("COROS_MCP_AUTH_TOKEN")),
            garmin_url=_service_url("BRIDGE_GARMIN_API_URL", "MCP_HOST",
                                    "GARMIN_MCP_PORT", 8080),
            garmin_api_token=(os.getenv("BRIDGE_GARMIN_API_TOKEN")
                              or os.getenv("GARMIN_MCP_AUTH_TOKEN")),
            manufacturer_id=_env_int("BRIDGE_MANUFACTURER_ID", DEFAULT_MANUFACTURER_ID),
            product_id=_env_int("BRIDGE_PRODUCT_ID", DEFAULT_PRODUCT_ID),
            serial_number=_env_int("BRIDGE_GARMIN_SERIAL",
                                   _env_int("GARMIN_SERIAL", None)),
            max_per_run=_env_int("BRIDGE_MAX_PER_RUN", 10),
            max_attempts=_env_int("BRIDGE_MAX_ATTEMPTS", 3),
            lookback_days=_env_int("BRIDGE_LOOKBACK_DAYS", 2),
            skip_sports=_env_int_set("BRIDGE_SKIP_SPORTS"),
            sleep_between=float(os.getenv("BRIDGE_SLEEP_BETWEEN", "2")),
        )


# ---------------------------------------------------------------------------
# State database
# ---------------------------------------------------------------------------

def init_db(db_path: Path) -> sqlite3.Connection:
    """Open the state database, creating or upgrading it from the schema."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA_PATH.read_text())
    conn.commit()
    return conn


def load_states(conn: sqlite3.Connection) -> Dict[str, sqlite3.Row]:
    rows = conn.execute("SELECT * FROM bridge_activities").fetchall()
    return {row["coros_label_id"]: row for row in rows}


def remember(conn: sqlite3.Connection, summary: Dict[str, Any]) -> None:
    """
    Record an activity as seen, without disturbing an outcome it already has.

    The metadata is refreshed every time because COROS renames activities and
    revises distances after import.
    """
    conn.execute(
        """INSERT INTO bridge_activities (
               coros_label_id, coros_date, coros_start_time, name, sport_type,
               distance, status, attempts, first_seen_at)
           VALUES (?, ?, ?, ?, ?, ?, 'pending', 0, ?)
           ON CONFLICT(coros_label_id) DO UPDATE SET
               coros_date       = excluded.coros_date,
               coros_start_time = excluded.coros_start_time,
               name             = excluded.name,
               sport_type       = excluded.sport_type,
               distance         = excluded.distance""",
        (summary["label_id"], summary["date"], summary["start_time"],
         summary["name"], summary["sport_type"], summary["distance"], _now()),
    )
    conn.commit()


def count_attempt(conn: sqlite3.Connection, label_id: str) -> None:
    """
    Count the attempt before making it.

    Counting afterwards would let a file that crashes the process be retried
    forever, an hour at a time.
    """
    conn.execute(
        "UPDATE bridge_activities SET attempts = attempts + 1 "
        "WHERE coros_label_id = ?", (label_id,))
    conn.commit()


def refund_attempt(conn: sqlite3.Connection, label_id: str) -> None:
    """
    Give back an attempt spent on a run a service aborted.

    The counter exists to stop a bad *file* being retried forever. An activity
    that never got a verdict because garmin-mcp was down learned nothing about
    itself, and three unlucky hours in a row should not exhaust it.
    """
    conn.execute(
        "UPDATE bridge_activities SET attempts = MAX(COALESCE(attempts, 0) - 1, 0) "
        "WHERE coros_label_id = ?", (label_id,))
    conn.commit()


def park(conn: sqlite3.Connection, label_id: str, attempts: int) -> None:
    """Spend an activity's remaining attempts, for a failure that will repeat."""
    conn.execute(
        "UPDATE bridge_activities SET attempts = MAX(attempts, ?) "
        "WHERE coros_label_id = ?", (attempts, label_id))
    conn.commit()


def set_outcome(conn: sqlite3.Connection, label_id: str, status: str,
                upload_id: Optional[str] = None,
                activity_id: Optional[str] = None,
                error: Optional[str] = None,
                source_sha: Optional[str] = None,
                converted_sha: Optional[str] = None) -> None:
    conn.execute(
        """UPDATE bridge_activities SET
               status             = ?,
               garmin_upload_id   = COALESCE(?, garmin_upload_id),
               garmin_activity_id = COALESCE(?, garmin_activity_id),
               last_error         = ?,
               source_sha256      = COALESCE(?, source_sha256),
               converted_sha256   = COALESCE(?, converted_sha256),
               uploaded_at        = CASE WHEN ? IN ('uploaded', 'duplicate')
                                         THEN COALESCE(uploaded_at, ?)
                                         ELSE uploaded_at END
           WHERE coros_label_id = ?""",
        (status, upload_id, activity_id, error, source_sha, converted_sha,
         status, _now(), label_id),
    )
    conn.commit()


def start_run(conn: sqlite3.Connection) -> int:
    cur = conn.execute(
        "INSERT INTO bridge_runs (started_at, status) VALUES (?, 'running')",
        (_now(),))
    conn.commit()
    return cur.lastrowid


def finish_run(conn: sqlite3.Connection, run_id: int, counts: Dict[str, int],
               status: str, error: Optional[str] = None) -> None:
    conn.execute(
        """UPDATE bridge_runs SET finished_at = ?, considered = ?, uploaded = ?,
               duplicates = ?, failed = ?, skipped = ?, status = ?, error = ?
           WHERE run_id = ?""",
        (_now(), counts.get("considered", 0), counts.get("uploaded", 0),
         counts.get("duplicate", 0), counts.get("failed", 0),
         counts.get("skipped", 0), status, error, run_id),
    )
    conn.commit()


def _from_yyyymmdd(value: int) -> Optional[date]:
    text = str(value)
    if len(text) != 8:
        return None
    try:
        return date(int(text[:4]), int(text[4:6]), int(text[6:8]))
    except ValueError:
        return None


def close_stale_run(conn: sqlite3.Connection, run_id: int) -> None:
    """
    Make sure no run row is left saying 'running'.

    Anything that gets past the handlers in main() — a KeyboardInterrupt, a
    SystemExit raised by an imported module — would otherwise leave a row that
    claims a run is still in flight an hour later.
    """
    conn.execute(
        """UPDATE bridge_runs
              SET finished_at = ?, status = 'error',
                  error = COALESCE(error, 'interrupted')
            WHERE run_id = ? AND status = 'running'""",
        (_now(), run_id))
    conn.commit()


def compute_cutoff(conn: sqlite3.Connection, cfg: Config) -> date:
    """
    Earliest day to ask COROS about.

    Two things widen the window, and the floor closes it:

    * the lookback, which absorbs activities that reach COROS late — a watch
      synced hours after the run, or a day the phone spent offline;
    * anything already known and not yet finished. Without this, a run capped
      by BRIDGE_MAX_PER_RUN would mark itself successful and move the cutoff
      past the very activities it deferred, and they would never be seen
      again. Every activity in the window is recorded before the cap is
      applied, so "the rest follow next run" actually holds.

    Neither can reach below BRIDGE_START_DATE.
    """
    cutoff = cfg.start_date

    row = conn.execute(
        "SELECT MAX(started_at) AS last FROM bridge_runs WHERE status = 'ok'"
    ).fetchone()
    if row and row["last"]:
        try:
            last = datetime.fromisoformat(row["last"])
        except ValueError:
            last = None
        if last:
            cutoff = max(cfg.start_date,
                         last.date() - timedelta(days=cfg.lookback_days))

    row = conn.execute(
        """SELECT MIN(coros_date) AS oldest FROM bridge_activities
           WHERE status = 'pending'
              OR (status = 'failed' AND COALESCE(attempts, 0) < ?)""",
        (cfg.max_attempts,)).fetchone()
    if row and row["oldest"]:
        unfinished = _from_yyyymmdd(row["oldest"])
        if unfinished:
            cutoff = min(cutoff, max(cfg.start_date, unfinished))

    return cutoff


# ---------------------------------------------------------------------------
# Selection
# ---------------------------------------------------------------------------

def in_scope(activities: List[Dict[str, Any]], cfg: Config
             ) -> Tuple[List[Dict[str, Any]], int]:
    """
    Summarize what COROS reported, dropping anything below the hard floor.

    The query is already bounded by the same floor, but COROS decides what a
    date range means, so it is enforced again here.
    """
    floor = _yyyymmdd(cfg.start_date)
    kept, rejected = [], 0
    for raw in activities:
        summary = coros_source.summarize(raw)
        if summary["date"] and summary["date"] < floor:
            rejected += 1
            continue
        kept.append(summary)
    return kept, rejected


def select(summaries: List[Dict[str, Any]], states: Dict[str, sqlite3.Row],
           cfg: Config, only: Optional[str], force: bool
           ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], Dict[str, int]]:
    """
    Split in-scope activities into work, skips and things already settled.

    Returns (candidates, to_skip, reasons). `to_skip` are activities whose
    sport type is excluded — they get a terminal `skipped` row so they are
    never looked at again.
    """
    candidates: List[Dict[str, Any]] = []
    to_skip: List[Dict[str, Any]] = []
    reasons: Dict[str, int] = {"terminal": 0, "exhausted": 0,
                               "other_activity": 0}

    for summary in summaries:
        label = summary["label_id"]

        if only and label != only:
            reasons["other_activity"] += 1
            continue

        state = states.get(label)
        if state is not None and not force:
            if state["status"] in TERMINAL_STATES:
                reasons["terminal"] += 1
                continue
            if (state["status"] == "failed"
                    and (state["attempts"] or 0) >= cfg.max_attempts):
                reasons["exhausted"] += 1
                continue

        if summary["sport_type"] in cfg.skip_sports:
            to_skip.append(summary)
            continue

        candidates.append(summary)

    # Oldest first, so an interrupted run leaves the newest for next time.
    candidates.sort(key=lambda s: (s["date"] or 0, s["start_time"] or 0))
    return candidates, to_skip, reasons


def describe(summary: Dict[str, Any]) -> str:
    km = (summary["distance"] or 0) / 1000.0
    return (f"{summary['date']} {summary['label_id']} "
            f"{(summary['name'] or '—')[:40]:<40} "
            f"{km:6.2f} km  sport={summary['sport_type']}")


# ---------------------------------------------------------------------------
# The pipeline
# ---------------------------------------------------------------------------

def process(conn: sqlite3.Connection, cfg: Config,
            summary: Dict[str, Any]) -> str:
    """
    Download, convert, upload and record one activity.

    Returns the status it ended in. Raises only SinkError, which means the
    upload service or Garmin behind it stopped answering — a verdict on the
    run, not on this file. Every other failure belongs to the activity, and
    one activity failing must not abort the run.
    """
    label = summary["label_id"]
    filename = f"{label}.fit"
    count_attempt(conn, label)

    source_sha = converted_sha = None
    try:
        # The sport type comes from the live listing, so coros-mcp can export
        # an activity its own database has not synced yet.
        raw, source_sha = coros_source.download_fit(
            label, summary["sport_type"], cfg.coros_url,
            api_token=cfg.coros_api_token)
        print(f"    📥 {len(raw):,} bytes from COROS")

        # The recording's start time is stamped into file_id.time_created,
        # because Garmin identifies an upload by that together with the serial.
        # A Garmin watch sets it when the recording starts, so it is unique per
        # activity; COROS sets it when the file is exported, so every activity
        # in one sync batch carries the same value. Since every file here is
        # given the same serial by design, the batch collapses to a single
        # identity and Garmin answers 409 for all but the first — silently, and
        # terminally, because a duplicate is never retried. Cost: one activity
        # of three on 2026-08-23.
        start_time = summary.get("start_time")
        if not start_time:
            print("    ⚠️  no start time from COROS — uploading with the file's "
                  "own timestamp, which may collide with a sibling activity")

        converted, converted_sha = converter.convert(
            raw, filename, cfg.converter_url,
            manufacturer_id=cfg.manufacturer_id,
            product_id=cfg.product_id,
            serial_number=cfg.serial_number,
            time_created=start_time or None,
            api_key=cfg.converter_api_key,
        )
        print(f"    🔁 rewritten to manufacturer={cfg.manufacturer_id} "
              f"product={cfg.product_id} serial={cfg.serial_number}")

        result = garmin_sink.upload_fit(
            converted, filename, cfg.garmin_url,
            api_token=cfg.garmin_api_token)

    except converter.ConversionError as e:
        set_outcome(conn, label, "failed", error=f"convert: {e}",
                    source_sha=source_sha)
        print(f"    ❌ conversion failed: {e}")
        if not e.retryable:
            # The service refused the file itself. Retrying it hourly until the
            # attempt budget runs out only adds noise, so park it now.
            park(conn, label, cfg.max_attempts)
            print("       (not retryable — parked for inspection)")
        return "failed"
    except coros_source.SourceError as e:
        set_outcome(conn, label, "failed", error=f"download: {e}")
        print(f"    ❌ download failed: {e}")
        return "failed"
    except garmin_sink.SinkError:
        # Garmin never answered, so nothing is known about this file. Leave it
        # exactly as it was and let the run abort; the next one picks it up.
        refund_attempt(conn, label)
        raise
    except Exception as e:                                        # noqa: BLE001
        set_outcome(conn, label, "failed", error=f"{type(e).__name__}: {e}",
                    source_sha=source_sha, converted_sha=converted_sha)
        print(f"    ❌ {type(e).__name__}: {e}")
        return "failed"

    set_outcome(conn, label, result.status,
                upload_id=result.upload_id,
                activity_id=result.activity_id,
                error=None if result.status == "uploaded" else result.message,
                source_sha=source_sha, converted_sha=converted_sha)

    if result.status == "uploaded":
        print(f"    ✅ uploaded — Garmin activity {result.activity_id}")
    elif result.status == "duplicate":
        print("    ⚠️  duplicate — Garmin already has this activity")
    else:
        print(f"    ❌ upload failed: {result.message}")
    return result.status


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def print_status(conn: sqlite3.Connection, cfg: Config) -> None:
    """What the database knows, for a human checking on the bridge."""
    print("📦 coros-garmin-bridge state")
    print(f"   database   : {cfg.db_path}")
    print(f"   enabled    : {cfg.enabled}")
    print(f"   start date : {cfg.start_date}")
    print(f"   device     : manufacturer={cfg.manufacturer_id} "
          f"product={cfg.product_id} serial={cfg.serial_number}")

    rows = conn.execute("SELECT * FROM v_bridge_status").fetchall()
    if not rows:
        print("\n   no activities recorded yet")
    else:
        print("\n   status      count   first      last       km")
        for r in rows:
            print(f"   {r['status']:<11} {r['activities']:>5}   "
                  f"{r['first_date']}   {r['last_date']}   {r['total_km'] or 0:>8}")

    failures = conn.execute(
        "SELECT * FROM v_bridge_failures LIMIT 10").fetchall()
    if failures:
        print("\n   needs attention:")
        for r in failures:
            print(f"   ⚠️  {r['coros_date']} {r['coros_label_id']} "
                  f"[{r['status']}, {r['attempts']} attempts] "
                  f"{(r['last_error'] or '')[:80]}")

    runs = conn.execute("SELECT * FROM v_bridge_runs LIMIT 5").fetchall()
    if runs:
        print("\n   recent runs:")
        for r in runs:
            print(f"   {r['started_at'][:19]}  {r['status']:<8} "
                  f"considered={r['considered']} uploaded={r['uploaded']} "
                  f"dup={r['duplicates']} failed={r['failed']} "
                  f"skipped={r['skipped']}"
                  + (f"  {r['error'][:60]}" if r["error"] else ""))


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Bridge new COROS activities into Garmin Connect")
    p.add_argument("--dry-run", action="store_true",
                   help="List what would be processed; touch nothing")
    p.add_argument("--only", metavar="LABEL_ID",
                   help="Process exactly one COROS activity")
    p.add_argument("--force", action="store_true",
                   help="With --only: reprocess an activity that is already "
                        "settled, and act even when BRIDGE_ENABLED is false")
    p.add_argument("--limit", type=int, default=None,
                   help="Override BRIDGE_MAX_PER_RUN for this run")
    p.add_argument("--since", metavar="YYYY-MM-DD", default=None,
                   help="Look back to this date instead of the computed cutoff "
                        "(still floored by BRIDGE_START_DATE)")
    p.add_argument("--status", action="store_true",
                   help="Print what the state database knows and exit")
    return p.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    cfg = Config.from_env()

    if args.force and not args.only:
        print("❌ --force only makes sense together with --only")
        return 1

    conn = init_db(cfg.db_path)

    if args.status:
        print_status(conn, cfg)
        return 0

    # The pause switch. A dry run is a no-op by definition, so it is allowed;
    # a single named activity is allowed only when explicitly forced, because
    # cron never passes --only and so can never take this path.
    if not cfg.enabled and not args.dry_run:
        if args.only and args.force:
            print("⚠️  BRIDGE_ENABLED=false — proceeding anyway for one "
                  f"explicitly named activity ({args.only})")
        else:
            print("⏸  BRIDGE_ENABLED=false — nothing to do.")
            print("   Set BRIDGE_ENABLED=true once the Garmin watch is retired,")
            print("   or use --only <label_id> --force for a single manual run.")
            return 0

    if cfg.serial_number is None:
        print("⚠️  BRIDGE_GARMIN_SERIAL is unset — the upload will claim a "
              "Garmin device with whatever serial the COROS file carries")

    cutoff = compute_cutoff(conn, cfg)
    if args.since:
        try:
            cutoff = max(cfg.start_date, date.fromisoformat(args.since))
        except ValueError:
            print(f"❌ --since must be YYYY-MM-DD, got {args.since!r}")
            return 1
    if args.only:
        # A named activity must be findable even if it predates the cutoff.
        cutoff = cfg.start_date

    start_day, end_day = _yyyymmdd(cutoff), _yyyymmdd(date.today())
    print(f"🌉 coros-garmin-bridge — window {start_day} … {end_day}"
          + ("  [dry run]" if args.dry_run else ""))

    run_id = None if args.dry_run else start_run(conn)
    counts = {"considered": 0, "uploaded": 0, "duplicate": 0,
              "failed": 0, "skipped": 0}

    try:
        # Preflight every service before touching anything. One that is down
        # would otherwise burn a retry attempt on every candidate in turn.
        # Garmin is probed too, and deliberately last: it is the slowest check
        # and the only one that costs a round trip to a third party.
        if not args.dry_run:
            info = converter.health(cfg.converter_url, cfg.converter_api_key)
            print(f"   converter  : {cfg.converter_url} "
                  f"({info.get('device_count')} devices)")

            coros_info = coros_source.health(cfg.coros_url, cfg.coros_api_token)
            print(f"   coros-mcp  : {cfg.coros_url} "
                  f"({coros_info.get('activities')} activities known)")

        client = coros_source.connect()
        activities = coros_source.list_activities(client, start_day, end_day)
        counts["considered"] = len(activities)
        print(f"   COROS      : {len(activities)} activities in the window")

        summaries, before_start = in_scope(activities, cfg)

        # States must be read before anything is recorded, or every activity
        # would look freshly pending.
        states = load_states(conn)

        # Record the whole window before applying the cap, so deferred
        # activities keep the cutoff open. See compute_cutoff().
        if not args.dry_run:
            for s in summaries:
                remember(conn, s)

        candidates, to_skip, reasons = select(
            summaries, states, cfg, args.only, args.force)
        reasons["before_start"] = before_start

        if args.only and not candidates and not to_skip:
            print(f"❌ {args.only} is not in the window, or is already settled "
                  f"(use --force to reprocess it)")
            if run_id:
                finish_run(conn, run_id, counts, "error",
                           f"--only {args.only} matched nothing")
            return 1

        detail = ", ".join(f"{n} {k.replace('_', ' ')}"
                           for k, n in reasons.items() if n and k != "other_activity")
        if detail:
            print(f"   ignored    : {detail}")

        limit = args.limit if args.limit is not None else cfg.max_per_run
        if not args.only and len(candidates) > limit:
            print(f"   ⚠️  {len(candidates)} candidates, processing {limit} "
                  f"(BRIDGE_MAX_PER_RUN); the rest follow next run")
            candidates = candidates[:limit]

        if args.dry_run:
            print(f"\n🔎 would skip {len(to_skip)}, process {len(candidates)}:")
            for s in to_skip:
                print(f"   🗑️  {describe(s)}  (sport type excluded)")
            for s in candidates:
                print(f"   →  {describe(s)}")
            print("\n   dry run: nothing downloaded, converted or uploaded")
            return 0

        for s in to_skip:
            set_outcome(conn, s["label_id"], "skipped",
                        error=f"sport type {s['sport_type']} in BRIDGE_SKIP_SPORTS")
            counts["skipped"] += 1
            print(f"   🗑️  skipped {describe(s)}")

        if not candidates:
            print("   nothing to do")
            finish_run(conn, run_id, counts, "ok")
            return 0

        garmin_sink.authenticate(cfg.garmin_url, cfg.garmin_api_token)
        print(f"   garmin-mcp : {cfg.garmin_url} (session ready)")

        for n, s in enumerate(candidates, 1):
            print(f"\n[{n}/{len(candidates)}] {describe(s)}")
            status = process(conn, cfg, s)
            counts[status] = counts.get(status, 0) + 1
            if n < len(candidates):
                time.sleep(cfg.sleep_between)

        finish_run(conn, run_id, counts, "ok")

    except (coros_source.SourceError, converter.ConversionError,
            garmin_sink.SinkError) as e:
        # A service-level failure, not an activity-level one: nothing was
        # marked failed, so the next run retries everything untouched.
        print(f"\n❌ run aborted: {e}")
        if run_id:
            finish_run(conn, run_id, counts, "error", str(e)[:500])
        return 1
    except Exception as e:                                        # noqa: BLE001
        print(f"\n❌ run aborted: {type(e).__name__}: {e}")
        if run_id:
            finish_run(conn, run_id, counts, "error",
                       f"{type(e).__name__}: {e}"[:500])
        raise
    finally:
        if run_id:
            close_stale_run(conn, run_id)

    print(f"\n✅ {counts['uploaded']} uploaded, {counts['duplicate']} duplicate, "
          f"{counts['failed']} failed, {counts['skipped']} skipped "
          f"({counts['considered']} considered)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
