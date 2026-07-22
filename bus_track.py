from logger import get_period_type
from utils import get_distance_m, compute_bearing, angle_diff, project_onto_shape
import sqlite3
import time
import json
import queue
from passiogo_fix import passiogo
from apscheduler.schedulers.background import BackgroundScheduler
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, TimeoutError

route_conn = sqlite3.connect("routegraph.db", check_same_thread=False)
track_conn = sqlite3.connect("tracking.db", check_same_thread=False)
obs_conn = sqlite3.connect("observations.db", check_same_thread=False)

# no global cursors — every function creates and closes its own

USF_SYSTEM_ID = 2343
VEHICLE_PRUNE_MISSES = 24

tracked_systems = {
#   system_id: <system_obj>
}

tracked_vehicles = {
#   'system_id': {
#       'vehicle_id': {
#           'route_name': 'Red',
#           'status': 'UNKNOWN',
#           'index': None,
#           'confidence': None,
#           'headings': [],
#           'last_speeds': [],
#           'coords1': (),
#           'vehicle_obj': <v>,
#           'cold_start_time': time.time(),
#           'last_update_time': time.time(),
#           'moving': None,
#           'last_moved': time.time(),
#           'stop_logging': False,
#           'stop_cleanup_done': False,
#           'progress_pct': 0.0,
#           'segment_entry_time': time.time(),
#           'segment_stopped_s': 0.0,
#       }
#   }
}

stop_sequence_cache = {
#   (system_id, route_name): [
#       (position, origin_stop_id, dest_stop_id, bearing, is_complex_zone, shape_points, road_distance_m, road_duration_s, s1_lat, s1_lon, s1_name, s2_lat, s2_lon, s2_name),
#       (position, origin_stop_id, dest_stop_id, bearing, is_complex_zone, shape_points, road_distance_m, road_duration_s, s1_lat, s1_lon, s1_name, s2_lat, s2_lon, s2_name),
#       ...
#   ]
}

segment_observations = {
#   (system_id, route_name, segment_index): [
#       (timestamp, observed_duration_s, osrm_duration_s, ratio),
#       (timestamp, observed_duration_s, osrm_duration_s, ratio),
#       ...
#   ]
}

stop_dwell_observations = {
#   (system_id, route_name, stop_id): [
#       (timestamp, dwell_s),
#       (timestamp, dwell_s),
#       ...
#   ]
}

# ── DB SETUP ──────────────────────────────────────────────────────────────────

