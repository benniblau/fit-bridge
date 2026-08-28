# fit-bridge — Claude Code Notes

## Project overview

Garmin only counts activities recorded on **Garmin** devices toward badges and
challenges. This bridge keeps a Garmin record advancing anyway: pull each new
activity from a source as a FIT file, rewrite its recording-device identity to
a Garmin device the account actually owns, upload it to Garmin Connect.
One-way, hourly from cron.

It began as `coros-garmin-bridge`, for Benni's switch from a Fenix 8 to a COROS
VERTIX 2S. It was renamed and re-scoped on 2026-08-28 when Zwift became a second
source. **One instance per bridge** — `--config coros.env`, `--config
zwift.env` — each with its own state database, pause switch, start date and
cap, so no bridge can lose another's activities or spend its retries.

**Status: COROS bridge live on 10.10.1.224, `BRIDGE_ENABLED=true`, hourly from
cron. Premise CONFIRMED on 2026-08-20; badge award itself still unconfirmed.
Zwift bridge built and verified as far as conversion, never uploaded.**

Garmin attributes an upload to a real device only when the file carries a
Garmin-shaped `device_info` message; the `file_id` rewrite alone yields a
*manual activity with no device*. Read "The premise was tested on 2026-08-20 and
it HOLDS" near the bottom first.

The first multi-activity day, 2026-08-23, then lost one run of three to a
second identity collision — see "Garmin identifies an upload by
(serial_number, time_created)". Read that before touching the conversion call.

## Adding a source

One entry in `source.py`'s `SOURCES`: how to normalise the upstream's list
items, and what its file route needs to reach an activity the mirror has not
synced. `bridge.py` is not touched.

The contract is that the source's MCP exposes both
`/api/v1/activities/live?start_day=&end_day=` and
`/api/v1/activities/{id}/file`, **answering from the upstream rather than that
project's local mirror**. The mirror is refreshed by a downloader on its own
schedule, so an activity from ten minutes ago is not in it — which is exactly
what an hourly bridge exists to catch.

Those routes pass the upstream's payload through unchanged, deliberately, so
normalising it is the bridge's job and happens in one place per source. The
canonical sport vocabulary (`running`, `cycling`, …) exists because the device
is chosen by sport and `BRIDGE_SKIP_SPORTS` has to mean the same thing whoever
recorded the activity.

### Devices are per-sport, and that is not cosmetic

`BRIDGE_DEVICE_<SPORT>=product_id:serial_number` overrides `BRIDGE_PRODUCT_ID`
/ `BRIDGE_GARMIN_SERIAL`. Zwift records both rides and runs; a run attributed to
an Edge 1030 is incoherent, and the failure would be **invisible** — the upload
succeeds and looks like every other one. Registered devices, confirmed against
`GET /device-service/deviceregistration/devices` (field `unitId`, and the part
number carries the product id: `006-B4536-00` → 4536, `006-B2713-00` → 2713):

| Device | product | part number | used for |
|---|---|---|---|
| fenix 8 - 51mm, AMOLED | 4536 | 006-B4536-00 | everything by default |
| Edge 1030 | 2713 | 006-B2713-00 | `BRIDGE_DEVICE_CYCLING` |

Serials are `unitId` and stay out of this repo, which is public.

### Zwift is the easy case, unlike COROS

Established 2026-08-28 by decoding a real Zwift FIT and converting it:

- Zwift writes **exactly one** `device_info`, already carrying `device_index`
  (creator), `manufacturer`, `product` and a real `serial_number` — the three
  fields COROS omits. `add_missing_identity_fields()` does not even need to
  fire, and conversion is **byte-neutral** (175,027 → 175,027) where COROS
  costs +23.
- `file_id.serial_number` is absent, as with COROS, so that one field is still
  added.
- Zwift stamps `time_created` per activity (5s before `session.start_time`), so
  it has **none** of the COROS sync-batch collision problem.
