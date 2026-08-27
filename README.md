# coros-garmin-bridge

Keeps a Garmin Connect track record alive after switching to a COROS watch.

Garmin only counts activities recorded on Garmin devices toward badges and
challenges. This bridge pulls each new COROS activity as a FIT file, rewrites
its recording-device identity to a Garmin device you own, and uploads it to
Garmin Connect. One-way, COROS → Garmin, hourly from cron.

```
cron (hourly)
   │
   ▼
bridge.py ──► COROS API                     list new activities (live)
   ├───────► coros-mcp   /api/v1/activities/{id}/file    download FIT
   ├───────► fit-manager /api/v1/convert                 rewrite device identity
   ├───────► garmin-mcp  /api/v1/upload/fit              upload
   └───────► bridge.db                                   state, dedup, retries
```

Every hop is idempotent and every outcome is recorded, so an interrupted run
costs nothing and no activity is ever uploaded twice.

Three of the four hops are HTTP calls to services already running on this
machine, so the bridge holds no FIT editor, no COROS export logic and no Garmin
session of its own. Only the listing reaches an API directly, and it must:
`coros-mcp`'s REST API serves a local mirror that a downloader refreshes on its
own schedule, and an activity recorded in the last hour — exactly what this run
exists to catch — is not in it yet.

## Before trusting it

Tested against a genuine COROS recording on 2026-08-20. Garmin accepts the
upload and attributes it to the registered Garmin watch — same `deviceId` and
`deviceTypePk` as that watch's own recordings, and not flagged as a manual
activity.

That attribution is the precondition the project rests on, but the **badge
award itself has not been observed directly**: Garmin recomputes badge progress
asynchronously, and its monthly challenges report `userJoined: false` until you
join them. Confirm real badge progress before setting `BRIDGE_ENABLED=true`.

Getting there needed more than a `file_id` rewrite. Garmin matches an upload to
a registered device on `device_info`, which a COROS file barely populates; see
`CLAUDE.md` and `fit-manager` commit `88c6be3`.

## Layout

| File | Role |
|---|---|
| `bridge.py` | Cron entrypoint: window, selection, orchestration, state |
| `coros_source.py` | Lists activities live (`CorosClient`); fetches FIT bytes from `coros-mcp` |
| `converter.py` | HTTP client for `fit-manager` `/api/v1/convert` |
| `garmin_sink.py` | HTTP client for `garmin-mcp` `/api/v1/upload/fit` |
| `schema/schema_bridge.sql` | State database — the single source of truth |
| `deploy/` | Optional systemd unit and timer, as an alternative to cron |

Nothing is reimplemented that a sibling project already owns, and nothing that
needs a credential is held here. The FIT editor stays in `fit-manager`, the
COROS export dance in `coros-mcp`, the Garmin session in `garmin-mcp`; all
three are reached over HTTP. Only `CorosClient` is imported, from
`../coros-mcp`, because the listing has to be live.