def setup_db():
    cursor = track_conn.cursor()
    cursor.executescript("""
        CREATE TABLE IF NOT EXISTS tracked_systems (
            system_id   INTEGER PRIMARY KEY,
            system_name TEXT
        );

        CREATE TABLE IF NOT EXISTS tracked_routes (
            system_id    INTEGER,
            route_name   TEXT,
            active_users INTEGER DEFAULT 1,
            PRIMARY KEY (system_id, route_name),
            FOREIGN KEY (system_id) REFERENCES tracked_systems (system_id)
        );
    """)
    track_conn.commit()
    cursor.close()

    cursor = obs_conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS segment_observations (
            system_id           INTEGER,
            route_name          TEXT,
            segment_index       INTEGER,
            observed_duration_s REAL,
            osrm_duration_s     REAL,
            ratio               REAL,
            timestamp           REAL,
            period_type         TEXT,
            PRIMARY KEY (system_id, route_name, segment_index, timestamp)
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS stop_dwell_observations (
            system_id     INTEGER,
            route_name    TEXT,
            stop_id       TEXT,
            dwell_s       REAL,
            timestamp     REAL,
            period_type   TEXT,
            PRIMARY KEY (system_id, route_name, stop_id, timestamp)
        )
    """)

    obs_conn.commit()
    cursor.close()

# ── STARTUP ───────────────────────────────────────────────────────────────────

def load_tracked_systems():
    cursor = track_conn.cursor()
    cursor.execute("SELECT system_id FROM tracked_systems")
    system_ids = cursor.fetchall()
    cursor.close()
    for (system_id,) in system_ids:
        if system_id not in tracked_systems:
            try:  # ADDED
                tracked_systems[system_id] = passiogo.getSystemFromID(system_id)
            except Exception as e:  # ADDED
                print(f"  [boot] failed to load system {system_id}, skipping this run: {e}")  # ADDED
                continue  # ADDED — one bad system no longer kills the whole boot
    

def preload_stop_sequences():
    cursor = track_conn.cursor()
    cursor.execute("SELECT system_id, route_name FROM tracked_routes")
    routes = cursor.fetchall()
    cursor.close()
    for system_id, route_name in routes:
        stop_sequence_cache[(system_id, route_name)] = get_stop_sequence(system_id, route_name)
    print(f"Cached {len(stop_sequence_cache)} route stop sequences")

def preload_segment_observations():
    today_weekday = datetime.now().weekday()  # 0=Monday, 6=Sunday
    cursor = obs_conn.cursor()
    cursor.execute("""
        SELECT system_id, route_name, segment_index, observed_duration_s, osrm_duration_s, ratio, timestamp
        FROM segment_observations
        WHERE strftime('%w', datetime(timestamp, 'unixepoch')) = ?
        ORDER BY timestamp ASC
    """, (str((today_weekday + 1) % 7),))  # sqlite %w: 0=Sunday, 1=Monday
    
    rows = cursor.fetchall()
    cursor.close()

    seen = set()
    for system_id, route_name, segment_index, observed_duration_s, osrm_duration_s, ratio, timestamp in rows:
        key = (system_id, route_name, segment_index)
        if key in seen:
            continue  # only take the first (earliest) observation per segment
        seen.add(key)
        if key not in segment_observations:
            segment_observations[key] = []
        segment_observations[key].append((timestamp, observed_duration_s, osrm_duration_s, ratio))

    print(f"Preloaded {len(seen)} segment observations from last same weekday")

def preload_stop_dwell_observations():  # ADDED
    today_weekday = datetime.now().weekday()
    cursor = obs_conn.cursor()
    cursor.execute("""
        SELECT system_id, route_name, stop_id, dwell_s, timestamp
        FROM stop_dwell_observations
        WHERE strftime('%w', datetime(timestamp, 'unixepoch')) = ?
        ORDER BY timestamp ASC
    """, (str((today_weekday + 1) % 7),))
    rows = cursor.fetchall()
    cursor.close()

    seen = set()
    for system_id, route_name, stop_id, dwell_s, timestamp in rows:
        key = (system_id, route_name, stop_id)
        if key in seen:
            continue
        seen.add(key)
        stop_dwell_observations.setdefault(key, []).append((timestamp, dwell_s))

    print(f"Preloaded {len(seen)} stop dwell observations from last same weekday")

# ── SYSTEM ────────────────────────────────────────────────────────────────────
fetch_executor = ThreadPoolExecutor(max_workers=10)

def fetch_vehicles_for_systems(system_dict):
    """
    Fetch vehicles across tracked systems in parallel with a strict 4s timeout.
    Prevents API hangups from blocking the main scheduler tick thread.
    """
    results = {}
    if not system_dict:
        return results

    def _fetch(sid, sys_obj):
        try:
            return sid, sys_obj.getVehicles()
        except Exception as e:
            print(f"[fetch] API error for system {sid}: {e}")
            return sid, None

    futures = {fetch_executor.submit(_fetch, sid, obj): sid for sid, obj in system_dict.items()}
    
    for future in futures:
        sid = futures[future]
        try:
            system_id, vehicles = future.result(timeout=4.0)
            if vehicles is not None:
                results[system_id] = vehicles
        except TimeoutError:
            print(f"[fetch] API timeout for system {sid}")
        except Exception as e:
            print(f"[fetch] Unexpected fetch error for system {sid}: {e}")

    return results

def get_system(system_id=USF_SYSTEM_ID):
    if system_id not in tracked_systems:
        try:  # ADDED
            system = passiogo.getSystemFromID(system_id)
        except Exception as e:  # ADDED
            print(f"  [get_system] failed to load system {system_id}: {e}")  # ADDED
            raise  # ADDED — re-raise: caller (e.g. add_tracked_route) needs to know this failed, can't silently return None here since callers assume a valid system object
        tracked_systems[system_id] = system
        cursor = track_conn.cursor()
        cursor.execute("""
            INSERT OR IGNORE INTO tracked_systems (system_id, system_name)
            VALUES (?, ?)
        """, (system_id, system.name))
        track_conn.commit()
        cursor.close()
    return tracked_systems[system_id]

# ── ROUTES ────────────────────────────────────────────────────────────────────

def get_routes(system):
    cursor = route_conn.cursor()
    cursor.execute("""
        SELECT route_name
        FROM route_graphs
        WHERE system_id = ?
        ORDER BY route_name
    """, (system.id,))
    results = cursor.fetchall()
    cursor.close()
    return results

def add_tracked_route(system, route_name):
    cursor = track_conn.cursor()
    cursor.execute("""
        INSERT INTO tracked_routes (system_id, route_name, active_users)
        VALUES (?, ?, 1)
        ON CONFLICT (system_id, route_name)
        DO UPDATE SET active_users = active_users + 1
    """, (system.id, route_name))
    track_conn.commit()
    cursor.close()

    if (system.id, route_name) not in stop_sequence_cache:
        stop_sequence_cache[(system.id, route_name)] = get_stop_sequence(system.id, route_name)

def remove_tracked_route(system, route_name):
    cursor = track_conn.cursor()
    cursor.execute("""
        UPDATE tracked_routes
        SET active_users = active_users - 1
        WHERE system_id = ? AND route_name = ?
    """, (system.id, route_name))
    cursor.execute("""
        DELETE FROM tracked_routes
        WHERE system_id = ? AND route_name = ? AND active_users <= 0
    """, (system.id, route_name))
    track_conn.commit()

    cursor.execute("SELECT COUNT(*) FROM tracked_routes WHERE system_id = ?", (system.id,))
    remaining = cursor.fetchone()[0]  # ADDED
    cursor.close()

    if remaining == 0:  # ADDED
        tracked_systems.pop(system.id, None)  # ADDED — stop polling a system with no active routes

        removed_count = len(tracked_vehicles.get(system.id, {}))  # ADDED
        tracked_vehicles.pop(system.id, None)  # ADDED — drop stale in-memory vehicle states for this system
        stale_keys = [k for k in stop_sequence_cache if k[0] == system.id]  # ADDED
        for k in stale_keys:  # ADDED
            del stop_sequence_cache[k]  # ADDED — no tracked route left means this cached sequence is dead weight too

        cursor = track_conn.cursor()  # ADDED
        cursor.execute("DELETE FROM tracked_systems WHERE system_id = ?", (system.id,))  # ADDED
        track_conn.commit()  # ADDED
        cursor.close()  # ADDED
        print(f"  [route] system {system.id} has no remaining tracked routes — removed from polling, "  # ADDED
              f"dropped {removed_count} vehicle state(s), cleared {len(stale_keys)} cached stop sequence(s)")  # ADDED

def get_stop_sequence(system_id, route_name):
    cursor = route_conn.cursor()
    cursor.execute("""
        SELECT sp.position, sp.origin_stop_id, sp.dest_stop_id,
               sp.bearing, sp.is_complex_zone, sp.shape_points, sp.road_distance_m, sp.road_duration_s,
               s1.lat, s1.lon, s1.name,
               s2.lat, s2.lon, s2.name
        FROM stop_pairs sp
        JOIN route_graphs rg ON sp.route_graph_id = rg.id
        JOIN stops s1 ON sp.origin_stop_id = s1.stop_id AND s1.system_id = rg.system_id
        JOIN stops s2 ON sp.dest_stop_id = s2.stop_id AND s2.system_id = rg.system_id
        WHERE rg.system_id = ? AND rg.route_name = ?
        ORDER BY sp.position
    """, (system_id, route_name))
    results = cursor.fetchall()
    cursor.close()
    return results

# ── COLD START ────────────────────────────────────────────────────────────────

def cold_start_tick(system_id, vehicle_id, state, v):
    v_lat = float(v.latitude)
    v_lon = float(v.longitude)
    coords = (v_lat, v_lon)

    if not state['coords1']:
        state['coords1'] = coords
        return

    distance = get_distance_m(state['coords1'][0], state['coords1'][1], v_lat, v_lon)
    speed = (distance / 5) * 3.6  # m/s → km/h, 5s interval
    if speed > 1:
        state['moving'] = True
        state['last_moved'] = time.time()
    else:
        state['moving'] = False
    
    if time.time() - state['last_moved'] >= 150 and not state['moving'] and not state['stop_logging']:
        state['stop_logging'] = True
        if not state['stop_cleanup_done']:
            if len(state['last_speeds']) > 30:
                state['last_speeds'] = state['last_speeds'][:-30]
            else:
                state['last_speeds'] = []
            state['stop_cleanup_done'] = True


    if state['moving']:
        state['stop_logging'] = False
        state['stop_cleanup_done'] = False  # reset so next stop cleans up too
    
    state['coords1'] = coords

    if state['stop_logging'] == False:
        state['last_speeds'].append(speed)
        if len(state['last_speeds']) > 300:
            state['last_speeds'].pop(0)
        if len(state['last_speeds']) < 3:
            return

    elapsed = time.time() - state['cold_start_time']

    if elapsed < 120:
        tier = 1
    elif elapsed < 180:
        tier = 2 
    elif elapsed < 300:
        tier = 3 
    else:
        tier = 4 #heading is not used anymore aka cant seem to get it.

    v_heading = float(v.calculatedCourse) if v.calculatedCourse else None
    if v_heading is not None:
        state['headings'].append(v_heading)
        if len(state['headings']) > 2:
            state['headings'].pop(0)

    stop_sequence = stop_sequence_cache[(system_id, state['route_name'])]

    # 1. Get stop sequence for this route from DB
    # (position, bearing, shape_points, road_distance_m for each pair)

    # 2. For each stop pair:
    # → project bus onto shape → get error_m
    # → check angle_diff(v_heading, local_bearing) < 45°
    # → if heading matches: record (error_m, position_index) as candidate

    # 3. Pick candidate with lowest error_m → that's the index

    # 4. If no heading available (speed too low):
    # → just pick lowest error_m regardless of heading
    # → mark as lower confidence

    # 5. _resolve_cold_start(system_id, vehicle_id, index, tier)
    best_candidate = None
    best_error_m = 20.0

    if speed < 5:
        v_heading = None

    for sp in stop_sequence:
        shape_points = json.loads(sp[5])
        error_m, _, local_bearing = project_onto_shape(v_lat, v_lon, shape_points)

        if v_heading is not None:
            if angle_diff(v_heading, local_bearing) > 45:
                continue
        elif v_heading is None and elapsed < 300:
            continue

        if error_m >= best_error_m:
            continue

        best_candidate = sp[0]
        best_error_m = error_m

    if best_candidate is None:
        return
    else:
        _resolve_cold_start(system_id, vehicle_id, int(best_candidate), tier)

def _resolve_cold_start(system_id, vehicle_id, index, tier):
    state = tracked_vehicles[system_id][vehicle_id]
    state['index'] = index
    state['status'] = 'DETERMINED'
    state['confidence'] = tier
    state['cold_start_time'] = None
    print(f"  ✓ Vehicle {vehicle_id}, resolved → index {index} (tier {tier} confidence)")

# ── LIVE TRACKING ─────────────────────────────────────────────────────────────

def live_tracking_tick(system_id, vehicle_id, state, v):
    # runs every 5s — shape projection is pure local math, no API calls needed (except bus lat/lon)
    v_lat = float(v.latitude)
    v_lon = float(v.longitude)
    v_name = v.name
    coords = (v_lat, v_lon)

    if not state['coords1']:
        state['coords1'] = coords
        return

    stop_sequence = stop_sequence_cache[(system_id, state['route_name'])]
    current_pair = stop_sequence[state['index']]
    progress_pct = state.get('progress_pct', 0.0)

    dist_to_stop_m = get_distance_m(v_lat, v_lon, current_pair[11], current_pair[12])  # ADDED — s2_lat/s2_lon of current pair, light haversine not shape projection
    if dist_to_stop_m <= 15 and state.get('stop_arrival_time') is None:  # ADDED
        state['stop_arrival_time'] = time.time()  # ADDED — first entry into the zone; GPS jitter in/out won't reset this, only advancement clears it
    
    distance = get_distance_m(state['coords1'][0], state['coords1'][1], v_lat, v_lon)
    speed = (distance / 5) * 3.6

    if speed > 1:
        state['moving'] = True
        state['last_moved'] = time.time()
    else:
        state['moving'] = False
    
    if time.time() - state['last_moved'] >= 150 and not state['moving'] and not state['stop_logging']:
        state['stop_logging'] = True
        state['segment_stopped_s'] = state.get('segment_stopped_s', 0.0) + (time.time() - state['last_moved'])  # ADDED
        if not state['stop_cleanup_done']:
            if len(state['last_speeds']) > 30:
                state['last_speeds'] = state['last_speeds'][:-30]
            else:
                state['last_speeds'] = []
            state['stop_cleanup_done'] = True
    elif state['stop_logging'] and not state['moving']:
        state['segment_stopped_s'] = state.get('segment_stopped_s', 0.0) + 5  # ADDED

    if state['moving']:
        state['stop_logging'] = False
        state['stop_cleanup_done'] = False  # reset so next stop cleans up too
        
    state['coords1'] = coords  # update coords1
    
    if state['stop_logging'] == False:
        state['last_speeds'].append(speed)
        if len(state['last_speeds']) > 300:
            state['last_speeds'].pop(0)
        if len(state['last_speeds']) < 3:
            return

    # This loop keeps advancing the segment index if the bus has overshot it.
    # 'loops_checked' resets to 0 every 5 seconds. Its sole purpose is a safety 
    # circuit breaker: if a bus leaves the route (e.g., goes to the garage), 
    # it prevents an infinite loop from cycling through the route forever 
    # and freezing the script.
    
    loops_checked = 0
    if state['stop_logging'] == False:  
        while loops_checked < len(stop_sequence):
            current_pair = stop_sequence[state['index']]
            shape_points = json.loads(current_pair[5])
            _, progress_pct, _ = project_onto_shape(v_lat, v_lon, shape_points)
            calculated_progress_m = progress_pct * current_pair[6]

            if calculated_progress_m < current_pair[6] - 10:
                break  # bus is still on this segment

            prev_index = state['index']

            observed_duration_s = (time.time() - state['segment_entry_time']) - state.get('segment_stopped_s', 0.0)  # MODIFIED
            osrm_duration_s = stop_sequence[prev_index][7]  # road_duration_s from precomputed DB

            # sanity check: skip observation if either value is suspicious
            if osrm_duration_s and osrm_duration_s > 0 and 10 < observed_duration_s < 3600:
                ratio = max(0.6, min(2.0, observed_duration_s / osrm_duration_s))
                key = (system_id, state['route_name'], prev_index)
                if key not in segment_observations:
                    segment_observations[key] = []
                segment_observations[key].append((time.time(), observed_duration_s, osrm_duration_s, ratio))

            # ── STOP DWELL: record + reset ──  (ADDED)
            if state.get('stop_arrival_time') is not None:  # ADDED
                dwell_s = time.time() - state['stop_arrival_time']  # ADDED
                if 0 <= dwell_s < 200:  # ADDED — sanity cap; excludes long driver-break stalls that also happened to sit near a stop
                    dwell_key = (system_id, state['route_name'], stop_sequence[prev_index][2])  # ADDED — dest_stop_id of segment just completed
                    stop_dwell_observations.setdefault(dwell_key, []).append((time.time(), dwell_s))  # ADDED
                state['stop_arrival_time'] = None  # ADDED — reset for the next stop

            state['index'] = (state['index'] + 1) % len(stop_sequence)
            state['last_update_time'] = time.time()
            loops_checked += 1
            state['segment_entry_time'] = time.time()
            state['segment_stopped_s'] = 0.0  # ADDED — reset break accounting for the new segment
        
    state['progress_pct'] = progress_pct
    current_pair = stop_sequence[state['index']]


    speeds_to_check = state['last_speeds'][:-30] if len(state['last_speeds']) > 30 else state['last_speeds']
    if time.time() - state['last_update_time'] > 900 and speeds_to_check and max(speeds_to_check) < 5:
        state['status'] = 'UNKNOWN'
        state['cold_start_time'] = time.time()

    print(f"  Vehicle {vehicle_id}, name {v_name} — index: {state['index']} | pair: {current_pair[10]} → {current_pair[13]} | progress: {state['progress_pct']:.2%}")

# ── JOB 1: ROSTER MANAGER (every 90s) ────────────────────────────────────────

def roster_check():
    cursor = track_conn.cursor()
    cursor.execute("SELECT system_id, route_name FROM tracked_routes")
    routes_to_track = cursor.fetchall()
    cursor.close()

    needed_system_ids = {system_id for system_id, _ in routes_to_track}

    for sid in needed_system_ids:
        if sid not in tracked_systems:
            try:  # ADDED
                get_system(sid)  # ADDED — reuses existing insert-into-tracked_systems-table logic
                print(f"  [roster] recovered system {sid} on retry")  # ADDED
            except Exception as e:  # ADDED
                print(f"  [roster] system {sid} still unavailable, skipping this cycle: {e}")  # ADDED
                continue  # ADDED — one bad system doesn't block the rest of roster_check

    systems_to_fetch = {
        sid: tracked_systems[sid] for sid in needed_system_ids if sid in tracked_systems
    }
    fresh_by_system = fetch_vehicles_for_systems(systems_to_fetch)

    for system_id, route_name in routes_to_track:
        system = tracked_systems.get(system_id)
        if system is None:
            continue  

        fresh_vehicles = fresh_by_system.get(system_id)  # CHANGED: was fresh_vehicles = system.getVehicles()
        if fresh_vehicles is None:
            print(f"  [roster] no fresh data for system {system_id} this cycle, skipping")  # ADDED
            continue

        fresh_route_vehicles = [v for v in fresh_vehicles if v.routeName == route_name]
        fresh_ids = {v.id for v in fresh_route_vehicles}

        if system_id not in tracked_vehicles:
            tracked_vehicles[system_id] = {}

        existing_ids = set(tracked_vehicles[system_id].keys())

        for v in fresh_route_vehicles:
            if v.id not in existing_ids:
                tracked_vehicles[system_id][v.id] = {
                    'route_name': route_name,
                    'status': 'UNKNOWN',
                    'index': None,
                    'confidence': None,
                    'headings': [],
                    'last_speeds': [],
                    'coords1': (),
                    'vehicle_obj': v,
                    'cold_start_time': time.time(),
                    'last_update_time': time.time(),
                    'moving': None,
                    'last_moved': time.time(),
                    'stop_logging': False,
                    'stop_cleanup_done': False,
                    'progress_pct': 0.0,
                    'segment_entry_time': time.time(),
                    'segment_stopped_s': 0.0,
                }
                print(f"  + New vehicle {v.id} on {route_name} → UNKNOWN")

            elif tracked_vehicles[system_id][v.id]['route_name'] != route_name:
                tracked_vehicles[system_id][v.id].update({
                    'route_name': route_name,
                    'status': 'UNKNOWN',
                    'index': None,
                    'confidence': None,
                    'headings': [],
                    'last_speeds': [],
                    'coords1': (),
                    'vehicle_obj': v,
                    'cold_start_time': time.time(),
                    'last_update_time': time.time(),
                    'moving': None,
                    'last_moved': time.time(),
                    'stop_logging': False,
                    'stop_cleanup_done': False,
                    'progress_pct': 0.0,
                    'segment_entry_time': time.time(),
                    'segment_stopped_s': 0.0,
                })
                print(f"  ↺ Vehicle {v.id} changed route → UNKNOWN")

        for vehicle_id in existing_ids - fresh_ids:
            del tracked_vehicles[system_id][vehicle_id]
            print(f"  - Vehicle {vehicle_id} disappeared → removed")

# ── JOB 2: GLOBAL TICKER (every 5s) ──────────────────────────────────────────

def global_tick():
    # 1. Fetch fresh vehicle updates across all systems with strict timeouts
    fresh_by_system = fetch_vehicles_for_systems(tracked_systems)

    for system_id, vehicles in tracked_vehicles.items():  # ADDED
        if system_id in fresh_by_system:  # ADDED
            continue  # ADDED — handled normally below
        for vehicle_id, state in list(vehicles.items()):  # ADDED
            state['_missing_ticks'] = state.get('_missing_ticks', 0) + 1  # ADDED
            if state['_missing_ticks'] > VEHICLE_PRUNE_MISSES:  # ADDED
                del vehicles[vehicle_id]  # ADDED
                print(f"  [tick] Pruned vehicle {vehicle_id} on system {system_id} "  # ADDED
                      f"(system unreachable for {state['_missing_ticks']} ticks)")  # ADDED

    for system_id, fresh_vehicles in fresh_by_system.items():
        fresh_by_id = {v.id: v for v in fresh_vehicles}
        vehicles = tracked_vehicles.get(system_id, {})

        # 2. Process tracked vehicles & prune stale ones
        for vehicle_id, state in list(vehicles.items()):
            v = fresh_by_id.get(vehicle_id)

            if v is None:
                # Vehicle vanished from API feed
                state['_missing_ticks'] = state.get('_missing_ticks', 0) + 1
                if state['_missing_ticks'] > VEHICLE_PRUNE_MISSES:
                    del vehicles[vehicle_id]
                    print(f"  [tick] Pruned vehicle {vehicle_id} on system {system_id} (missing {state['_missing_ticks']} ticks)")
                continue

            # Vehicle is alive - reset miss counter and update object reference
            state['_missing_ticks'] = 0
            state['vehicle_obj'] = v

            # 3. State machine tracking
            try:
                if state['status'] == 'UNKNOWN':
                    cold_start_tick(system_id, vehicle_id, state, v)
                elif state['status'] == 'DETERMINED':
                    live_tracking_tick(system_id, vehicle_id, state, v)
            except Exception as e:
                print(f"  [tick] Error processing vehicle {vehicle_id} on system {system_id}: {e}")

# ── CLEAN ──────────────────────────────────────────────────────────────────

def trim_segment_observations():
    for key, observations in list(segment_observations.items()):
        if len(observations) > 100:
            overflow = observations[:-100]  # CHANGED — capture what's about to be cut, not just discard it
            _archive_segment_observations(key, overflow)  # ADDED
            segment_observations[key] = observations[-100:]

    for key, observations in list(stop_dwell_observations.items()):
        if len(observations) > 100:
            overflow = observations[:-100]  # ADDED
            _archive_stop_dwell_observations(key, overflow)  # ADDED
            stop_dwell_observations[key] = observations[-100:]

def _archive_segment_observations(key, observations):  # ADDED
    system_id, route_name, segment_index = key
    cursor = obs_conn.cursor()
    for (timestamp, observed_duration_s, osrm_duration_s, ratio) in observations:
        cursor.execute("""
            INSERT OR IGNORE INTO segment_observations
                (system_id, route_name, segment_index, observed_duration_s, osrm_duration_s, ratio, timestamp, period_type)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (system_id, route_name, segment_index, observed_duration_s, osrm_duration_s, ratio, timestamp, get_period_type()))
    obs_conn.commit()
    cursor.close()

