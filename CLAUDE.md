# coros-garmin-bridge — Claude Code Notes

## Project overview

Benni is switching from a Garmin Fenix 8 to a COROS VERTIX 2S permanently.
Garmin only counts activities recorded on **Garmin** devices toward badges and
challenges, so once the Fenix is retired his Garmin record stops advancing.

This bridge keeps it alive: pull each new COROS activity as a FIT file, rewrite
its recording-device identity to the Fenix 8 he actually owns, upload it to
Garmin Connect. One-way, COROS → Garmin, hourly from cron.

**Status: implemented, not yet deployed, premise still unverified.**
`README.md` describes what exists and how to verify it.
Everything up to the Garmin upload has been exercised against live COROS and a
live conversion service. The upload itself has never run — see "The premise is
unverified" at the bottom.

## Where this sits

Four related projects, all on GitHub under `benniblau`, all deployed to
`10.10.1.224` under `/home/benni/<project>`:

| Project | Role here |
|---|---|
| `../coros-mcp` | Source of activities. Import `CorosClient` from `coros_downloader.py` — it handles region discovery, token caching, `1019` re-login and retries. |
| `../garmin-mcp` | Destination auth. Reuse the garth session in `.garth/` and the `authenticate()` function in `export_fit.py`. |
| `../fit-file-manager-web` | Conversion service. Flask + gunicorn on port **7000**. The bridge calls it over HTTP; it does **not** import the editor. |
| `../strava-mcp` | Not used, but relevant: if Garmin re-exports uploads to Strava, activities will duplicate there. |

## Key facts established by prior investigation

Do not re-derive these; they cost a lot to establish.

### FIT device rewriting

- COROS manufacturer id is **294**; Garmin is **1**. Fenix 8 product id is
  **4536** (`fenix_8` in `garmin_devices.json`).
- The real Fenix 8 serial lives in `.env` as `BRIDGE_GARMIN_SERIAL` and is
  deliberately not in this repo, which is public. Preserve it on every upload,
  so Garmin sees a device registered to the account.
- `fit_targeted_editor.py` was audited against the FIT spec and fixed
  (commit `7649483`). It handles developer-data fields, compressed-timestamp
  headers, endianness and declared field widths. Before that fix it silently
  misparsed any file with developer data.
- The editor relabels **only the recording device**. Paired sensors and device
  timestamps are preserved deliberately — an earlier version flattened them and
  produced files COROS accepted but computed nothing for.
- Real COROS product ids, decoded from genuine recordings: 804 PACE 3, 805 PACE
  Pro, 806 PACE 4, 822 APEX 2 Pro, 831 VERTIX, 832 VERTIX 2, 841 APEX Pro, 851
  DURA. **VERTIX 2S is not among them and its id is unknown.**

### Garmin upload

- Endpoint: `POST /upload-service/upload/fit` on `https://connectapi.garmin.com`,
  multipart field `file`, Bearer auth (garth adds it with `api=True`).
- **HTTP 409 means duplicate.** garth calls `raise_for_status()`, so a duplicate
  arrives as `GarthHTTPError` — unwrap `err.error.response.status_code`.
- Response envelope is `detailedImportResult` with `successes` / `failures`;
  the human-readable reason is at `failures[0].messages[0]`.
- `garth.upload()` needs a real file handle — a bare `BytesIO` fails because it
  reads `fp.name`.

### COROS behaviour

- COROS stores uploaded FIT files **byte-for-byte**; it does not re-encode.
- Training load is computed by a **periodic batch job (~15 min)**, not per
  upload, and values are refined across passes. Never judge an upload's result
  immediately — this wasted hours of investigation.
- Per COROS support: for imported FIT files, EvoLab only updates from the
  **previous 42 days**.
- COROS renames activities on import, geocoding a title from sport and location.
  `../coros-mcp/restore_titles.py` reverses that.

### Decisions made during implementation

Not in the original design, and each one was arrived at the hard way.

- **The conversion endpoint is `POST /api/v1/convert`** in
  `fit-file-manager-web` (blueprint `app/blueprints/api_v1.py`). It takes
  `manufacturer_id` + `product_id` (or `device_name`) plus `serial_number`,
  returns raw FIT bytes, and distinguishes `400` bad request, `422` conversion
  refused (`reason` is `no_device_messages` or `already_in_target_format`) and
  `500` genuine fault. `converter.py` branches on exactly that: a 4xx is a
  verdict on the file, a 5xx or a connection error is a verdict on the service.
