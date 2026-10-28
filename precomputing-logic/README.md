# Route graph precomputing

`precompute.py` performs the initial/full route graph build. The migration
scripts add fields to an existing database:

- `migrate.py` adds bearings and complex-zone flags.
- `migrate_shapes.py` adds GeoJSON road shapes.

## Incremental updates

The current updater adds a per-route `version` and preserves repeated stop
occurrences. Let any old updater already running finish first. Back up the
databases while no old tracker/logger is writing, then initialize the schema:

```powershell
python precomputing-logic/update_routes.py --migrate-only
```

This command makes no API requests and does not replace route paths. Existing
routes start at version 1. A route's version changes in the same transaction as
a successful layout replacement; unchanged routes keep their version. Samples
include that version, so reordered stops cannot reinterpret old observations.

Run `update_routes.py` periodically from the `leave-on-time` directory:

```powershell
python precomputing-logic/update_routes.py
```

The updater fetches the current PassioGo system list, discovers new systems,
and compares route stop order and stop coordinates with `routegraph.db`.
Unchanged routes make no OSRM requests. New or changed routes fetch distance,
duration, and GeoJSON shapes from the public OSRM service by default:

```powershell
python precomputing-logic/update_routes.py --dry-run
python precomputing-logic/update_routes.py --system-id 2343
python precomputing-logic/update_routes.py --osrm-server https://router.project-osrm.org
python precomputing-logic/update_routes.py --system-id 2343 --refresh-geometry
```

Requests are deliberately conservative (`2` workers and a short delay between
requests). Adjust `--workers`, `--delay`, and `--timeout` only when needed to
respect the selected OSRM service's usage policy. Routes that disappear from
PassioGo are retained and reported rather than deleted. A route is replaced
only after every segment succeeds, and the database transaction is rolled back
if any request fails.

`--refresh-geometry` checks paths even if stop order/coordinates have not changed.
It increments a version only when the fetched path/timing or layout differs.
The normal weekly update cannot detect a road-only detour from stop data alone.
`--dry-run` uses a read-only connection and does not initialize or migrate tables.
