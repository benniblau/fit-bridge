#!/usr/bin/env python3
"""
fit-bridge — carry activities from one service into Garmin Connect.

Garmin only counts activities recorded on Garmin devices toward badges and
challenges. This pulls each new activity from a source as a FIT file, rewrites
its recording-device identity to a Garmin device the account actually owns, and
uploads it. One-way, source → Garmin, hourly from cron.

    list  ──► <source>-mcp /api/v1/activities/live
    fetch ──► <source>-mcp /api/v1/activities/{id}/file
    edit  ──► fit-manager  /api/v1/convert
    push  ──► garmin-mcp   /api/v1/upload/fit
    log   ──► <source>.db

Every hop is an HTTP call to a service that already runs on this machine, so
this holds no upstream API logic, no FIT editor, no Garmin session and no
credentials of any kind. Both source hops answer from the upstream itself
rather than that project's local mirror, which a downloader refreshes on its
own schedule: an activity recorded in the last hour — exactly what an hourly
run exists to catch — is not in that mirror yet.

ONE INSTANCE PER BRIDGE. `--config coros.env` and `--config zwift.env` run the
same code against different sources, each with its own state database, pause
switch, start date and cap. Nothing is shared, so one bridge cannot lose
another's activities or spend its retries. Adding a source means adding an
entry to source.py's SOURCES, not touching this file.

The device is chosen per activity by canonical sport, because a bridge can
carry more than one kind: a Zwift ride should arrive as the Edge, a Zwift run
as the watch, and claiming a bike computer recorded a run is exactly the sort
of incoherence Garmin can be expected to discard.

Every hop is idempotent and every outcome is written down before the next
activity starts, so a killed run costs nothing and nothing is uploaded twice.

Safety, in the order it bites:

    BRIDGE_ENABLED=false     every scheduled run is a no-op. This is how a
                             bridge stays paused while cron is already
                             installed.
    BRIDGE_START_DATE        hard floor. Nothing older is ever uploaded, which
                             is what stops a bridge backfilling years of
                             history into Garmin.
    BRIDGE_MAX_PER_RUN       bounds the blast radius of a bug.
    terminal states          uploaded / duplicate / skipped are never retried.

Usage:
    python bridge.py --config coros.env            # a normal hourly run
    python bridge.py --config zwift.env --dry-run  # list candidates only
    python bridge.py --config zwift.env --only <id>
    python bridge.py --config coros.env --status
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
import garmin_sink
import source as sources

# The config file is read before anything else looks at the environment, so
# --config picks the bridge. Without one, a plain .env still works.
_CONFIG_FLAG = "--config"
if _CONFIG_FLAG in sys.argv:
    _cfg_path = Path(sys.argv[sys.argv.index(_CONFIG_FLAG) + 1])
    if not _cfg_path.is_file():
        raise SystemExit(f"❌ No such config file: {_cfg_path}")
    load_dotenv(_cfg_path, override=True)
else:
    load_dotenv()

BASE = Path(__file__).parent
SCHEMA_PATH = BASE / "schema" / "schema_bridge.sql"
DEFAULT_DB_PATH = BASE / "bridge.db"

# Never reprocessed. See schema/schema_bridge.sql for what each one means.
TERMINAL_STATES = ("uploaded", "duplicate", "skipped")

# The manufacturer/product pair is what makes an upload a Garmin recording at
# all; the serial is what makes it a device Garmin can find on the account.
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


def _env_str_set(name: str) -> Set[str]:
    """
    A comma-separated set of canonical sport names.

    Was a set of COROS integer sport codes. Names travel across sources —
    "cycling" means the same thing whoever recorded it — and a bridge that
    skips strength work should not have to know each upstream's numbering.
    """
    raw = os.getenv(name, "")
    values = {part.strip().lower()
              for part in raw.replace(";", ",").split(",") if part.strip()}
    unknown = values - {sources.RUNNING, sources.CYCLING, sources.SWIMMING,
                        sources.WALKING, sources.GYM, sources.SNOW,
                        sources.WATER, sources.CLIMB, sources.OTHER}
    if unknown:
        raise SystemExit(f"❌ {name} has unknown sport(s): "
                         f"{', '.join(sorted(unknown))}")
    return values


@dataclass(frozen=True)
class Device:
    """A Garmin device an activity can be attributed to."""
    product_id: int
    serial_number: Optional[int]

    def __str__(self) -> str:
        return f"product={self.product_id} serial={self.serial_number}"


def _parse_device(raw: str, var: str) -> Device:
    """`product_id:serial_number`, e.g. 2713:3330008244."""
    parts = [p.strip() for p in raw.split(":")]
    if len(parts) != 2 or not all(parts):
        raise SystemExit(f"❌ {var} must be product_id:serial_number, "
                         f"got {raw!r}")
    try:
        return Device(int(parts[0]), int(parts[1]))
    except ValueError:
        raise SystemExit(f"❌ {var} must be two integers separated by a colon, "
                         f"got {raw!r}")


def _device_map(default: Device) -> Dict[str, Device]:
    """
    Per-sport device overrides, from BRIDGE_DEVICE_<SPORT>.

    A bridge can carry more than one kind of activity, and the device has to
    match it: a Zwift ride should arrive as the Edge and a Zwift run as the
    watch. Claiming a bike computer recorded a run is the sort of incoherence
    Garmin can be expected to discard, and it would be invisible here — the
    upload would look like every other success.
    """
    out: Dict[str, Device] = {}
    for sport in (sources.RUNNING, sources.CYCLING, sources.SWIMMING,
                  sources.WALKING, sources.GYM, sources.SNOW,
                  sources.WATER, sources.CLIMB, sources.OTHER):
        var = f"BRIDGE_DEVICE_{sport.upper()}"
        raw = os.getenv(var, "").strip()
        if raw:
            out[sport] = _parse_device(raw, var)
    return out


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
    source: "sources.Source"
    source_url: str
    source_api_token: Optional[str]
    garmin_url: str
    garmin_api_token: Optional[str]
    manufacturer_id: int
    default_device: Device
    max_per_run: int
    max_attempts: int
    lookback_days: int
    # Per-sport overrides of default_device. Defaulted, so it sits with the
    # other optional fields rather than in the middle of the required ones.
    devices: Dict[str, Device] = field(default_factory=dict)
    skip_sports: Set[str] = field(default_factory=set)
    sleep_between: float = 2.0

    def device_for(self, sport: Optional[str]) -> Device:
        """The device an activity of this sport is attributed to."""
        return self.devices.get(sport or "", self.default_device)

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

        source_name = os.getenv("BRIDGE_SOURCE", "coros").strip().lower()
        src = sources.get(source_name)

        default_device = Device(
            product_id=_env_int("BRIDGE_PRODUCT_ID", DEFAULT_PRODUCT_ID),
            serial_number=_env_int("BRIDGE_GARMIN_SERIAL",
                                   _env_int("GARMIN_SERIAL", None)),
        )

        # The state database defaults to one per source, so two bridges sharing
        # a directory cannot end up sharing a ledger and losing each other's
        # activities to a primary-key collision.
        db_default = BASE / f"{source_name}.db"

        return cls(
            enabled=_env_bool("BRIDGE_ENABLED", False),
            start_date=start_date,
            db_path=Path(os.getenv("BRIDGE_DB_PATH", str(db_default))),
            converter_url=os.getenv("BRIDGE_CONVERTER_URL", "http://127.0.0.1:7077"),
            converter_api_key=(os.getenv("BRIDGE_CONVERTER_API_KEY")
                               or os.getenv("FIT_API_KEY")),
            source=src,
            source_url=_service_url(src.url_var, "MCP_HOST",
                                    src.port_var, src.default_port),
            source_api_token=next(
                (v for v in (os.getenv(n) for n in src.token_vars) if v), None),
            garmin_url=_service_url("BRIDGE_GARMIN_API_URL", "MCP_HOST",
                                    "GARMIN_MCP_PORT", 8080),
            garmin_api_token=(os.getenv("BRIDGE_GARMIN_API_TOKEN")
                              or os.getenv("GARMIN_MCP_AUTH_TOKEN")),
            manufacturer_id=_env_int("BRIDGE_MANUFACTURER_ID", DEFAULT_MANUFACTURER_ID),
            default_device=default_device,
            devices=_device_map(default_device),
            max_per_run=_env_int("BRIDGE_MAX_PER_RUN", 10),
            max_attempts=_env_int("BRIDGE_MAX_ATTEMPTS", 3),
            lookback_days=_env_int("BRIDGE_LOOKBACK_DAYS", 2),
            skip_sports=_env_str_set("BRIDGE_SKIP_SPORTS"),
            sleep_between=float(os.getenv("BRIDGE_SLEEP_BETWEEN", "2")),
        )


# ---------------------------------------------------------------------------
# State database
# ---------------------------------------------------------------------------

# The columns that were named for COROS before this carried more than one
# source, and what they are called now.
_RENAMED_COLUMNS = {
    "coros_label_id": "source_activity_id",
    "coros_date": "source_date",
    "coros_start_time": "source_start_time",
}

# Added after the first databases existed. CREATE TABLE IF NOT EXISTS will not
# add a column to a table that is already there, so these are explicit.
_ADDED_COLUMNS = {
    "sport": "TEXT",
    "device_product_id": "INTEGER",
}


def _migrate(conn: sqlite3.Connection) -> List[str]:
    """
    Bring an existing database up to the current schema. Returns what changed.

    Runs before the schema script, because that script's indexes and views name
    the new columns and would fail against the old shape. Idempotent: every step
    checks what is actually there rather than tracking a version number, so a
    fresh database and a three-times-migrated one end up identical.
    """
    have = {r["name"] for r in conn.execute("PRAGMA table_info(bridge_activities)")}
    if not have:
        return []                              # fresh database, nothing to move

    done: List[str] = []
    for old_name, new_name in _RENAMED_COLUMNS.items():
        if old_name in have and new_name not in have:
            # Views reference the old names; drop them and let the schema
            # script recreate them, as the project's convention already does.
            for (view,) in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'view'").fetchall():
                conn.execute(f"DROP VIEW IF EXISTS {view}")
            conn.execute(f"ALTER TABLE bridge_activities "
                         f"RENAME COLUMN {old_name} TO {new_name}")
            done.append(f"{old_name} → {new_name}")

    have = {r["name"] for r in conn.execute("PRAGMA table_info(bridge_activities)")}
    for name, decl in _ADDED_COLUMNS.items():
        if name not in have:
            conn.execute(f"ALTER TABLE bridge_activities ADD COLUMN {name} {decl}")
            done.append(f"+{name}")

    if done:
        conn.commit()
    return done


def init_db(db_path: Path) -> sqlite3.Connection:
    """Open the state database, creating or upgrading it from the schema."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    changed = _migrate(conn)
    if changed:
        print(f"   migrated {db_path.name}: {', '.join(changed)}")
    conn.executescript(SCHEMA_PATH.read_text())
    conn.commit()
    return conn


