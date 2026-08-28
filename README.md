# fit-bridge

Carries activities from one service into Garmin Connect, rewriting the
recording device on the way.

Garmin only counts activities recorded on **Garmin devices** toward badges and
challenges. So a COROS watch or a Zwift session advances nothing, however real
the effort. This pulls each new activity as a FIT file, rewrites its
recording-device identity to a Garmin device you actually own, and uploads it.
One-way, source → Garmin, hourly from cron.

```
cron (hourly)
   │
   ▼
bridge.py ──► <source>-mcp /api/v1/activities/live       list new activities
   ├───────► <source>-mcp /api/v1/activities/{id}/file   download FIT
   ├───────► fit-manager  /api/v1/convert                rewrite device identity
   ├───────► garmin-mcp   /api/v1/upload/fit             upload
   └───────► <source>.db                                 state, dedup, retries
```

Every hop is an HTTP call to a service already running on this machine, so this
repo holds no upstream API logic, no FIT editor, no Garmin session — and **no
credentials of any kind**. The only secrets in a config file are bearer tokens
authorising this bridge to those services.

Every hop is idempotent and every outcome is recorded, so an interrupted run
costs nothing and no activity is ever uploaded twice.

## One instance per bridge

`--config coros.env` and `--config zwift.env` run the same code against
different sources. Each bridge has its own state database, pause switch, start
date and cap, so one cannot lose another's activities or spend its retries.

```bash
.venv/bin/python bridge.py --config coros.env              # a normal hourly run
.venv/bin/python bridge.py --config zwift.env --dry-run    # candidates only
.venv/bin/python bridge.py --config coros.env --status     # what the state DB knows
.venv/bin/python bridge.py --config zwift.env --only <id>  # exactly one activity
```

`--dry-run` writes nothing at all, not even to the state database, so it is
safe while a bridge is paused. `--only <id> --force` is the one way to act while
`BRIDGE_ENABLED=false`; cron never passes `--only`, so the pause switch still
holds for scheduled runs.

## Sources

| Source | Service | Notes |
|---|---|---|
| `coros` | `coros-mcp` | Runs. Needs `sport_type` to export, supplied automatically |
| `zwift` | `zwift-mcp` | Rides **and** runs — see the device map below |

Adding a source means one entry in `source.py`'s `SOURCES`: how to normalise
its list items, and what its file route needs. It does not mean touching
`bridge.py`. The requirement is that the service exposes
`/api/v1/activities/live` and `/api/v1/activities/{id}/file` — both answering
from the upstream rather than that project's local mirror, which a downloader
refreshes on its own schedule and so cannot see an activity from ten minutes
ago.

## The device map

The device is chosen **per activity, by canonical sport**. A Zwift ride should
arrive as an Edge and a Zwift run as a watch; claiming a bike computer recorded
a run is incoherent, and it would be invisible here — the upload would look
like every other success.

```bash
BRIDGE_PRODUCT_ID=4536            # the default device …
BRIDGE_GARMIN_SERIAL=<unit id>    # … and its serial
BRIDGE_DEVICE_CYCLING=2713:<unit id>   # overridden for cycling
```

`BRIDGE_DEVICE_<SPORT>` is `product_id:serial_number`. Canonical sports are
`running`, `cycling`, `swimming`, `walking`, `gym`, `snow`, `water`, `climb`,
`other` — `source.py` maps each upstream's own vocabulary onto them, so
`BRIDGE_SKIP_SPORTS=gym,swimming` means the same thing whoever recorded it.

