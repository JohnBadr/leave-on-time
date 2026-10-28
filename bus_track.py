"""Live tracker facade shared by the collector and ETA engine.

Importing this module has no database or network side effects. Configure paths
explicitly before starting a tracker; the route database is always read-only.
"""

import argparse
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor, wait

from observation_store import ObservationStore
from route_store import open_routes, read_route
from service_calendar import get_period_type
from tracking_core import new_state, process_fix

USF_SYSTEM_ID = 2343
VEHICLE_PRUNE_MISSES = 24
tracked_systems = {}
tracked_vehicles = {}
stop_sequence_cache = {}
# Keys include the route version and ordered destination occurrence.
segment_observations = {}
stop_dwell_observations = {}
route_conn = None
track_conn = None
observation_store = None
state_lock = threading.RLock()
_executor = None
_inflight = {}


def configure(route_db='routegraph.db', observation_db='observations.db', tracking_db='tracking.db', now=None):
    global route_conn, track_conn, observation_store
    close()
    route_conn = open_routes(route_db)
    track_conn = sqlite3.connect(tracking_db, timeout=10, check_same_thread=False)
    observation_store = ObservationStore(observation_db)
    setup_db()
    at = time.time() if now is None else now
    segments, stops = observation_store.load_samples(at)
    segment_observations.clear()
    segment_observations.update(segments)
    stop_dwell_observations.clear()
    stop_dwell_observations.update(stops)
    tracked_systems.clear()
    tracked_vehicles.clear()
    stop_sequence_cache.clear()
    # Import lazily to keep the tracker/estimator module dependency acyclic.
    from eta import clear_caches
    clear_caches()
    return observation_store


def close():
    global route_conn, track_conn, observation_store
    for connection in (route_conn, track_conn):
        if connection is not None:
            connection.close()
    if observation_store is not None:
        observation_store.close()
    route_conn = track_conn = observation_store = None


def _ensure_configured():
    if route_conn is None:
        raise RuntimeError('Call bus_track.configure() with explicit database paths first')


def setup_db():
    _ensure_configured()
    with track_conn:
        track_conn.executescript('''
            CREATE TABLE IF NOT EXISTS tracked_systems(system_id INTEGER PRIMARY KEY,system_name TEXT);
            CREATE TABLE IF NOT EXISTS tracked_routes(system_id INTEGER,route_name TEXT,
                active_users INTEGER DEFAULT 1,PRIMARY KEY(system_id,route_name));
        ''')


def get_system(system_id=USF_SYSTEM_ID):
    _ensure_configured()
    if system_id not in tracked_systems:
        from passiogo_fix import passiogo
        system = passiogo.getSystemFromID(system_id)
        tracked_systems[system_id] = system
        with track_conn:
            track_conn.execute('INSERT OR IGNORE INTO tracked_systems VALUES(?,?)', (system_id, system.name))
    return tracked_systems[system_id]


def load_tracked_systems():
    for (sid,) in track_conn.execute('SELECT system_id FROM tracked_systems').fetchall():
        try:
            get_system(sid)
        except Exception as error:
            print(f'[boot] Cannot load system {sid}: {error}')


def get_routes(system):
    _ensure_configured()
    with state_lock:
        return route_conn.execute('SELECT route_name FROM route_graphs WHERE system_id=? ORDER BY route_name',
                                  (system.id,)).fetchall()


def get_stop_sequence(system_id, route_name):
    _ensure_configured()
    with state_lock:
        sequence = read_route(route_conn, system_id, route_name)
        stop_sequence_cache[(system_id, route_name)] = sequence
        return sequence


def preload_stop_sequences():
    for sid, route in track_conn.execute('SELECT system_id,route_name FROM tracked_routes').fetchall():
        get_stop_sequence(sid, route)


def preload_segment_observations(now=None):
    segments, stops = observation_store.load_samples(time.time() if now is None else now)
    segment_observations.clear()
    segment_observations.update(segments)
    stop_dwell_observations.clear()
    stop_dwell_observations.update(stops)


preload_stop_dwell_observations = preload_segment_observations


def add_tracked_route(system, route_name):
    with state_lock, track_conn:
        track_conn.execute('''INSERT INTO tracked_routes VALUES(?,?,1)
            ON CONFLICT(system_id,route_name) DO UPDATE SET active_users=active_users+1''',
                           (system.id, route_name))
        get_stop_sequence(system.id, route_name)


