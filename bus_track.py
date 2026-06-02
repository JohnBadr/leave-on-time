import sqlite3
import time
from passiogo_fix import passiogo
from geopy.distance import geodesic
from apscheduler.schedulers.background import BackgroundScheduler

route_conn = sqlite3.connect("routegraph.db", check_same_thread=False)
route_cursor = route_conn.cursor()

track_conn = sqlite3.connect("tracking.db", check_same_thread=False)
track_cursor = track_conn.cursor()

USF_SYSTEM_ID = 2343

tracked_systems = {
#   system_id: <system_obj>
}

tracked_vehicles = {
#   system_id: {
#       vehicle_id: {
#           'route_name': 'Red',
#           'status': 'UNKNOWN',
#           'index': None,
#           'confidence': None,
#           'headings': [],
#           'last_speeds': [],
#           'coords1': (),
#           'vehicle_obj': <v>,
#           'tick_count': 0,
#           'cold_start_time': time.time(),
#       }
#   }
}

track_cursor.executescript("""
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

# ── STARTUP ───────────────────────────────────────────────────────────────────

def load_tracked_systems():
    track_cursor.execute("SELECT system_id FROM tracked_systems")
    for (system_id,) in track_cursor.fetchall():
        if system_id not in tracked_systems:
            tracked_systems[system_id] = passiogo.getSystemFromID(system_id)

# ── SYSTEM ────────────────────────────────────────────────────────────────────

def get_system(system_id=USF_SYSTEM_ID):
    if system_id not in tracked_systems:
        system = passiogo.getSystemFromID(system_id)
        tracked_systems[system_id] = system
        track_cursor.execute("""
            INSERT OR IGNORE INTO tracked_systems (system_id, system_name)
            VALUES (?, ?)
        """, (system_id, system.name))
        track_conn.commit()
    return tracked_systems[system_id]

# ── ROUTES ────────────────────────────────────────────────────────────────────

def get_routes(system):
    route_cursor.execute("""
        SELECT route_name
        FROM route_graphs
        WHERE system_id = ?
        ORDER BY route_name
    """, (system.id,))
    return route_cursor.fetchall()

def add_tracked_route(system, route_name):
    track_cursor.execute("""
        INSERT INTO tracked_routes (system_id, route_name, active_users)
        VALUES (?, ?, 1)
        ON CONFLICT (system_id, route_name)
        DO UPDATE SET active_users = active_users + 1
    """, (system.id, route_name))
    track_conn.commit()

def remove_tracked_route(system, route_name):
    track_cursor.execute("""
        UPDATE tracked_routes
        SET active_users = active_users - 1
        WHERE system_id = ? AND route_name = ?
    """, (system.id, route_name))
    track_cursor.execute("""
        DELETE FROM tracked_routes
        WHERE system_id = ? AND route_name = ? AND active_users <= 0
    """, (system.id, route_name))
    track_conn.commit()

def get_stop_sequence(system_id, route_name):
    route_cursor.execute("""
        SELECT sp.position, sp.origin_stop_id, sp.dest_stop_id,
               sp.bearing, sp.is_complex_zone,
               s1.lat, s1.lon, s1.name,
               s2.lat, s2.lon, s2.name
        FROM stop_pairs sp
        JOIN route_graphs rg ON sp.route_graph_id = rg.id
        JOIN stops s1 ON sp.origin_stop_id = s1.stop_id AND s1.system_id = rg.system_id
        JOIN stops s2 ON sp.dest_stop_id = s2.stop_id AND s2.system_id = rg.system_id
        WHERE rg.system_id = ? AND rg.route_name = ?
        ORDER BY sp.position
    """, (system_id, route_name))
    return route_cursor.fetchall()

# ── MATH HELPERS ──────────────────────────────────────────────────────────────

def get_distance_m(lat1, lon1, lat2, lon2):
    return geodesic((lat1, lon1), (lat2, lon2)).meters

def angle_diff(a, b):
    diff = abs(a - b) % 360
    return diff if diff <= 180 else 360 - diff

# ── COLD START ────────────────────────────────────────────────────────────────

def cold_start_tick(system_id, vehicle_id, state, v):
    v_lat = float(v.latitude)
    v_lon = float(v.longitude)
    coords = (v_lat, v_lon)

    if not state['coords1']:
        state['coords1'] = coords
        return

    # compute speed from last coords
    distance = get_distance_m(state['coords1'][0], state['coords1'][1], v_lat, v_lon)
    speed = (distance / 5) * 3.6  # m/s → km/h, 5s interval
    state['coords1'] = coords

    state['last_speeds'].append(speed)
    if len(state['last_speeds']) > 300:
        state['last_speeds'].pop(0)
    if len(state['last_speeds']) < 3:
        return
    avg_speed = sum(state['last_speeds']) / len(state['last_speeds'])

    elapsed = time.time() - state['cold_start_time']

    if elapsed < 120:
        tier = 1
    elif elapsed < 180:
        tier = 2
    elif elapsed < 300:
        tier = 3
    else:
        tier = 4

    v_heading = float(v.calculatedCourse) if v.calculatedCourse else None
    stop_sequence = get_stop_sequence(system_id, state['route_name'])

    # TODO: tier 1 — within 25m + speed < 15 + not complex zone + heading match < 45°
    # TODO: tier 2 — within 25m + not complex zone + heading match < 45°
    # TODO: tier 3 — within 25m + heading match < 45°
    # TODO: tier 4 — closest stop, no conditions
    # when resolved → call _resolve_cold_start(system_id, vehicle_id, index, tier)

def _resolve_cold_start(system_id, vehicle_id, index, tier):
    state = tracked_vehicles[system_id][vehicle_id]
    state['index'] = index
    state['status'] = 'DETERMINED'
    state['confidence'] = tier
    state['cold_start_time'] = None
    state['tick_count'] = 0
    print(f"  ✓ Vehicle {vehicle_id} resolved → index {index} (tier {tier} confidence)")

# ── LIVE TRACKING ─────────────────────────────────────────────────────────────

def live_tracking_tick(system_id, vehicle_id, state, v):
    # only run every 3rd tick → effectively every 15s
    state['tick_count'] = state.get('tick_count', 0) + 1
    if state['tick_count'] % 3 != 0:
        return

    # TODO: rule 1 — standard arrival bubble (< 25m to current target stop → advance +1)
    # TODO: rule 2 — escape / missed trigger (< 100m, was decreasing, now increasing, got within 40m → advance +1)
    # TODO: rule 3 — quantum leap (within 50m of any stop 1-6 ahead → snap forward)
    # TODO: auto reset — no rule fired in > 3min while moving → reset to UNKNOWN

    pass

# ── JOB 1: ROSTER MANAGER (every 90s) ────────────────────────────────────────

def roster_check():
    track_cursor.execute("SELECT system_id, route_name FROM tracked_routes")
    routes_to_track = track_cursor.fetchall()

    for system_id, route_name in routes_to_track:
        system = tracked_systems.get(system_id)
        if system is None:
            continue

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
                    'tick_count': 0,
                    'cold_start_time': time.time(),
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
                    'tick_count': 0,
                    'cold_start_time': time.time(),
                })
                print(f"  ↺ Vehicle {v.id} changed route → UNKNOWN")

        for vehicle_id in existing_ids - fresh_ids:
            del tracked_vehicles[system_id][vehicle_id]
            print(f"  - Vehicle {vehicle_id} disappeared → removed")

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
scheduler.add_job(global_tick,  'interval', seconds=5,  id='global_tick')

# ── BOOT ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    # 1. Repopulate the in-memory tracked_systems dict from the DB
    load_tracked_systems()

    # 2. Query the DB for all routes we are supposed to be actively tracking
    track_cursor.execute("SELECT system_id, route_name FROM tracked_routes")
    active_routes = track_cursor.fetchall()

    if active_routes:
        print(f"Found {len(active_routes)} routes in DB. Activating tracking...")
        for system_id, route_name in active_routes:
            # This ensures the system object is initialized in tracked_systems
            system_obj = get_system(system_id)
            print(f" -> Booting tracking for System: {system_id}, Route: {route_name}")
    else:
        # Fallback: If the DB is completely fresh/empty, seed it with your defaults
        print("Database empty. Seeding default USF Red route...")
        system = get_system(USF_SYSTEM_ID)
        add_tracked_route(system, "Red")

    # 3. Fire the initial roster check sync to populate vehicles immediately
    roster_check()

    # 4. Fire up the clocks
    scheduler.start()
    print("Scheduler started. Press Ctrl+C to stop.")

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        scheduler.shutdown()
        print("Stopped.")