- **The run window heals itself.** The cutoff is
  `max(BRIDGE_START_DATE, last successful run − BRIDGE_LOOKBACK_DAYS)`, then
  widened to cover the oldest activity still `pending` or retryable. Without
  that, a run capped by `BRIDGE_MAX_PER_RUN` marks itself successful and moves
  the cutoff past the activities it deferred, which loses them permanently.
  Every activity in the window is therefore recorded *before* the cap applies.
- **Attempts are counted before the attempt**, so a file that crashes the
  process cannot be retried hourly forever.
- **`--only <id> --force` is the one exception to `BRIDGE_ENABLED=false`.**
  Cron never passes `--only`, so the pause switch still holds for every
  scheduled run; this is how the single-activity go/no-go test gets run while
  the bridge is otherwise paused. `--dry-run` writes nothing at all, not even
  to the state database.
- **Service failures abort the run, activity failures do not.** The converter
  is health-checked before any work, because a service that is down would
  otherwise burn one retry attempt on every candidate in the window.
- **`export_fit.py` exits the process** at import time when garth is missing
  and inside `authenticate()` when credentials are. `garmin_sink.authenticate()`
  converts that `SystemExit` into a `SinkError`; without it the run dies past
  every handler and leaves its `bridge_runs` row saying `running`.

## Conventions to follow

Match the sibling projects:

- Two-component split where applicable; `schema/schema_*.sql` as the single
  source of truth, loaded with `Path.read_text()` + `executescript()`.
- Every DDL statement `IF NOT EXISTS`; **views dropped and recreated** so fixes
  reach existing databases.
- Resumable ledger or state table for anything long-running, so an interrupted
  run costs nothing.
- `print()` with the emoji vocabulary (✅ ⚠️ ❌ 📥 📦 🗑️) in CLI tools;
  `logging` to stderr in servers.
- Env vars namespaced `BRIDGE_*`; `load_dotenv()` at import.
- `.env`, `*.db`, `*.fit`, `*.log` and run state must be gitignored — the repos
  are public and this data is personal.

## Safety invariants

- **`BRIDGE_ENABLED=false` makes every run a no-op.** This is how the bridge
  stays paused during the overlap week while cron is already installed.
- **`BRIDGE_START_DATE` is a hard floor.** Nothing older is ever uploaded. Its
  job is to stop the bridge backfilling Garmin's own history.
- Terminal states (`uploaded`, `duplicate`, `skipped`) are never retried.
- One activity failing must never abort the run.
- `BRIDGE_MAX_PER_RUN` bounds the blast radius of a bug.

## The premise is unverified

Whether Garmin actually awards badges for an uploaded file claiming a registered
device **has never been tested** — and it is the entire point of the project.
Test it with a single activity (`--only <label_id>`) and confirm badge progress
before trusting the bridge. Be prepared for the answer to be no.

Nothing can be fully validated until a genuine VERTIX 2S recording exists;
every activity currently in COROS is an imported Garmin file. That is worse
than it sounds for testing: those files came *from* Garmin, so uploading one
back can only ever produce a 409 duplicate. It exercises the pipeline, but it
cannot answer the badge question, and it is not worth the noise in the account.
Wait for a real recording.

What has been verified, so the next session does not redo it:

- The device rewrite, against the official `garmin_fit_sdk` decoder:
  `check_integrity()` true, zero decode errors, `file_id` showing
  garmin/fenix8 and the configured serial, paired sensors untouched,
  every `record`, `lap`,
  `session` and `split` message identical to the source, 38–90 bytes changed
  out of ~745 kB. (Watch out when diffing decoded messages: unset float fields
  are NaN, and NaN != NaN, so a naive comparison reports every one of them.)
- `POST /api/v1/convert` end to end, including its 400/401/413/422 paths, and
  that the old `/api/convert` and the web UI still work.
- The bridge's listing, selection, capping, skip list, retry accounting and
  exhaustion, against live COROS.
- The full pipeline — COROS download → convert → state — with the upload
  stubbed out.
- Garmin's response classification (409, duplicate-in-body, failure message,
  success) against fakes, since it cannot be exercised without uploading.
