"""Read-only route snapshots. Importing this module never opens a database."""

import hashlib
import json
import sqlite3
from pathlib import Path


class RouteSequence(list):
    def __init__(self, rows=(), version=None, fingerprint=None):
        super().__init__(rows)
        self.fingerprint = fingerprint or hashlib.sha256(
            json.dumps([list(row[:10]) + list(row[11:13]) for row in self],
                       separators=(',', ':'), ensure_ascii=True).encode()
        ).hexdigest()
        # Content identity protects readers while an already-running old
        # updater (without a version column) finishes its work.
        self.version = str(version) if version is not None else 'legacy-' + self.fingerprint


def read_route(conn, system_id, route_name):
    """Read version and geometry together. Readers never migrate the graph."""
    with conn:
        conn.execute('BEGIN')
        columns = {row[1] for row in conn.execute('PRAGMA table_info(route_graphs)')}
        version_column = 'version' if 'version' in columns else 'NULL'
        row = conn.execute(
            f'SELECT id, {version_column} FROM route_graphs WHERE system_id=? AND route_name=?',
            (system_id, route_name),
        ).fetchone()
        if row is None:
            return RouteSequence()
        rows = conn.execute('''
            SELECT sp.position, sp.origin_stop_id, sp.dest_stop_id,
                   sp.bearing, sp.is_complex_zone, sp.shape_points,
                   sp.road_distance_m, sp.road_duration_s,
                   s1.lat, s1.lon, s1.name, s2.lat, s2.lon, s2.name
            FROM stop_pairs sp
            JOIN stops s1 ON s1.stop_id=sp.origin_stop_id AND s1.system_id=?
            JOIN stops s2 ON s2.stop_id=sp.dest_stop_id AND s2.system_id=?
            WHERE sp.route_graph_id=? ORDER BY sp.position
        ''', (system_id, system_id, row[0])).fetchall()
        return RouteSequence(rows, row[1])


def open_routes(path):
    uri = Path(path).resolve().as_uri() + '?mode=ro'
    return sqlite3.connect(uri, uri=True, timeout=5, check_same_thread=False)