def load_states(conn: sqlite3.Connection) -> Dict[str, sqlite3.Row]:
    rows = conn.execute("SELECT * FROM bridge_activities").fetchall()
    return {row["source_activity_id"]: row for row in rows}


def remember(conn: sqlite3.Connection, summary: Dict[str, Any]) -> None:
    """
    Record an activity as seen, without disturbing an outcome it already has.

    The metadata is refreshed every time because sources rename activities and
    revise distances after the fact — COROS geocodes a title on import.
    """
    conn.execute(
        """INSERT INTO bridge_activities (
               source_activity_id, source_date, source_start_time, name,
               sport_type, sport, distance, status, attempts, first_seen_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', 0, ?)
           ON CONFLICT(source_activity_id) DO UPDATE SET
               source_date       = excluded.source_date,
               source_start_time = excluded.source_start_time,
               name              = excluded.name,
               sport_type        = excluded.sport_type,
               sport             = excluded.sport,
               distance          = excluded.distance""",
        (summary["activity_id"], summary["date"], summary["start_time"],
         summary["name"], str(summary["raw_sport"]), summary["sport"],
         summary["distance"], _now()),
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
        "WHERE source_activity_id = ?", (label_id,))
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
        "WHERE source_activity_id = ?", (label_id,))
    conn.commit()