**Every device must be registered to the Garmin account.** The serial is the
`unitId` from `GET /device-service/deviceregistration/devices`, and it is what
makes Garmin match the upload to a device you own. Without it the activity is
filed as manual with no device — the category Garmin excludes from badges.

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp coros.env.example coros.env      # then edit
cp zwift.env.example zwift.env
```

The two things that must be right in each config:

- **`BRIDGE_START_DATE`** — a hard floor. Nothing recorded before this day is
  ever uploaded. Required. Zwift in particular holds years of history; without
  it the first run would upload all of it at once.
- **`BRIDGE_ENABLED`** — leave it `false` until you have confirmed a single
  activity end to end. While false every scheduled run is a no-op, so cron can
  be installed immediately.

All three services are health-checked before a run does any work — one being
down would otherwise burn a retry attempt on every candidate:

| Service | Default port | Checked by |
|---|---|---|
| `fit-manager` | 7077 | `/api/v1/health` |
| `<source>-mcp` | per source | `/api/v1/health` |
| `garmin-mcp` | 8080 | `/api/v1/upload/health`, which probes the Garmin session |

## How an activity moves

1. **List** — `GET <source>-mcp /api/v1/activities/live`, everything between the
   cutoff and today. The service walks every page, so a short list means the
   upstream had nothing more.
2. **Filter** — drop anything already settled, anything below the start date,
   and any sport in `BRIDGE_SKIP_SPORTS`.
3. **Download** — `GET /api/v1/activities/{id}/file`, sha256 recorded and
   checked against the service's own header.
4. **Convert** — `POST fit-manager /api/v1/convert` with the device chosen for
   this activity's sport, plus `time_created`.
5. **Upload** — `POST garmin-mcp /api/v1/upload/fit`, which answers with the
   verdict already classified.
6. **Record** — `uploaded`, `duplicate`, `skipped` (all terminal) or `failed`.

`time_created` is the activity's start time, and it is not cosmetic. Garmin
identifies an upload by `file_id`'s `(serial_number, time_created)` pair, and
COROS stamps `time_created` at *export* time — so every activity in one sync
batch carries the same value. Since every file gets the same serial by design,
without this the batch is a single file to Garmin and everything after the
first comes back 409, which is terminal. It cost one activity of three on
2026-08-23 before it was found.

`failed` retries until `BRIDGE_MAX_ATTEMPTS`, then stops and shows up in
`--status`. One activity failing never aborts the run.

The exception is a service that stops answering mid-run. Nothing is then known
about the file in flight, so it keeps whatever status it had, the attempt it
spent is handed back, and the run aborts — three unlucky hours in a row must
not exhaust an activity that never got a verdict.

The query window is `max(BRIDGE_START_DATE, last successful run −
BRIDGE_LOOKBACK_DAYS)`, widened to cover anything already known and unfinished.
That last part matters: a run capped by `BRIDGE_MAX_PER_RUN` would otherwise
mark itself successful and move the cutoff past the activities it deferred.

## State

One database per bridge, defaulting to `<source>.db`.

```bash
sqlite3 coros.db "SELECT * FROM v_bridge_status"      # counts per status
sqlite3 zwift.db "SELECT * FROM v_bridge_activities"  # every activity
sqlite3 coros.db "SELECT * FROM v_bridge_failures"    # needs attention
sqlite3 coros.db "SELECT * FROM v_bridge_runs"        # run history
```

A database from before the rename is migrated in place on first open —
`coros_label_id` → `source_activity_id` and so on, plus the new `sport` and
`device_product_id` columns. It is idempotent and reports what it changed.

## Layout

| File | Role |
|---|---|
| `bridge.py` | Cron entrypoint: window, selection, orchestration, state |
| `source.py` | The source registry: one entry per upstream, and the HTTP client |
| `converter.py` | HTTP client for `fit-manager` `/api/v1/convert` |
| `garmin_sink.py` | HTTP client for `garmin-mcp` `/api/v1/upload/fit` |
| `schema/schema_bridge.sql` | State database — the single source of truth |
| `deploy/` | Optional systemd units, as an alternative to cron |

## Before trusting it

Tested against a genuine COROS recording on 2026-08-20. Garmin accepts the
upload and attributes it to the registered watch — same `deviceId` and
`deviceTypePk` as that watch's own recordings, and not flagged manual.

**Confirmed on 2026-08-28: bridged activities count toward challenge
progress.** Garmin's "2026 Running - Stage 3" challenge reported
`badgeProgressValue = 270577.63` m; the sum of every running activity in that
window is 270577.63 m across 24 activities, of which six — 56394.04 m — came
from this bridge. Native recordings alone fall exactly that much short. All six
also report `manualActivity: False` against the registered watch.

Read `GET /badgechallenge-service/badgeChallenge/non-completed` for live
progress. The earned-badge list clamps `badgeProgressValue` to the target and
so cannot show what contributed.

Getting there needed more than a `file_id` rewrite. Garmin matches an upload to
a registered device on `device_info`, which a COROS file barely populates; see
`CLAUDE.md` and `fit-manager` commit `88c6be3`. A Zwift file, by contrast,
already declares everything needed.

## Verification

1. `bridge.py --config <c> --dry-run` — lists candidates live, touches nothing.
2. `bridge.py --config <c> --only <id> --force` on **one** activity. Then check
   Garmin Connect: does it appear, is the device right, and — the actual point
   — **does it count toward badge/challenge progress?**
3. Re-run the same `--only` without `--force`: it must report the activity as
   already settled.
4. Confirm the activity did **not** appear in Strava, validating the
   no-re-export claim.
5. Let one hourly cron run complete and inspect `bridge_runs`.

Steps 1–3 were completed for COROS on 2026-08-20, and step 2's "does it count?"
was answered on 2026-08-28. Step 4 is still open, and nothing has been
confirmed end to end for Zwift.

## Related projects

| Project | Role |
|---|---|
| [`coros-mcp`](https://github.com/benniblau/coros-mcp) | A source. Owns the COROS login |
| [`zwift-mcp`](https://github.com/benniblau/zwift-mcp) | A source. Owns the Zwift login |
| [`garmin-mcp`](https://github.com/benniblau/garmin-mcp) | Destination — owns the garth session and `/api/v1/upload/fit` |
| [`fit-manager`](https://github.com/benniblau/fit-manager) | Conversion service on port 7077 |