The upload path is worth spelling out, because it moved: `garmin-mcp` owns the
garth session, the `/upload-service/upload/fit` call and the classification of
Garmin's answer. That means one Garmin credential store on the machine, and an
hourly bridge that cannot race the daily sync over the same session file.

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env      # then edit
```

The two things that must be right in `.env`:

- **`BRIDGE_START_DATE`** — a hard floor. Nothing recorded before this day is
  ever uploaded. It is required; without it the bridge could push years of
  imported Garmin history straight back into Garmin.
- **`BRIDGE_ENABLED`** — leave it `false` until the Garmin watch is retired.
  While it is false every scheduled run is a no-op, so cron can be installed
  immediately and the bridge switched on later by flipping one value.

All three services must be running before a run does anything, and each is
health-checked first — a service that is down would otherwise burn a retry
attempt on every candidate in the window:

| Service | Default port | Checked by |
|---|---|---|
| `fit-manager` | 7077 | `/api/v1/health` |
| `coros-mcp` | 8080 | `/api/v1/health` |
| `garmin-mcp` | 8080 | `/api/v1/upload/health`, which probes the Garmin session |

Their addresses come from `MCP_HOST` plus `COROS_MCP_PORT` / `GARMIN_MCP_PORT`,
so a machine running both already has them configured; `BRIDGE_COROS_API_URL`
and `BRIDGE_GARMIN_API_URL` override that. The bearer tokens must match
`COROS_MCP_AUTH_TOKEN` and `GARMIN_MCP_AUTH_TOKEN` in those checkouts.

The bridge keeps **no Garmin credentials at all** — not the session, not the
email and password. Those live in `garmin-mcp`.

## Usage

```bash
.venv/bin/python bridge.py                    # a normal hourly run
.venv/bin/python bridge.py --dry-run          # list candidates, touch nothing
.venv/bin/python bridge.py --status           # what the state database knows
.venv/bin/python bridge.py --only <label_id>  # exactly one activity
.venv/bin/python bridge.py --limit 3          # override BRIDGE_MAX_PER_RUN
.venv/bin/python bridge.py --since 2026-08-01 # widen this run's window
```

`--dry-run` writes nothing at all, not even to the state database, so it is
safe while the bridge is paused. `--only <id> --force` is the one way to make
the bridge act while `BRIDGE_ENABLED=false`; cron never passes `--only`, so the
pause switch still holds for scheduled runs.

## How an activity moves

1. **List** — every activity COROS reports between the cutoff and today.
2. **Filter** — drop anything already settled, anything below the start date,
   and any sport type in `BRIDGE_SKIP_SPORTS`.
3. **Download** — `GET coros-mcp /api/v1/activities/{id}/file`, sha256 recorded
   and checked against the `X-Coros-Sha256` header the service sends.
4. **Convert** — `POST fit-manager /api/v1/convert` with `manufacturer_id`,
   `product_id`, `serial_number` and `time_created`, sha256 recorded.
5. **Upload** — `POST garmin-mcp /api/v1/upload/fit`, which does the garth call
   and answers with the verdict already classified.
6. **Record** — `uploaded`, `duplicate`, `skipped` (all terminal) or `failed`.

`time_created` is the activity's start time, and it is not cosmetic. Garmin
identifies an upload by `file_id`'s `(serial_number, time_created)` pair, and
COROS stamps `time_created` at export time — so every activity in one sync
batch carries the same value. Since every file gets the same serial by design,
without this the batch is a single file to Garmin and everything after the
first comes back 409, which is terminal. It cost one activity of three on
2026-08-23 before it was found.

`failed` retries until `BRIDGE_MAX_ATTEMPTS`, then stops and shows up in
`--status` so it can be inspected rather than looping forever. One activity
failing never aborts the run.

The exception is a service that stops answering mid-run. Nothing is then known
about the file in flight, so it keeps whatever status it had, the attempt it
spent is handed back, and the run aborts — three unlucky hours in a row must
not exhaust an activity that never got a verdict.

The query window is `max(BRIDGE_START_DATE, last successful run −
BRIDGE_LOOKBACK_DAYS)`, widened to cover anything already known and unfinished.
That last part matters: a run capped by `BRIDGE_MAX_PER_RUN` would otherwise
mark itself successful and move the cutoff past the activities it deferred.

## State

```bash
sqlite3 bridge.db "SELECT * FROM v_bridge_status"      # counts per status
sqlite3 bridge.db "SELECT * FROM v_bridge_activities"  # every activity
sqlite3 bridge.db "SELECT * FROM v_bridge_failures"    # needs attention
sqlite3 bridge.db "SELECT * FROM v_bridge_runs"        # run history
```

## Deployment (10.10.1.224)

`/home/benni/coros-garmin-bridge`, running as `benni`, own venv:

```cron
25 * * * * cd /home/benni/coros-garmin-bridge && .venv/bin/python bridge.py >> bridge.log 2>&1
```

Hourly at :25, offset away from the daily syncs at :15. `deploy/` holds a
systemd service and timer as an alternative — use one or the other, not both.

The three services are separate units on the same host —
`fit-manager.service` (gunicorn, 7077), `coros-mcp.service` (8086) and
`garmin-mcp.service` (8080). The bridge health-checks all three before doing
any work, so if one is down a run aborts without spending a retry attempt on
every candidate.

## Verification

1. `bridge.py --dry-run` — lists candidates against live COROS, touches nothing.
2. `bridge.py --only <label_id> --force` on **one** activity. Then check Garmin
   Connect: does it appear, is the device shown as Fenix 8, and — the actual
   point — **does it count toward badge/challenge progress?**
3. Re-run the same `--only` without `--force`: it must report the activity as
   already settled. With `--force`, Garmin's 409 must come back as `duplicate`,
   not `failed`.
4. Confirm the activity did **not** appear in Strava, validating the
   no-re-export claim.
5. Let one hourly cron run complete and inspect `bridge_runs`.

Step 2 is the go/no-go, and it needs a genuine COROS recording — an activity
that was imported into COROS from Garmin is already in Garmin, so uploading it
back proves nothing except that duplicate detection works.

Steps 1–3 were completed on 2026-08-20 against the first real COROS recording.
Step 3 in particular: a forced re-run while the activity was already in Garmin
came back `duplicate`, not `failed`, and created no second copy. Steps 4 and 5
are still open.

## Related projects

| Project | Role |
|---|---|
| [`coros-mcp`](https://github.com/benniblau/coros-mcp) | Source — `CorosClient` for listing, `/api/v1/activities/{id}/file` for the bytes |
| [`garmin-mcp`](https://github.com/benniblau/garmin-mcp) | Destination — owns the garth session and `/api/v1/upload/fit` |
| [`fit-manager`](https://github.com/benniblau/fit-manager) | Conversion service on port 7077 |
