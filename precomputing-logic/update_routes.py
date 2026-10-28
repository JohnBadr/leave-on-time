"""Incrementally refresh routegraph.db from PassioGo and public OSRM.

Run this periodically from the leave-on-time directory:
    python precomputing-logic/update_routes.py

Only new or changed routes cause OSRM requests.  A route replacement is
committed only after every segment has been fetched successfully, so an OSRM
failure leaves the previous route data intact.
"""

import argparse
import json
import sqlite3
import time
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from math import atan2, cos, degrees, radians, sin, sqrt
from typing import Any

import requests
from passiogo_fix import passiogo


DEFAULT_OSRM_SERVER = "https://router.project-osrm.org"
DEFAULT_DB_PATH = "routegraph.db"
DEFAULT_WORKERS = 2
DEFAULT_TIMEOUT_S = 15
DEFAULT_REQUEST_DELAY_S = 0.2


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS systems (
            system_id INTEGER PRIMARY KEY,
            name TEXT,
            precomputed INTEGER DEFAULT 0,
            last_updated TEXT
        );
        CREATE TABLE IF NOT EXISTS route_graphs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            system_id INTEGER,
            route_name TEXT,
            color TEXT,
            version INTEGER NOT NULL DEFAULT 1,
            UNIQUE(system_id, route_name)
        );
        CREATE TABLE IF NOT EXISTS stops (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            system_id INTEGER,
            stop_id TEXT,
            name TEXT,
            lat REAL,
            lon REAL
        );
        CREATE TABLE IF NOT EXISTS stop_pairs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            route_graph_id INTEGER,
            position INTEGER,
            origin_stop_id TEXT,
            dest_stop_id TEXT,
            road_distance_m REAL,
            road_duration_s REAL,
            bearing REAL,
            is_complex_zone INTEGER DEFAULT 0,
            shape_points TEXT,
            UNIQUE(route_graph_id, position)
        );
        """
    )
    # This migration runs only when the NEW updater is explicitly started.
    # Editing this file does not alter an already-running updater's database.
    with conn:
        conn.execute('BEGIN IMMEDIATE')
        if 'version' not in table_columns(conn, 'route_graphs'):
            conn.execute('ALTER TABLE route_graphs ADD COLUMN version INTEGER NOT NULL DEFAULT 1')
        for name, definition in [('bearing', 'REAL'), ('is_complex_zone', 'INTEGER DEFAULT 0'), ('shape_points', 'TEXT')]:
            if name not in table_columns(conn, 'stop_pairs'):
                conn.execute(f'ALTER TABLE stop_pairs ADD COLUMN {name} {definition}')
        # Ordered occurrences, rather than stop IDs, identify route sections.
        old_unique = any(
            [row[2] for row in conn.execute(f'PRAGMA index_info("{index[1]}")')]
            == ['route_graph_id', 'origin_stop_id', 'dest_stop_id']
            for index in conn.execute('PRAGMA index_list(stop_pairs)').fetchall() if index[2]
        )
        if old_unique:
            conn.execute('''CREATE TABLE stop_pairs_occurrences (
                id INTEGER PRIMARY KEY AUTOINCREMENT, route_graph_id INTEGER,
                position INTEGER, origin_stop_id TEXT, dest_stop_id TEXT,
                road_distance_m REAL, road_duration_s REAL, bearing REAL,
                is_complex_zone INTEGER DEFAULT 0, shape_points TEXT,
                UNIQUE(route_graph_id, position))''')
            columns = 'id,route_graph_id,position,origin_stop_id,dest_stop_id,road_distance_m,road_duration_s,bearing,is_complex_zone,shape_points'
            conn.execute(f'INSERT INTO stop_pairs_occurrences({columns}) SELECT {columns} FROM stop_pairs')
            conn.execute('DROP TABLE stop_pairs')
            conn.execute('ALTER TABLE stop_pairs_occurrences RENAME TO stop_pairs')


def route_stops(route: Any, stops: list[Any]) -> list[Any]:
    route_key = str(route.myid)
    ordered = []
    for stop in stops:
        memberships = {
            str(key): value
            for key, value in getattr(stop, "routesAndPositions", {}).items()
        }
        if route_key in memberships:
            ordered.extend((position, stop) for position in memberships[route_key])
    ordered.sort(key=lambda item: item[0])
    result = [stop for _, stop in ordered]
    if len(result) > 1 and str(result[0].id) == str(result[-1].id):
        result.pop()  # Closure is represented by the final -> first segment.
    return result


def best_routes(system: Any, stops: list[Any]) -> list[Any]:
    selected: dict[str, tuple[Any, int]] = {}
    for route in system.getRoutes():
        count = len(route_stops(route, stops))
        previous = selected.get(route.name)
        if previous is None or count > previous[1]:
            selected[route.name] = (route, count)
    return [route for route, _ in selected.values()]


def haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    radius = 6371000.0
    lat1, lon1, lat2, lon2 = map(radians, (lat1, lon1, lat2, lon2))
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    value = sin(dlat / 2) ** 2 + cos(lat1) * cos(lat2) * sin(dlon / 2) ** 2
    return radius * 2 * atan2(sqrt(value), sqrt(1 - value))


def bearing(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    lat1, lon1, lat2, lon2 = map(radians, (lat1, lon1, lat2, lon2))
    dlon = lon2 - lon1
    x = sin(dlon) * cos(lat2)
    y = cos(lat1) * sin(lat2) - sin(lat1) * cos(lat2) * cos(dlon)
    return (degrees(atan2(x, y)) + 360) % 360


def fetch_segment(
    origin: Any,
    destination: Any,
    server: str,
    timeout_s: float,
    delay_s: float,
) -> tuple[float, float, str]:
    if not all((origin.latitude, origin.longitude, destination.latitude, destination.longitude)):
        raise ValueError(f"missing coordinates for {origin.id} -> {destination.id}")
    if abs(float(origin.latitude)) < 1 or abs(float(origin.longitude)) < 1:
        raise ValueError(f"invalid coordinates for {origin.id}")

    if delay_s:
        time.sleep(delay_s)
    url = (
        f"{server.rstrip('/')}/route/v1/driving/"
        f"{origin.longitude},{origin.latitude};{destination.longitude},{destination.latitude}"
        "?overview=full&geometries=geojson"
    )
    response = requests.get(url, timeout=timeout_s)
    response.raise_for_status()
    data = response.json()
    routes = data.get("routes", [])
    if not routes:
        raise RuntimeError(f"OSRM returned no route for {origin.id} -> {destination.id}")
    selected = routes[0]
    geometry = selected.get("geometry", {}).get("coordinates", [])
    if not geometry:
        raise RuntimeError(f"OSRM returned no shape for {origin.id} -> {destination.id}")
    return (
        float(selected["distance"]),
        float(selected["duration"]),
        json.dumps(geometry, separators=(",", ":")),
    )


def existing_sequence(conn: sqlite3.Connection, route_graph_id: int) -> list[str]:
    rows = conn.execute(
        "SELECT origin_stop_id FROM stop_pairs WHERE route_graph_id = ? ORDER BY position",
        (route_graph_id,),
    ).fetchall()
    return [str(row[0]) for row in rows]


def table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def refresh_route(
    conn: sqlite3.Connection,
    route_graph_id: int,
    ordered_stops: list[Any],
    server: str,
    workers: int,
    timeout_s: float,
    delay_s: float,
    dry_run: bool,
) -> bool:
    segments = [
        (ordered_stops[index], ordered_stops[(index + 1) % len(ordered_stops)])
        for index in range(len(ordered_stops))
    ]
    results: list[tuple[int, str, str, float, float, str]] = []
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {
            executor.submit(
                fetch_segment, origin, destination, server, timeout_s, delay_s
            ): (index, origin, destination)
            for index, (origin, destination) in enumerate(segments)
        }
        for future in as_completed(futures):
            index, origin, destination = futures[future]
            distance, duration, shape = future.result()
            results.append(
                (index, str(origin.id), str(destination.id), distance, duration, shape)
            )
    results.sort()
    if dry_run:
        return False

    previous = conn.execute('''SELECT position,origin_stop_id,dest_stop_id,
        road_distance_m,road_duration_s,shape_points FROM stop_pairs
        WHERE route_graph_id=? ORDER BY position''', (route_graph_id,)).fetchall()
    def canonical(rows):
        return [(row[0], str(row[1]), str(row[2]), round(row[3], 3), round(row[4], 3),
                 json.loads(row[5]) if row[5] else None) for row in rows]
    geometry_changed = canonical(previous) != canonical(results)

    columns = table_columns(conn, "stop_pairs")
    conn.execute("DELETE FROM stop_pairs WHERE route_graph_id = ?", (route_graph_id,))
    for position, origin_id, destination_id, distance, duration, shape in results:
        values: list[Any] = [
            route_graph_id,
            position,
            origin_id,
            destination_id,
            distance,
            duration,
        ]
        insert_columns = [
            "route_graph_id",
            "position",
            "origin_stop_id",
            "dest_stop_id",
            "road_distance_m",
            "road_duration_s",
        ]
        if "shape_points" in columns:
            insert_columns.append("shape_points")
            values.append(shape)
        if "bearing" in columns:
            origin = ordered_stops[position]
            destination = ordered_stops[(position + 1) % len(ordered_stops)]
            insert_columns.append("bearing")
            values.append(
                bearing(
                    float(origin.latitude),
                    float(origin.longitude),
                    float(destination.latitude),
                    float(destination.longitude),
                )
            )
        if "is_complex_zone" in columns:
            origin = ordered_stops[position]
            destination = ordered_stops[(position + 1) % len(ordered_stops)]
            straight_line = haversine(
                float(origin.latitude),
                float(origin.longitude),
                float(destination.latitude),
                float(destination.longitude),
            )
            insert_columns.append("is_complex_zone")
            values.append(int(distance / straight_line > 1.4) if straight_line else 0)
        placeholders = ",".join("?" for _ in values)
        conn.execute(
            f"INSERT INTO stop_pairs ({','.join(insert_columns)}) VALUES ({placeholders})",
            values,
        )
    return geometry_changed


def update_system(
    conn: sqlite3.Connection,
    system: Any,
    server: str,
    workers: int,
    timeout_s: float,
    delay_s: float,
    dry_run: bool,
    force_geometry: bool = False,
) -> tuple[int, int, int]:
    system_id = int(system.id)
    stops = system.getStops()
    routes = best_routes(system, stops)
    old_stops = {
        str(row[0]): (row[1], row[2], row[3])
        for row in conn.execute(
            "SELECT stop_id, lat, lon, name FROM stops WHERE system_id = ?",
            (system_id,),
        )
    }
    changed_stop_ids = {
        str(stop.id)
        for stop in stops
        if str(stop.id) not in old_stops
        or old_stops[str(stop.id)][0] != float(stop.latitude)
        or old_stops[str(stop.id)][1] != float(stop.longitude)
    }

    existing_routes = {
        row[1]: row[0]
        for row in conn.execute(
            "SELECT id, route_name FROM route_graphs WHERE system_id = ?",
            (system_id,),
        )
    }
    current_names = {route.name for route in routes}
    removed = len(set(existing_routes) - current_names)
    affected: list[tuple[Any, int, list[Any]]] = []
    layout_changed = {}
    for route in routes:
        ordered = route_stops(route, stops)
        if len(ordered) < 2:
            continue
        route_graph_id = existing_routes.get(route.name)
        if route_graph_id is None:
            route_graph_id = -1
            current_sequence: list[str] = []
        else:
            current_sequence = existing_sequence(conn, route_graph_id)
        new_sequence = [str(stop.id) for stop in ordered]
        layout_changed[route.name] = current_sequence != new_sequence or any(
            str(stop.id) in changed_stop_ids for stop in ordered
        )
        route_changed = force_geometry or layout_changed[route.name]
        if route_changed:
            affected.append((route, route_graph_id, ordered))

    print(
        f"System {system_id} ({getattr(system, 'name', system_id)}): "
        f"{len(affected)} changed/new route(s), {removed} removed route(s) retained"
    )
    if dry_run:
        return len(affected), removed, len(changed_stop_ids)

    try:
        conn.execute("BEGIN")
        conn.execute(
            "INSERT OR IGNORE INTO systems(system_id, name) VALUES(?, ?)",
            (system_id, getattr(system, "name", str(system_id))),
        )
        conn.execute(
            "UPDATE systems SET name = ?, last_updated = ? WHERE system_id = ?",
            (getattr(system, "name", str(system_id)), datetime.now().isoformat(), system_id),
        )
        for stop in stops:
            stop_id = str(stop.id)
            if stop_id in old_stops:
                conn.execute(
                    """
                    UPDATE stops SET name = ?, lat = ?, lon = ?
                    WHERE system_id = ? AND stop_id = ?
                    """,
                    (stop.name, stop.latitude, stop.longitude, system_id, stop_id),
                )
            else:
                conn.execute(
                    """
                    INSERT INTO stops(system_id, stop_id, name, lat, lon)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (system_id, stop_id, stop.name, stop.latitude, stop.longitude),
                )
        for route in routes:
            conn.execute(
                """
                INSERT OR IGNORE INTO route_graphs(system_id, route_name, color)
                VALUES (?, ?, ?)
                """,
                (system_id, route.name, route.groupColor),
            )
        for route, route_graph_id, ordered in affected:
            is_new = route_graph_id == -1
            if route_graph_id == -1:
                route_graph_id = conn.execute(
                    "SELECT id FROM route_graphs WHERE system_id = ? AND route_name = ?",
                    (system_id, route.name),
                ).fetchone()[0]
            geometry_changed = refresh_route(
                conn,
                route_graph_id,
                ordered,
                server,
                workers,
                timeout_s,
                delay_s,
                False,
            )
            if not is_new and (geometry_changed or layout_changed[route.name]):
                conn.execute('UPDATE route_graphs SET version=version+1 WHERE id=?', (route_graph_id,))
        conn.execute(
            "UPDATE systems SET precomputed = 1 WHERE system_id = ?", (system_id,)
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return len(affected), removed, len(changed_stop_ids)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=DEFAULT_DB_PATH)
    parser.add_argument("--system-id", type=int, help="Only update one PassioGo system")
    parser.add_argument("--osrm-server", default=DEFAULT_OSRM_SERVER)
    parser.add_argument("--workers", type=int, default=DEFAULT_WORKERS)
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_S)
    parser.add_argument("--delay", type=float, default=DEFAULT_REQUEST_DELAY_S)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument('--refresh-geometry', action='store_true',
                        help='Refetch paths even when stops are unchanged; versions change only if the layout changes')
    parser.add_argument('--migrate-only', action='store_true',
                        help='Initialize version/occurrence columns without API requests; run after the old updater finishes')
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.dry_run and args.migrate_only:
        raise SystemExit('--dry-run and --migrate-only cannot be combined')
    if args.workers < 1 or args.delay < 0:
        raise SystemExit("--workers must be positive and --delay cannot be negative")
    if args.dry_run:
        conn = sqlite3.connect(Path(args.db).resolve().as_uri() + '?mode=ro', uri=True, timeout=30)
    else:
        conn = sqlite3.connect(args.db, timeout=30)
        ensure_schema(conn)
    if args.migrate_only:
        conn.close()
        print('Route schema ready. Existing routes start at version 1; no route paths were changed.')
        return
    systems = passiogo.getSystems()
    if args.system_id is not None:
        systems = [system for system in systems if int(system.id) == args.system_id]
        if not systems:
            raise SystemExit(f"PassioGo system {args.system_id} was not found")
    totals = [0, 0, 0]
    try:
        for system in systems:
            values = update_system(
                conn,
                system,
                args.osrm_server,
                args.workers,
                args.timeout,
                args.delay,
                args.dry_run,
                args.refresh_geometry,
            )
            totals = [left + right for left, right in zip(totals, values)]
    finally:
        conn.close()
    print(
        f"Done: {totals[0]} changed/new route(s), "
        f"{totals[1]} removed route(s) retained, {totals[2]} changed stop(s)"
    )


if __name__ == "__main__":
    main()