def remove_tracked_route(system, route_name):
    with state_lock, track_conn:
        track_conn.execute('UPDATE tracked_routes SET active_users=active_users-1 WHERE system_id=? AND route_name=?',
                           (system.id, route_name))
        track_conn.execute('DELETE FROM tracked_routes WHERE active_users<=0')
        active = {row[0] for row in track_conn.execute('SELECT route_name FROM tracked_routes WHERE system_id=?', (system.id,))}
        for vid, state in list(tracked_vehicles.get(system.id, {}).items()):
            if state['route_name'] not in active:
                del tracked_vehicles[system.id][vid]
        if not active:
            tracked_systems.pop(system.id, None)


def fetch_vehicles_for_systems(system_dict):
    global _executor
    if _executor is None:
        _executor = ThreadPoolExecutor(max_workers=10)
    for sid, system in list(system_dict.items()):
        if sid not in _inflight:
            _inflight[sid] = _executor.submit(system.getVehicles)
    if not _inflight:
        return {}
    wait(list(_inflight.values()), timeout=4)
    results = {}
    for sid, future in list(_inflight.items()):
        if not future.done():
            continue  # Don't queue more requests behind an already-hung one.
        del _inflight[sid]
        try:
            vehicles = future.result()
            if vehicles is not None:
                results[sid] = vehicles
        except Exception as error:
            print(f'[fetch] System {sid}: {error}')
    return results


def reconcile_vehicles(system_id, routes, fresh_vehicles, now):
    """Reconcile once per system, so one route cannot delete another's buses."""
    vehicles = tracked_vehicles.setdefault(system_id, {})
    selected = {v.id: v for v in fresh_vehicles if v.routeName in routes}
    for vid, vehicle in selected.items():
        if vid not in vehicles or vehicles[vid]['route_name'] != vehicle.routeName:
            vehicles[vid] = new_state(vehicle, vehicle.routeName, now)
    for vid in list(vehicles):
        if vid not in selected:
            del vehicles[vid]


def roster_check():
    _ensure_configured()
    with state_lock:
        groups = {}
        for sid, route in track_conn.execute('SELECT system_id,route_name FROM tracked_routes').fetchall():
            groups.setdefault(sid, set()).add(route)
            get_stop_sequence(sid, route)
    for sid in groups:
        try:
            get_system(sid)
        except Exception as error:
            print(f'[roster] System {sid}: {error}')
    fresh = fetch_vehicles_for_systems({sid: tracked_systems[sid] for sid in groups if sid in tracked_systems})
    with state_lock:
        for sid, vehicles in fresh.items():
            reconcile_vehicles(sid, groups[sid], vehicles, time.time())


def process_vehicle(system_id, vehicle_id, state, vehicle, sequence, now):
    events = process_fix(state, vehicle, sequence, now)
    for event in events:
        key = (system_id, state['route_name'], state['route_version'], event['segment_index'])
        if event['type'] == 'segment':
            segment_observations.setdefault(key, []).append(
                (event['timestamp'], event['duration_s'], event['baseline_s'], event['ratio']))
        elif event['type'] == 'dwell':
            stop_dwell_observations.setdefault(key, []).append((event['timestamp'], event['duration_s']))
        if observation_store is not None and event['type'] in ('segment', 'dwell'):
            observation_store.sample(system_id, state['route_name'], state['route_version'], vehicle_id, event)
    return events


def cold_start_tick(system_id, vehicle_id, state, vehicle):
    return process_vehicle(system_id, vehicle_id, state, vehicle,
                           stop_sequence_cache[(system_id, state['route_name'])], time.time())


live_tracking_tick = cold_start_tick


def global_tick():
    fresh = fetch_vehicles_for_systems(tracked_systems)
    with state_lock:
        for sid, vehicles in list(tracked_vehicles.items()):
            by_id = {v.id: v for v in fresh.get(sid, [])}
            for vid, state in list(vehicles.items()):
                vehicle = by_id.get(vid)
                if vehicle is None:
                    state['_missing_ticks'] = state.get('_missing_ticks', 0) + 1
                    if state['_missing_ticks'] > VEHICLE_PRUNE_MISSES:
                        del vehicles[vid]
                    continue
                state['_missing_ticks'] = 0
                if vehicle.routeName != state['route_name']:
                    del vehicles[vid]
                    continue
                cold_start_tick(sid, vid, state, vehicle)


def trim_segment_observations(now=None):
    cutoff = (time.time() if now is None else now) - 56 * 86400
    for collection in (segment_observations, stop_dwell_observations):
        for key, observations in list(collection.items()):
            collection[key] = [sample for sample in observations if sample[0] >= cutoff]
            if not collection[key]:
                del collection[key]


def flush_all_observations():
    # Every completed sample is committed immediately, including on crashes.
    if observation_store is not None:
        observation_store.conn.commit()


if __name__ == '__main__':
    from logger import main
    main()
