from utils import get_distance_m, compute_bearing, angle_diff, project_onto_shape
import sqlite3
import time
import json
from passiogo_fix import passiogo
from apscheduler.schedulers.background import BackgroundScheduler
from datetime import datetime

route_conn = sqlite3.connect("routegraph.db", check_same_thread=False)
track_conn = sqlite3.connect("tracking.db", check_same_thread=False)

# no global cursors — every function creates and closes its own

USF_SYSTEM_ID = 2343

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

# ── STARTUP ───────────────────────────────────────────────────────────────────

def load_tracked_systems():
    cursor = track_conn.cursor()
    cursor.execute("SELECT system_id FROM tracked_systems")
    for (system_id,) in cursor.fetchall():
        if system_id not in tracked_systems:
            tracked_systems[system_id] = passiogo.getSystemFromID(system_id)
    cursor.close()

def preload_stop_sequences():
    cursor = track_conn.cursor()
    cursor.execute("SELECT system_id, route_name FROM tracked_routes")
    routes = cursor.fetchall()
    cursor.close()
    for system_id, route_name in routes:
        stop_sequence_cache[(system_id, route_name)] = get_stop_sequence(system_id, route_name)
    print(f"Cached {len(stop_sequence_cache)} route stop sequences")

# ── SYSTEM ────────────────────────────────────────────────────────────────────

def get_system(system_id=USF_SYSTEM_ID):
    if system_id not in tracked_systems:
        system = passiogo.getSystemFromID(system_id)
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
    cursor.close()

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
    
    if time.time() - state['last_moved'] >= 280 and not state['moving'] and not state['stop_logging']:
        state['stop_logging'] = True
        if not state['stop_cleanup_done']:
            state['last_speeds'] = state['last_speeds'][:-56]
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
        tier = 2 #heading is not used anymore aka cant seem to get it.
    elif elapsed < 300:
        tier = 3 
    else:
        tier = 4

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
    print(f"  ✓ Vehicle {vehicle_id} resolved → index {index} (tier {tier} confidence)")

# ── LIVE TRACKING ─────────────────────────────────────────────────────────────

def live_tracking_tick(system_id, vehicle_id, state, v):
    # runs every 5s — shape projection is pure local math, no API calls needed (except bus lat/lon)
    v_lat = float(v.latitude)
    v_lon = float(v.longitude)
    coords = (v_lat, v_lon)

    if not state['coords1']:
        state['coords1'] = coords
        return

    stop_sequence = stop_sequence_cache[(system_id, state['route_name'])]
    distance = get_distance_m(state['coords1'][0], state['coords1'][1], v_lat, v_lon)
    speed = (distance / 5) * 3.6

    if speed > 1:
        state['moving'] = True
        state['last_moved'] = time.time()
    else:
        state['moving'] = False
    
    if time.time() - state['last_moved'] >= 280 and not state['moving'] and not state['stop_logging']:
        state['stop_logging'] = True
        if not state['stop_cleanup_done']:
            state['last_speeds'] = state['last_speeds'][:-56]
            state['stop_cleanup_done'] = True

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
    while loops_checked < len(stop_sequence):
        current_pair = stop_sequence[state['index']]
        shape_points = json.loads(current_pair[5])
        _, progress_pct, _ = project_onto_shape(v_lat, v_lon, shape_points)
        calculated_progress_m = progress_pct * current_pair[6]

        if calculated_progress_m < current_pair[6] - 10:
            break  # bus is still on this segment

        state['index'] = (state['index'] + 1) % len(stop_sequence)
        state['last_update_time'] = time.time()
        loops_checked += 1
    
    if time.time() - state['last_update_time'] > 180 and speed > 5:
        state['status'] = 'UNKNOWN'
        state['cold_start_time'] = time.time()

    print(f"  Vehicle {vehicle_id} — index: {state['index']} | pair: {current_pair[10]} → {current_pair[13]}") 

# ── JOB 1: ROSTER MANAGER (every 90s) ────────────────────────────────────────

def roster_check():
    cursor = track_conn.cursor()
    cursor.execute("SELECT system_id, route_name FROM tracked_routes")
    routes_to_track = cursor.fetchall()

    for system_id, route_name in routes_to_track:
        system = tracked_systems.get(system_id)
        if system is None:
            continue  # cursor stays open, loop continues

        fresh_vehicles = system.getVehicles()
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
                })
                print(f"  ↺ Vehicle {v.id} changed route → UNKNOWN")

        for vehicle_id in existing_ids - fresh_ids:
            del tracked_vehicles[system_id][vehicle_id]
            print(f"  - Vehicle {vehicle_id} disappeared → removed")

    cursor.close()  # closed once at the very end, never inside the loop

# ── JOB 2: GLOBAL TICKER (every 5s) ──────────────────────────────────────────

def global_tick():
    for system_id, vehicles in tracked_vehicles.items():
        system = tracked_systems.get(system_id)
        if system is None:
            continue

        try:
            fresh_vehicles = system.getVehicles()
        except Exception as e:
            print(f"  API error for system {system_id}: {e}")
            continue

        fresh_by_id = {v.id: v for v in fresh_vehicles}

        for vehicle_id, state in list(vehicles.items()):
            v = fresh_by_id.get(vehicle_id)
            if v is None:
                continue

            state['vehicle_obj'] = v

            if state['status'] == 'UNKNOWN':
                cold_start_tick(system_id, vehicle_id, state, v)

            elif state['status'] == 'DETERMINED':
                live_tracking_tick(system_id, vehicle_id, state, v)

# ── SCHEDULER ─────────────────────────────────────────────────────────────────

scheduler = BackgroundScheduler()
scheduler.add_job(roster_check, 'interval', seconds=90, id='roster_check')
scheduler.add_job(global_tick,  'interval', seconds=5,  id='global_tick', max_instances=3)

# ── BOOT ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    setup_db()
    load_tracked_systems()
    preload_stop_sequences()

    boot_cursor = track_conn.cursor()
    boot_cursor.execute("SELECT system_id, route_name FROM tracked_routes")
    active_routes = boot_cursor.fetchall()
    boot_cursor.close()

    if active_routes:
        print(f"Found {len(active_routes)} routes in DB. Activating tracking...")
        for system_id, route_name in active_routes:
            system_obj = get_system(system_id)
            print(f" -> Booting tracking for System: {system_id}, Route: {route_name}")
    else:
        print("Database empty. Seeding default USF Red route...")
        system = get_system(USF_SYSTEM_ID)
        add_tracked_route(system, "Red")
        add_tracked_route(system, "Purple")
        add_tracked_route(system, "Green")

    roster_check()

    scheduler.start()
    print("Scheduler started. Press Ctrl+C to stop.")

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        scheduler.shutdown()
        print("Stopped.")