- Garmin holds **no** cycling activities, so Zwift is not auto-syncing and
  there is no duplication risk.
- Zwift's `sport` is `CYCLING`/`RUNNING`; 195 vs 105 in the history.
- Its list pages newest-first by offset with **no date filter**, so zwift-mcp
  walks back until a whole page predates the window. Days use the activity's
  own `utcOffsetMinutes` — a 00:30 CEST ride belongs to the previous UTC day.
- 13 Zwift FITs are already flagged unreadable by the downloader; those will
  fail conversion and park, which is the designed behaviour.

**There have been no Zwift activities since 2026-02-26.** The sync is current,
so that is real, not stale data. The Zwift bridge is built ahead of the season.

## Where this sits

Four related projects, all on GitHub under `benniblau`, all deployed to
`10.10.1.224` under `/home/benni/<project>`:

| Project | Role here |
|---|---|
| `../zwift-mcp` | Source, over HTTP on port **8087**. Same two routes. Its file route takes an explicit `bucket`/`key` where coros-mcp takes `sport_type`, for the same reason. **No upload endpoint exists or can** — Zwift has no activity-import API. |
| `../coros-mcp` | Source, over HTTP on port **8086**. `GET /api/v1/activities/live` for the listing, `GET /api/v1/activities/{label_id}/file` for the bytes. It owns the COROS login, the shared token cache, region discovery, `1019` re-login and retries — none of which the bridge sees. |
| `../garmin-mcp` | Destination. Owns the garth session, the upload call and the classification of Garmin's answer, behind `POST /api/v1/upload/fit` on port **8080**. The bridge holds no Garmin credentials. |
| `../fit-manager` | Conversion service. Flask + gunicorn on port **7077**. The bridge calls it over HTTP; it does **not** import the editor. |
| `../strava-mcp` | Not used, but relevant: if Garmin re-exports uploads to Strava, activities will duplicate there. |

**All four hops are HTTP calls to services already running on the host, and the
bridge holds no credentials at all.** It imports nothing from a sibling
checkout.

Both COROS hops deliberately bypass coros-mcp's local mirror, which a
downloader refreshes on its own schedule: an activity recorded in the last hour
— exactly what an hourly run exists to catch — is not in it yet. `/api/v1/
activities/live` proxies COROS's own list endpoint for that reason, and the
file route takes an explicit `sport_type` for the same one, so an activity the
mirror has never seen still exports.

The earlier argument for keeping `CorosClient` imported here was that the REST
API served the mirror. That was an argument against *that endpoint*, not
against any endpoint — adding a live one removed the last credential from this
repo, which is public.

Service addresses come from `MCP_HOST` + `COROS_MCP_PORT` / `GARMIN_MCP_PORT`,
which the deployed `.env` already carried; `BRIDGE_COROS_API_URL` and
`BRIDGE_GARMIN_API_URL` override. Tokens fall back to `COROS_MCP_AUTH_TOKEN`
and `GARMIN_MCP_AUTH_TOKEN` so one `.env` holds a single copy of each.

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
  Pro, 806 PACE 4, 822 APEX 2 Pro, 831 VERTIX, 832 VERTIX 2, **833 VERTIX 2S**,
  841 APEX Pro, 851 DURA. 833 was decoded on 2026-08-20 from the first genuine
  VERTIX 2S recording (COROS label `479760828581576905`).

### The serial is NOT written into a genuine COROS recording

Established 2026-08-20, on the first real VERTIX 2S file. This contradicts what
the "verified" list at the bottom of this file used to claim.

- A genuine COROS `file_id` carries `type`, `manufacturer`, `product`,
  `time_created` and `product_name` — and **no `serial_number` field at all**.
- `fit_targeted_editor.py` rewrites fields **in place**. Its guard is
  `loc['serial_number_offset'] is not None` (around line 419): where the field
  is absent it silently writes nothing. It cannot add a field, because that
  means resizing the record definition.
