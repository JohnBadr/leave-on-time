# Route graph precomputing

`precompute.py` performs the initial/full route graph build. The migration
scripts add fields to an existing database:

- `migrate.py` adds bearings and complex-zone flags.
- `migrate_shapes.py` adds GeoJSON road shapes.

## Incremental updates

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
```

Requests are deliberately conservative (`2` workers and a short delay between
requests). Adjust `--workers`, `--delay`, and `--timeout` only when needed to
respect the selected OSRM service's usage policy. Routes that disappear from
PassioGo are retained and reported rather than deleted. A route is replaced
only after every segment succeeds, and the database transaction is rolled back
if any request fails.