def park(conn: sqlite3.Connection, label_id: str, attempts: int) -> None:
    """Spend an activity's remaining attempts, for a failure that will repeat."""
    conn.execute(
        "UPDATE bridge_activities SET attempts = MAX(attempts, ?) "
        "WHERE source_activity_id = ?", (attempts, label_id))
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
           WHERE source_activity_id = ?""",
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
        """SELECT MIN(source_date) AS oldest FROM bridge_activities
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
    Drop anything below the hard floor.

    The query is already bounded by the same floor, but the upstream decides
    what a date range means, so it is enforced again here.
    """
    floor = _yyyymmdd(cfg.start_date)
    kept, rejected = [], 0
    for summary in activities:
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
        label = summary["activity_id"]

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

        if summary["sport"] in cfg.skip_sports:
            to_skip.append(summary)
            continue

        candidates.append(summary)

    # Oldest first, so an interrupted run leaves the newest for next time.
    candidates.sort(key=lambda s: (s["date"] or 0, s["start_time"] or 0))
    return candidates, to_skip, reasons


def describe(summary: Dict[str, Any]) -> str:
    km = (summary["distance"] or 0) / 1000.0
    return (f"{summary['date']} {summary['activity_id']} "
            f"{(summary['name'] or '—')[:36]:<36} "
            f"{km:6.2f} km  {summary['sport']}")


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
    label = summary["activity_id"]
    filename = f"{label}.fit"
    count_attempt(conn, label)

    source_sha = converted_sha = None
    try:
        # The source adapter supplies whatever its file route needs to reach an
        # activity the mirror has not synced yet — a sport type for COROS, a
        # bucket and key for Zwift.
        raw, source_sha = sources.download_fit(
            cfg.source, summary, cfg.source_url,
            api_token=cfg.source_api_token)
        print(f"    📥 {len(raw):,} bytes from {cfg.source.name}")

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
            print(f"    ⚠️  no start time from {cfg.source.name} — uploading "
                  "with the file's own timestamp, which may collide with a "
                  "sibling activity")

        # The device is chosen by sport: a ride must not arrive claiming a
        # watch, nor a run claiming a bike computer.
        device = cfg.device_for(summary["sport"])

        converted, converted_sha = converter.convert(
            raw, filename, cfg.converter_url,
            manufacturer_id=cfg.manufacturer_id,
            product_id=device.product_id,
            serial_number=device.serial_number,
            time_created=start_time or None,
            api_key=cfg.converter_api_key,
        )
        print(f"    🔁 rewritten to manufacturer={cfg.manufacturer_id} "
              f"{device}  ({summary['sport']})")

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
    except sources.SourceError as e:
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
    conn.execute("UPDATE bridge_activities SET device_product_id = ? "
                 "WHERE source_activity_id = ?", (device.product_id, label))
    conn.commit()

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
    print(f"📦 fit-bridge {cfg.source.name} → garmin")
    print(f"   database   : {cfg.db_path}")
    print(f"   enabled    : {cfg.enabled}")
    print(f"   start date : {cfg.start_date}")
    print(f"   source     : {cfg.source_url}")
    print(f"   device     : manufacturer={cfg.manufacturer_id}, "
          f"{cfg.default_device} by default")
    for sport, device in sorted(cfg.devices.items()):
        print(f"                {sport:<10} {device}")
    if cfg.skip_sports:
        print(f"   skipping   : {', '.join(sorted(cfg.skip_sports))}")

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
            print(f"   ⚠️  {r['source_date']} {r['source_activity_id']} "
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
        description="Bridge new activities from a source into Garmin Connect")
    p.add_argument("--config", metavar="FILE", default=None,
                   help="Env file selecting the bridge, e.g. coros.env or "
                        "zwift.env. Read before anything else looks at the "
                        "environment; without one, a plain .env is used")
    p.add_argument("--dry-run", action="store_true",
                   help="List what would be processed; touch nothing")
    p.add_argument("--only", metavar="ACTIVITY_ID",
                   help="Process exactly one activity")
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
            print("   Set BRIDGE_ENABLED=true to start bridging,")
            print("   or use --only <activity_id> --force for a single run.")
            return 0

    missing = [name for name, d in
               [("default", cfg.default_device)] + sorted(cfg.devices.items())
               if d.serial_number is None]
    if missing:
        print(f"⚠️  no serial for {', '.join(missing)} — those uploads will "
              "claim a Garmin device with whatever serial the source file "
              "carries, which Garmin will not match to your account")

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
    print(f"🌉 fit-bridge {cfg.source.name} → garmin — "
          f"window {start_day} … {end_day}"
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

            src_info = sources.health(cfg.source, cfg.source_url,
                                      cfg.source_api_token)
            print(f"   {cfg.source.name + '-mcp':<11}: {cfg.source_url} "
                  f"({src_info.get('activities')} activities in its mirror)")

        activities = sources.list_activities(
            cfg.source, start_day, end_day, cfg.source_url,
            api_token=cfg.source_api_token)
        counts["considered"] = len(activities)
        print(f"   {cfg.source.name.upper():<11}: {len(activities)} "
              f"activities in the window")

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

    except (sources.SourceError, converter.ConversionError,
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