- So converting a real COROS file yields `file_id` = garmin/4536 with **no
  serial**, plus a leftover `file_id.product_name` and a `device_info` message
  both still reading `COROS VERTIX 2S`.
- `POST /api/v1/convert` still answers `200` and still echoes
  `X-Fit-Serial-Number: <requested>` — that header reflects the *request*, not
  what was written, so neither the service nor `converter.py` notices.
- Why this was missed: every earlier test used a COROS activity **imported from
  Garmin**. Those are Garmin's own recordings stored byte-for-byte, so their
  `file_id` already carried the real `serial_number` and garmin/4536. The
  serial appeared to survive because it was never actually written.
- The real Fenix 8 identity, confirmed against both the device registration
  (`GET /device-service/deviceregistration/devices`, field `unitId`) and that
  watch's own FIT files (`file_id.serial_number`): product **4536**, part number
  006-B4536-00, "fenix 8 - 51mm, AMOLED". The unit id itself is the serial and
  stays out of this repo — it lives in `.env` as `BRIDGE_GARMIN_SERIAL`.

### Garmin upload

**This all lives in `../garmin-mcp/garmin_files.py` now**, behind
`POST /api/v1/upload/fit`. The bridge no longer imports garth, holds no session
and does no classification — `garmin_sink.py` is a thin HTTP client. Change
this behaviour there, not here.

- Endpoint: `POST /upload-service/upload/fit` on `https://connectapi.garmin.com`,
  multipart field `file`, Bearer auth (garth adds it with `api=True`).
- **HTTP 409 means duplicate.** garth calls `raise_for_status()`, so a duplicate
  arrives as `GarthHTTPError` — unwrap `err.error.response.status_code`.
- Response envelope is `detailedImportResult` with `successes` / `failures`;
  the human-readable reason is at `failures[0].messages[0]`.
- `garth.upload()` needs a real file handle — a bare `BytesIO` fails because it
  reads `fp.name`.
- garth's default timeout is **10 seconds for every request**, and it passes
  that to requests itself — so a `timeout=` kwarg on `garth.client.post()`
  collides with it and raises `TypeError`. Set it with
  `garth.client.configure(timeout=...)` instead.

The service's own contract, which is what the bridge actually sees: **200 for
anything Garmin decided**, with `status` = `uploaded` | `duplicate` | `failed`;
**4xx** for a bad request; **503** when Garmin never answered. Only the last is
retryable, and only the last aborts a run.

### Garmin identifies an upload by (serial_number, time_created)

Established 2026-08-23, the hard way: three runs recorded that morning, two
uploaded, one lost.

Both fields live in `file_id`. A Garmin watch stamps `time_created` when the
recording starts, so it is unique per activity. **COROS stamps it when the file
is exported**, so every activity in one sync batch carries the same value:

    ...310137  2.14 km   time_created 09:47:21Z   session start 06:37:59Z
    ...310138 20.97 km   time_created 09:47:21Z   session start 07:30:19Z
    ...310139  3.01 km   time_created 09:47:22Z   session start 09:25:36Z

The bridge writes the same serial into all of them by design, so to Garmin
`...310138` was the same file as `...310137`. It came back **409, and 409 is
terminal** — `duplicate` is never retried, so the run was lost silently and
permanently. `...310139` survived only because the sync clock ticked over.

Nothing was wrong with that file: re-converting it reproduced the recorded
`converted_sha256` exactly, `device_info` and all.

The fix is to pass the recording's start time as `time_created` — which is what
a real Fenix 8 file carries (verified on activity `24010567666`: `time_created`
and `session.start_time` are both `13:28:10Z`). COROS's list `startTime` is
exactly the session start, so the bridge already has the value. See
`fit-manager` commit `ae6d40f`; the editor writes it in place and
**raises rather than skip a field it cannot write**, so a 200 now means the
value actually reached the file.