def _archive_stop_dwell_observations(key, observations):  # ADDED
    system_id, route_name, stop_id = key
    cursor = obs_conn.cursor()
    for (timestamp, dwell_s) in observations:
        cursor.execute("""
            INSERT OR IGNORE INTO stop_dwell_observations
                (system_id, route_name, stop_id, dwell_s, timestamp, period_type)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (system_id, route_name, stop_id, dwell_s, timestamp, get_period_type()))
    obs_conn.commit()
    cursor.close()

def flush_all_observations():
    cursor = obs_conn.cursor()

    for (system_id, route_name, segment_index), observations in segment_observations.items():
        for (timestamp, observed_duration_s, osrm_duration_s, ratio) in observations:
            cursor.execute("""
                INSERT OR IGNORE INTO segment_observations
                    (system_id, route_name, segment_index, observed_duration_s, osrm_duration_s, ratio, timestamp, period_type)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (system_id, route_name, segment_index, observed_duration_s, osrm_duration_s, ratio, timestamp, get_period_type()))

    for (system_id, route_name, stop_id), observations in stop_dwell_observations.items():
        for (timestamp, dwell_s) in observations:
            cursor.execute("""
                INSERT OR IGNORE INTO stop_dwell_observations
                    (system_id, route_name, stop_id, dwell_s, timestamp, period_type)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (system_id, route_name, stop_id, dwell_s, timestamp, get_period_type()))

    obs_conn.commit()
    cursor.close()
    print(f"  [shutdown] flushed {len(segment_observations)} segment key(s) and {len(stop_dwell_observations)} stop-dwell key(s) to disk")

# ── SCHEDULER ─────────────────────────────────────────────────────────────────

scheduler = BackgroundScheduler()
scheduler.add_job(roster_check, 'interval', seconds=90, id='roster_check')
scheduler.add_job(global_tick, 'interval', seconds=5, id='global_tick', max_instances=1, coalesce=True)
scheduler.add_job(trim_segment_observations, 'interval', minutes=120, id='trim_segment_observations')  # ADDED

# ── BOOT ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    setup_db()
    load_tracked_systems()
    preload_stop_sequences()
    preload_segment_observations()

    boot_cursor = track_conn.cursor()
    boot_cursor.execute("SELECT system_id, route_name FROM tracked_routes")
    active_routes = boot_cursor.fetchall()
    boot_cursor.close()

    if active_routes:
        print(f"Found {len(active_routes)} routes in DB. Activating tracking...")
        for system_id, route_name in active_routes:
            try:  # ADDED
                system_obj = get_system(system_id)
                print(f" -> Booting tracking for System: {system_id}, Route: {route_name}")
            except Exception as e:  # ADDED
                print(f" -> Failed to boot system {system_id} ({route_name}), will retry via roster_check: {e}")  # ADDED
                continue  # ADDED — one bad system no longer aborts the entire boot sequence
    else:
        print("Database empty. Seeding default USF route(s)...")
        system = get_system(USF_SYSTEM_ID)
        add_tracked_route(system, "Red")

    roster_check()

    scheduler.start()
    print("Scheduler started. Press Ctrl+C to stop.")

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        scheduler.shutdown()
        flush_all_observations()
        route_conn.close()
        track_conn.close()
        obs_conn.close()
        print("Stopped.")
