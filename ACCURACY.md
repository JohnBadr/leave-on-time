# Running the ETA recorder and checking improvements

The existing OSRM route-building approach is retained. The new tracker uses
distance along those paths, real observation intervals, separate arrival and
departure events, and history combined with recent fleet conditions. Completed
travel and stop samples are saved immediately, including long waits. Data from
other route versions and the old, unversioned measurement tables is excluded
from the new predictor; the old tables are preserved.

## Finish the current updater first

An updater already running continues to execute the code it loaded at startup.
The changes in this checkout do not migrate its open database or restart it.
Do not start a second updater while the old one is working.

After it finishes, stop any old tracker/logger processes and make a backup of
the databases. Then initialize the route version columns without network calls:

```powershell
.\venv\Scripts\python.exe precomputing-logic/update_routes.py --migrate-only
```

Existing layouts start at version 1. Subsequent runs increment only routes whose
ordered stops, coordinates, or fetched geometry/timing change. Repeated stop
visits retain separate positions. Each route replacement and its version are
saved in the same transaction. Unchanged updates keep the version.

Continue running the updater weekly as before. `--refresh-geometry` also checks
road paths when stops have not changed. Use it after a known path change or
detour; an ordinary update cannot discover a road-only change from stop order.
`--dry-run` opens the graph read-only and does not migrate it.

The recorder reads routes without writing to routegraph.db and reloads them
every 90 seconds. An updated layout or trip assignment resets tracking and
discards any unfinished measurement spanning the change. If the graph has not
yet been migrated, a content-based legacy identity prevents silently mixing
different layouts; collection after the migration starts a separate history.

## Test and record locally

Use Python 3.10+ and the packages in requirements.txt. On Windows, the existing
virtual environment already has the runtime packages used by these commands.

```powershell
.\venv\Scripts\python.exe -B -m unittest discover -s tests -v
.\venv\Scripts\python.exe logger.py --duration 3600
```

Omit `--duration` for continuous recording. Stop with Ctrl+C. All provider bus
fields, including the original vehicle payload, are saved before inference;
unknown, repeated, stale, and out-of-service reports are retained too. The
output defaults to bullrunner.db. The recorder never writes tracking.db.

The recorder retries failed feeds with capped backoff and bounded HTTP requests.
Every new sample is committed rather than waiting for shutdown. On restart it
loads up to eight weeks of the new, versioned history. Calendar features use
America/New_York and each event's date, including across daylight-saving changes.

A provider `created` value is retained without assuming it means GPS fix time.
By default observations use receipt times and predictions are marked uncertain.
Identical repeated reports cannot establish continued movement or a driver hold.
After checking the field against real in-service reports, `--trust-created-time`
allows a timezone-qualified or epoch timestamp to be used as source time.
A timezone-free string still uses receipt time rather than guessing its timezone.

Predictions are recorded every 15 seconds for every stop on each observed route.
Missing predictions and interrupted journeys have explicit statuses. Route
changes, feed loss, timeouts, and shutdown close pending journeys. The timestamp
of a bus's physical arrival is distinct from its later departure.

## Score predictions and replay the same journeys

```powershell
.\venv\Scripts\python.exe evaluation.py report --db bullrunner.db --quality automatic
.\venv\Scripts\python.exe replay.py --input bullrunner.db --output replay-001.db
.\venv\Scripts\python.exe evaluation.py report --db replay-001.db --quality automatic
```

`shadow_tester.py` uses the same recorder with an isolated default output file,
`observations_shadow_test.db`. Repeat `--system-id` for a chosen cohort, or use
`--random-systems 4 --seed 42` to choose a stable random cohort from the graph.
This removes the old 70-minute exclusion period and two-vehicle selection limit.
The old shadow tables remain untouched; new results use the documented tables.

Replay requires a new output filename, makes no network calls, and opens its
source read-only. It reconstructs the saved route snapshots and processes raw
reports in receipt order. Later observations never become available to earlier
predictions. Use `--evaluate-from UNIX_SECONDS` to let early days build history
while scoring only the later period. Use the same records and evaluation date
when comparing code versions; keep each replay output separately.

The scorecard reports missing and interrupted predictions, typical errors,
90th-percentile errors, large misses, and signed bias. Positive bias means the
bus arrived later than predicted. Predicted-time and actual-time horizons are
reported separately. Collector errors and empty-service polls are counted too.

Automatic labels come from independent proximity and approach checks, not the
tracker's current segment. They remain provisional: a fresh report within 20 m
of a stop, following an observed approach from outside 35 m, bounds an arrival.
Ambiguous repeated locations and arrivals hidden inside feed gaps stay unscored.
Manually inspect representative raw traces, including large misses and holds.

Record an audited arrival with UTC Unix seconds and the start of that particular
journey's prediction window (after the previous visit to the same stop):

```powershell
.\venv\Scripts\python.exe evaluation.py label --db bullrunner.db --vehicle-id BUS_ID --route Red --version 1 --target-index 5 --arrival-time ARRIVAL_UNIX_SECONDS --prediction-start WINDOW_START_UNIX_SECONDS
.\venv\Scripts\python.exe evaluation.py report --db bullrunner.db
```

`target-index` identifies the ordered destination occurrence in a saved route
snapshot, not a stop ID. A manual label can correct provisional labels within
that explicit window. Public prediction ranges remain unset until at least 30
separate manually checked arrivals in the relevant route/version/horizon are
available. Each range describes 10th–90th percentile total-trip error, rather
than adding separate stop uncertainty ranges. Display smoothing is not treated
as having independently calibrated ranges.

The engine already uses historical segment and stop times, recent fleet
corrections, future time-of-day along the trip, and remaining hold time conditioned
on elapsed waiting. Sparse histories use broader route averages and marked
fallbacks. No trained ML model or improved real-world accuracy is claimed until
representative in-service data is collected and held-out days show improvement.

## Run on a Raspberry Pi or Linux server

Copy this checkout and the updated routegraph.db to the machine. Create a venv
with `python3 -m venv venv`, install `requirements.txt`, and first run logger.py
in the foreground. The service example in deploy/leave-on-time.service assumes
the project is at `/opt/leave-on-time` and runs as an existing `leaveontime` user.
Adjust those paths/user for your machine and ensure that user can write the
output database and its SQLite journal files.

Install the unit into `/etc/systemd/system`, then run:

```sh
sudo systemctl daemon-reload
sudo systemctl enable --now leave-on-time
journalctl -u leave-on-time -f
```

The service restarts the recorder after a crash and starts on boot. Keep route
updates in a separate scheduled job, with only one updater active at a time.
No service is installed or started by editing this repository.