The general lesson, twice over now: this project rewrites files to a single
device identity, and *anything* Garmin uses to tell two files apart has to be
made unique deliberately. And a converter that echoes a requested value back in
a response header teaches you nothing — that is how the missing `device_info`
survived for weeks.

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

- **Every hop that needs domain knowledge is a service, not an import.** The
  FIT editor was already behind fit-manager; the COROS export, the Garmin
  upload and finally the COROS listing followed on 2026-08-27. What this
  bought: one Garmin credential store on the machine instead of two processes
  sharing a session file (the hourly bridge could otherwise race the daily sync
  over it), one copy of the two-hop signed-URL export, **no credentials in this
  public repo at all**, and a venv that needs only `requests` and
  `python-dotenv`. What it cost: the bridge depends on three services being up,
  which is why all three are health-checked before a run does any work.
  `coros_source.download_fit` verifies the `X-Coros-Sha256` header against the
  bytes that arrived, because they now cross a network.
- **The `.env` holds two bearer tokens and no credentials.** They authorise the
  bridge to coros-mcp and garmin-mcp and nothing else — neither is a COROS or
  Garmin credential, and neither reaches either account directly. `COROS_USER`
  mattered longer than it looked: it is the *cache key* for the shared token
  file, not just a login field, so it was needed on every run and not only at
  expiry. It is gone now along with the login itself.
- **A service failing mid-run refunds the attempt it spent.** `count_attempt`
  runs before the attempt so a file that crashes the process cannot loop
  forever — but an activity that got no verdict because garmin-mcp was down
  learned nothing about itself, and three unlucky hours in a row must not
  exhaust it. `SinkError` is therefore the one exception to "process() never
  raises": it propagates, `refund_attempt()` undoes the increment, and the run
  aborts with the activity untouched.
- **The conversion endpoint is `POST /api/v1/convert`** in
  `fit-manager` (blueprint `app/blueprints/api_v1.py`). It takes
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
  and inside `authenticate()` when credentials are. That trap now belongs to
  `garmin_files.authenticate()` in garmin-mcp, which converts the `SystemExit`
  into an `UploadError` — otherwise a missing credential takes down the whole
  MCP server, not just one request.

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

## The premise was tested on 2026-08-20 and it HOLDS — with a bigger rewrite

The first genuine VERTIX 2S recording (`479760828581576905`, 13.78 km) was put
through the bridge and uploaded three times. The third attempt produced an
activity Garmin attributes to the real Fenix 8, indistinguishable from one the
watch recorded itself:

| upload | `deviceMetaDataDTO` | `manualActivity` |
|---|---|---|
| 1 — converter as-is | `{deviceId: "0", deviceTypePk: 19, deviceVersionPk: 80}` | `True` |
| 2 — plus `file_id.serial_number` | `{deviceId: "0", deviceTypePk: 19, deviceVersionPk: 80}` | `True` |
| 3 — plus a Garmin-shaped `device_info` | `{deviceId: "<the real unit id>", deviceTypePk: 37161, deviceVersionPk: 1004425}` | `False` |
| *a real Fenix 8 recording* | `{deviceId: "<the same unit id>", deviceTypePk: 37161, deviceVersionPk: 1014322}` | `False` |

**`device_info` is what Garmin matches on, not `file_id`.** Rewriting `file_id`
alone — which is all `/api/v1/convert` does — produces a *manual activity with
no device*, the category Garmin excludes from badges. Adding
`file_id.serial_number` changes nothing on its own. What flips it is a
`device_info` message carrying `device_index=0` (creator), `manufacturer=1`,
`product=4536` and `serial_number=<the registered unit id>`.

### Why the current converter cannot do this

A genuine COROS recording declares far less than a Garmin one:

    COROS   file_id     {type, manufacturer, product, time_created, product_name}
            device_info {timestamp, manufacturer, product_name}          3 fields
    Garmin  file_id     {serial_number, time_created, 7, manufacturer, product, number, type}
            device_info {…28 fields, incl. device_index, serial_number, product}

`fit_targeted_editor.py` rewrites fields **in place** and cannot add one — its
serial guard is `loc['serial_number_offset'] is not None` (around line 419), so
where the field is absent it silently writes nothing. It also never touches this
`device_info` at all, because it only considers messages that declare *both*
manufacturer and product, and the COROS one has no product field.

Worse, `/api/v1/convert` still answers `200` and still echoes
`X-Fit-Serial-Number: <requested>` — that header reflects the request, not the
file — so neither the service nor `converter.py` can tell it did nothing.

Every earlier test used a COROS activity **imported from Garmin**. Those are
Garmin's own recordings stored byte-for-byte, so they already had all of this.
The serial looked preserved because it was never written.

### The fix, in `fit_targeted_editor.py`

`add_missing_identity_fields()` runs before anything is patched. It declares the
fields the source omits — `file_id.serial_number`, and `device_info`'s
`device_index`, `product` and `serial_number` — which means growing those
definition messages and every data record using them, then repairing
`data_size`. It also blanks the leftover `product_name` ("COROS VERTIX 2S")
whenever the device actually changes.

Values are **seeded, not final**: `serial_number` goes in as the invalid
sentinel, and `device_info.product` is seeded with `file_id`'s *current* product
so the existing in-place pass recognises the message as the recording device
rather than a paired sensor and fills everything in. That way the audited
in-place code is untouched and does all the actual writing.

Guard: `device_info` is only augmented when the file has **exactly one**
`device_info` record naming the same manufacturer as `file_id`. With several
there is no way to tell the watch from a paired sensor without a `device_index`,
and mislabelling a heart-rate strap as the watch is worse than doing nothing.

`find_message_byte_locations_in_data()` was split out of
`find_message_byte_locations()`, because augmentation moves every offset after
`file_id` and the locations must come from the augmented buffer.

Verified:

- Output is **byte-identical** to the file Garmin accepted and attributed.
- Against the pre-change editor, output is **byte-identical on every
  Garmin-shaped file** (a real Fenix 8 recording relabelled to itself and to an
  Edge 1030, and a Garmin-origin COROS activity). Only the genuine COROS file
  differs, by +23 bytes.
- Over HTTP: `POST /api/v1/convert` returns those same bytes, by
  `manufacturer_id`/`product_id` and by `device_name`, and still answers 400 for
  a bad request, 400 for a non-FIT upload, and 422 `already_in_target_format`
  when fed its own output.

This lives in `fit-manager` on purpose. The bridge reaches the editor
over HTTP and does not carry its own copy of FIT logic — see "Conventions to
follow".

Remaining cosmetic gap: `deviceVersionPk` is 1004425 rather than 1014322,
because the COROS file has no `file_creator` message. A real Fenix 8 file
carries `file_creator{software_version: 2241}`. Adding it would make the two
identical; it did not affect device attribution.

### Still to confirm

Badge and challenge *award* was not directly observed — Garmin recomputes
asynchronously, and the monthly challenges (`August Rundown` and friends) report
`userJoined: false`, so they track no progress until joined. What is established
is the precondition the whole project rests on: the upload is attributed to the
registered Fenix 8 and is not flagged manual. Confirm an actual badge before
switching `BRIDGE_ENABLED` on.

### Other things that surfaced during the test

- `POST /upload-service/upload/fit` answers **202 with no import result**, so
  the classifier takes its "Accepted with no import result reported" branch and
  `garmin_activity_id` is **never populated**. The upload id is recorded; the
  activity id is not. Resolving it needs a follow-up poll. (That classifier is
  `garmin_files._classify()` in garmin-mcp since 2026-08-27; it was
  `garmin_sink._classify()` here.) Confirmed again on 2026-08-27: 202, upload
  id `475376063944`, no activity id.
- Garmin's activity list is briefly stale after a `DELETE` (204).
- A deleted activity can be re-uploaded: all three attempts went through after
  deleting the previous one, with no 409.
- **Garmin's 409 was finally exercised for real.** A full
  `bridge.py --only … --force` against the patched converter, while the activity
  was already in Garmin, came back `duplicate` and not `failed`, and created no
  second copy — README verification step 3, previously only tested against
  fakes. Note what that does and does not prove: the 409 fired because the
  re-uploaded file carried the *same* `(serial_number, time_created)` pair. A
  file for the same run without that pair is not recognised as a duplicate at
  all (see the fitfiletools note below).
- The external device changer at fitfiletools.com/changer was compared against
  this implementation. It also adds `device_info.product`, but writes **no
  serial and no `device_index`** and leaves the "COROS VERTIX 2S" strings in
  place. It additionally writes `enhanced_speed`/`enhanced_altitude` (record
  fields 73/78) and the session equivalents (124/125), which costs +19% file
  size and is semantically redundant — the FIT profile derives those from the
  legacy 16-bit `speed`/`altitude`, and the SDK reports identical values for the
  unmodified COROS file.

  **Its output is NOT attributed correctly — tested 2026-08-27.** Uploading it
  produced `{deviceId: "0", deviceTypePk: 19, deviceVersionPk: 80}` and
  `manualActivity: True`: the excluded category, the same result as attempts 1
  and 2 in the table above. The serial is what Garmin needs, and this tool does
  not write one. The activity (`24137833590`) was deleted.

  Two things fell out of that upload, both confirming the identity model:
  Garmin did **not** answer 409 even though it already held a byte-equivalent
  recording of the same run, because with no serial the
  `(serial_number, time_created)` pair could not match — and a re-upload of an
  activity Garmin already has is therefore *not* reliably a duplicate. It
  depends entirely on that pair.

  **`coros_modified_to_garmin.fit` in the repo root is this tool's output, not
  the bridge's.** It is 209,013 bytes; converting the same recording through
  `/api/v1/convert` gives 175,712 (the source is 175,689, +23 for the added
  identity fields). Do not reach for that file as "the file Garmin accepted" —
  it is the file Garmin filed as manual.
- `Decoder.check_integrity()` consumes its `Stream`; call it on its own stream
  or a following `read()` silently returns nothing.

## The original premise, as written before the test

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
  garmin/fenix8, paired sensors untouched, every `record`, `lap`,
  `session` and `split` message identical to the source, 38–90 bytes changed
  out of ~745 kB. (Watch out when diffing decoded messages: unset float fields
  are NaN, and NaN != NaN, so a naive comparison reports every one of them.
  And call `check_integrity()` on its own `Stream` — it consumes the stream, so
  a `read()` afterwards silently returns nothing.)
  **The "and the configured serial" part of this claim was wrong**, and only
  held because the file under test came from Garmin already. See "The serial is
  NOT written into a genuine COROS recording" above, and note that the serial
  alone turned out not to be what Garmin matches on anyway.
- On the first genuine VERTIX 2S recording (2026-08-20,
  `479760828581576905`, 175,689 bytes): listing, download, convert and the
  state database all work. The conversion changes exactly 6 bytes —
  `file_id.manufacturer` 294→1, `file_id.product` 833→4536, and the file CRC —
  and the result decodes clean. **Not yet uploaded**, because it is the only
  real recording that exists and Garmin will 409 every later attempt, so it is
  a one-shot go/no-go that should not be spent on a file missing the serial.
- `POST /api/v1/convert` end to end, including its 400/401/413/422 paths, and
  that the old `/api/convert` and the web UI still work.
- The bridge's listing, selection, capping, skip list, retry accounting and
  exhaustion, against live COROS.
- The full pipeline — COROS download → convert → state — with the upload
  stubbed out.
- Garmin's response classification (409, duplicate-in-body, failure message,
  success) against fakes, since it cannot be exercised without uploading.
