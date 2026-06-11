import sqlite3
import time
import json
import math
from datetime import datetime, date
from passiogo_fix import passiogo
from geopy.distance import geodesic
from apscheduler.schedulers.background import BackgroundScheduler

# ── DB CONNECTIONS ─────────────────────────────────────────────────────────────

log_conn   = sqlite3.connect("bullrunner.db",  check_same_thread=False)
route_conn = sqlite3.connect("routegraph.db",  check_same_thread=False)

# no global cursors — every function creates and closes its own

USF_SYSTEM_ID = 2343

tracked_systems = {
#   system_id: <system_obj>
}

tracked_vehicles = {
#   system_id: {
#       vehicle_id: {
#           'route_name':        'Red',
#           'status':            'UNKNOWN',
#           'index':             None,
#           'confidence':        None,
#           'headings':          [],
#           'last_speeds':       [],
#           'coords1':           (),
#           'vehicle_obj':       <v>,
#           'cold_start_time':   time.time(),
#           'last_update_time':  time.time(),
#           'moving':            None,
#           'last_moved':        time.time(),
#           'stop_logging':      False,
#           'stop_cleanup_done': False,
#           'elapsed_s':         0.0,
#           'last_tick_time':    None,
#       }
#   }
}

# ── DB SETUP ───────────────────────────────────────────────────────────────────

def setup_db():
    cursor = log_conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS vehicle_logs (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp         TEXT,
            tick_time         REAL,
            vehicle_id        TEXT,
            vehicle_name      TEXT,
            route_name        TEXT,
            route_id          TEXT,
            latitude          TEXT,
            longitude         TEXT,
            calculated_course TEXT,
            out_of_service    INTEGER,
            period_type       TEXT,
            stop_pair_index   INTEGER,
            elapsed_s         REAL,
            stop_logging      INTEGER
        )
    """)
    log_conn.commit()
    cursor.close()

# ── CALENDAR ───────────────────────────────────────────────────────────────────

def get_period_type():
    today = date.today()

    # --- HOLIDAYS ---
    holidays = {
        date(2026, 5, 25),   # Memorial Day
        date(2026, 6, 19),   # Juneteenth
        date(2026, 7, 3),    # Independence Day observed
        date(2026, 9, 7),    # Labor Day
        date(2026, 11, 11),  # Veterans Day
        date(2026, 11, 26),  # Thanksgiving
        date(2026, 11, 27),  # Thanksgiving
        date(2026, 12, 25),  # Christmas
        date(2027, 1, 1),    # New Year's Day
        date(2027, 1, 18),   # MLK Day
        date(2027, 5, 31),   # Memorial Day
        date(2027, 6, 19),   # Juneteenth
        date(2027, 7, 5),    # Independence Day observed
        date(2027, 9, 6),    # Labor Day
        date(2027, 11, 11),  # Veterans Day
        date(2027, 11, 25),  # Thanksgiving
        date(2027, 11, 26),  # Thanksgiving
        date(2027, 12, 25),  # Christmas
        date(2028, 1, 1),    # New Year's Day
        date(2028, 1, 17),   # MLK Day
    }
    if today in holidays:
        return "holiday"

    # --- SUMMER 2026 ---
    if date(2026, 5, 18) <= today <= date(2026, 8, 7):
        return "summer"

    # --- FALL 2026 ---
    if date(2026, 8, 24) <= today <= date(2026, 8, 28):
        return "first_week"
    if date(2026, 12, 5) <= today <= date(2026, 12, 10):
        return "finals"
    if date(2026, 8, 24) <= today <= date(2026, 12, 4):
        return "regular"

    # --- WINTER BREAK 2026-2027 ---
    if date(2026, 12, 11) <= today <= date(2027, 1, 10):
        return "break"

    # --- SPRING 2027 ---
    if date(2027, 1, 11) <= today <= date(2027, 1, 17):
        return "first_week"
    if date(2027, 3, 15) <= today <= date(2027, 3, 21):
        return "break"  # Spring break
    if date(2027, 4, 24) <= today <= date(2027, 4, 30):
        return "finals_week_prep"
    if date(2027, 5, 1) <= today <= date(2027, 5, 6):
        return "finals"
    if date(2027, 1, 11) <= today <= date(2027, 4, 30):
        return "regular"

    # --- SUMMER 2027 ---
    if date(2027, 5, 17) <= today <= date(2027, 8, 6):
        return "summer"

    # --- FALL 2027 (estimated) ---
    if date(2027, 8, 23) <= today <= date(2027, 8, 27):
        return "first_week"
    if date(2027, 12, 4) <= today <= date(2027, 12, 10):
        return "finals"
    if date(2027, 8, 23) <= today <= date(2027, 12, 3):
        return "regular"

    # --- WINTER BREAK 2027-2028 ---
    if date(2027, 12, 11) <= today <= date(2028, 1, 9):
        return "break"

    # --- SPRING 2028 (estimated) ---
    if date(2028, 1, 10) <= today <= date(2028, 1, 16):
        return "first_week"
    if date(2028, 3, 10) <= today <= date(2028, 3, 16):
        return "break"
    if date(2028, 4, 28) <= today <= date(2028, 5, 3):
        return "finals"
    if date(2028, 1, 10) <= today <= date(2028, 4, 27):
        return "regular"

    # --- SUMMER 2028 (estimated) ---
    if date(2028, 5, 15) <= today <= date(2028, 8, 7):
        return "summer"

    return "break"

# ── MATH HELPERS ───────────────────────────────────────────────────────────────

def get_distance_m(lat1, lon1, lat2, lon2):
    return geodesic((lat1, lon1), (lat2, lon2)).meters

def angle_diff(a, b):
    diff = abs(a - b) % 360
    return diff if diff <= 180 else 360 - diff

def compute_bearing(lat1, lon1, lat2, lon2):
    lat1, lon1, lat2, lon2 = map(math.radians, [lat1, lon1, lat2, lon2])
    dlon = lon2 - lon1
    x = math.sin(dlon) * math.cos(lat2)
    y = math.cos(lat1) * math.sin(lat2) - math.sin(lat1) * math.cos(lat2) * math.cos(dlon)
    return (math.degrees(math.atan2(x, y)) + 360) % 360

def project_onto_shape(lat, lon, shape_points):
    best_error_m    = float('inf')
    best_segment_idx = 0
    best_t          = 0
    for i in range(len(shape_points) - 1):
        A = shape_points[i]
        B = shape_points[i + 1]
        lon_A, lat_A = A
        lon_B, lat_B = B
        scale = math.cos(math.radians((lat_A + lat_B) / 2))
        dx = (lon_B - lon_A) * scale
        dy = lat_B - lat_A
        vx = (lon - lon_A) * scale
        vy = (lat - lat_A)

        denom = dx * dx + dy * dy
        if denom == 0:
            continue

        t = (vx * dx + vy * dy) / denom
        t = max(0.0, min(1.0, t))
        closest_lat = lat_A + t * (lat_B - lat_A)
        closest_lon = lon_A + t * (lon_B - lon_A)

        error_m = get_distance_m(lat, lon, closest_lat, closest_lon)

        if error_m < best_error_m:
            best_error_m     = error_m
            best_segment_idx = i
            best_t           = t

    progress_pct = (best_segment_idx + best_t) / (len(shape_points) - 1)

    lon_A, lat_A = shape_points[best_segment_idx]
    lon_B, lat_B = shape_points[best_segment_idx + 1]
    local_bearing = compute_bearing(lat_A, lon_A, lat_B, lon_B)

    return best_error_m, progress_pct, local_bearing

# 1. find t          → WHERE on the segment is the closest point
# 2. find the point  → WHAT are its coordinates (using t)
# 3. measure error   → HOW FAR is the bus from that point

# ── ROUTES ─────────────────────────────────────────────────────────────────────

def get_stop_sequence(system_id, route_name):
    cursor = route_conn.cursor()
    cursor.execute("""
        SELECT sp.position, sp.origin_stop_id, sp.dest_stop_id,
               sp.bearing, sp.is_complex_zone, sp.shape_points, sp.road_distance_m,
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

# ── STOP DETECTION ─────────────────────────────────────────────────────────────

def update_stop_detection(state, speed):
    if speed > 1.0:
        state['moving']            = True
        state['last_moved']        = time.time()
        state['stop_logging']      = False
        state['stop_cleanup_done'] = False  # reset so next stop cleans up too
    else:
        state['moving'] = False

    if time.time() - state['last_moved'] >= 280 and not state['moving'] and not state['stop_logging']:
        state['stop_logging'] = True
        if not state['stop_cleanup_done']:
            state['last_speeds']       = state['last_speeds'][:-56]
            state['stop_cleanup_done'] = True

# ── COLD START ─────────────────────────────────────────────────────────────────

def cold_start_tick(system_id, vehicle_id, state, v):
    v_lat  = float(v.latitude)
    v_lon  = float(v.longitude)
    coords = (v_lat, v_lon)

    if not state['coords1']:
        state['coords1'] = coords
        return

    distance = get_distance_m(state['coords1'][0], state['coords1'][1], v_lat, v_lon)
    speed    = (distance / 5) * 3.6  # m/s → km/h, 5s interval

    update_stop_detection(state, speed)

    state['coords1'] = coords

    if not state['stop_logging']:
        state['last_speeds'].append(speed)
        if len(state['last_speeds']) > 300:
            state['last_speeds'].pop(0)
        if len(state['last_speeds']) < 3:
            return

    elapsed = time.time() - state['cold_start_time']

    if elapsed < 120:
        tier = 1
    elif elapsed < 180:
        tier = 2  # heading is not used anymore aka cant seem to get it.
    elif elapsed < 300:
        tier = 3
    else:
        tier = 4

    v_heading = float(v.calculatedCourse) if v.calculatedCourse else None
    if v_heading is not None:
        state['headings'].append(v_heading)
        if len(state['headings']) > 2:
            state['headings'].pop(0)

    stop_sequence = get_stop_sequence(system_id, state['route_name'])

    best_candidate = None
    best_error_m   = 20.0

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
        best_error_m   = error_m

    if best_candidate is None:
        return

    _resolve_cold_start(system_id, vehicle_id, int(best_candidate), tier)

def _resolve_cold_start(system_id, vehicle_id, index, tier):
    state                    = tracked_vehicles[system_id][vehicle_id]
    state['index']           = index
    state['status']          = 'DETERMINED'
    state['confidence']      = tier
    state['cold_start_time'] = None
    print(f"  ✓ Vehicle {vehicle_id} resolved → index {index} (tier {tier} confidence)")

# ── LIVE TRACKING + LOGGING ────────────────────────────────────────────────────

def live_tracking_tick(system_id, vehicle_id, state, v):
    tick_time = time.time()

    v_lat  = float(v.latitude)
    v_lon  = float(v.longitude)
    coords = (v_lat, v_lon)

    if not state['coords1']:
        state['coords1']        = coords
        state['last_tick_time'] = tick_time
        return

    stop_sequence = get_stop_sequence(system_id, state['route_name'])
    distance      = get_distance_m(state['coords1'][0], state['coords1'][1], v_lat, v_lon)
    speed         = (distance / 5) * 3.6

    update_stop_detection(state, speed)

    # elapsed_s: use actual wall-clock delta instead of hardcoded 5s to avoid drift
    tick_delta = tick_time - state['last_tick_time'] if state['last_tick_time'] else 5.0

    if coords == state['coords1']:
        state['elapsed_s'] += tick_delta
    else:
        state['elapsed_s'] = 0.0

    state['coords1']        = coords
    state['last_tick_time'] = tick_time

    if not state['stop_logging']:
        state['last_speeds'].append(speed)
        if len(state['last_speeds']) > 300:
            state['last_speeds'].pop(0)

    # advance index if bus has overshot current segment
    # loops_checked is a circuit breaker: prevents infinite loop if bus leaves route (e.g. garage)
    loops_checked = 0
    while loops_checked < len(stop_sequence):
        current_pair  = stop_sequence[state['index']]
        shape_points  = json.loads(current_pair[5])
        _, progress_pct, _ = project_onto_shape(v_lat, v_lon, shape_points)
        calculated_progress_m = progress_pct * current_pair[6]

        if calculated_progress_m < current_pair[6] - 10:
            break  # bus is still on this segment

        state['index']           = (state['index'] + 1) % len(stop_sequence)
        state['last_update_time'] = time.time()
        loops_checked            += 1

    if time.time() - state['last_update_time'] > 180 and speed > 5:
        state['status']          = 'UNKNOWN'
        state['cold_start_time'] = time.time()
        print(f"  ↺ Vehicle {vehicle_id} stalled → back to UNKNOWN")
        return  # don't log this tick, status is now invalid

    # ── INSERT ROW ──
    cursor = log_conn.cursor()
    cursor.execute("""
        INSERT INTO vehicle_logs
            (timestamp, tick_time, vehicle_id, vehicle_name, route_name, route_id,
             latitude, longitude, calculated_course, out_of_service, period_type,
             stop_pair_index, elapsed_s, stop_logging)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        datetime.now().isoformat(),
        tick_time,
        v.id,
        v.name,
        v.routeName,
        v.routeId,
        v.latitude,
        v.longitude,
        v.calculatedCourse,
        v.outOfService,
        get_period_type(),
        state['index'],
        state['elapsed_s'],
        int(state['stop_logging']),
    ))
    log_conn.commit()
    cursor.close()

    print(f"  Logged {vehicle_id} — index: {state['index']} | pair: {current_pair[9]} → {current_pair[12]} | elapsed: {state['elapsed_s']:.1f}s | stop_logging: {state['stop_logging']}")

# ── JOB 1: ROSTER MANAGER (every 90s) ─────────────────────────────────────────

def get_known_routes(system_id):
    # returns the set of canonical route names we have stop sequences for in routegraph.db
    # used to filter out detour/unknown routes that would leave index permanently stale
    cursor = route_conn.cursor()
    cursor.execute("SELECT route_name FROM route_graphs WHERE system_id = ?", (system_id,))
    names = {row[0] for row in cursor.fetchall()}
    cursor.close()
    return names

def roster_check():
    system = tracked_systems.get(USF_SYSTEM_ID)
    if system is None:
        return

    known_routes   = get_known_routes(USF_SYSTEM_ID)
    fresh_vehicles = system.getVehicles()
    fresh_ids      = {v.id for v in fresh_vehicles if v.routeName in known_routes}

    if USF_SYSTEM_ID not in tracked_vehicles:
        tracked_vehicles[USF_SYSTEM_ID] = {}

    existing_ids = set(tracked_vehicles[USF_SYSTEM_ID].keys())

    for v in fresh_vehicles:
        if v.routeName not in known_routes:
            print(f"  ⚠ Skipping vehicle {v.id} on unknown route '{v.routeName}'")
            continue

        if v.id not in existing_ids:
            tracked_vehicles[USF_SYSTEM_ID][v.id] = {
                'route_name':        v.routeName,
                'status':            'UNKNOWN',
                'index':             None,
                'confidence':        None,
                'headings':          [],
                'last_speeds':       [],
                'coords1':           (),
                'vehicle_obj':       v,
                'cold_start_time':   time.time(),
                'last_update_time':  time.time(),
                'moving':            None,
                'last_moved':        time.time(),
                'stop_logging':      False,
                'stop_cleanup_done': False,
                'elapsed_s':         0.0,
                'last_tick_time':    None,
            }
            print(f"  + New vehicle {v.id} on {v.routeName} → UNKNOWN")

        elif tracked_vehicles[USF_SYSTEM_ID][v.id]['route_name'] != v.routeName:
            tracked_vehicles[USF_SYSTEM_ID][v.id].update({
                'route_name':        v.routeName,
                'status':            'UNKNOWN',
                'index':             None,
                'confidence':        None,
                'headings':          [],
                'last_speeds':       [],
                'coords1':           (),
                'vehicle_obj':       v,
                'cold_start_time':   time.time(),
                'last_update_time':  time.time(),
                'moving':            None,
                'last_moved':        time.time(),
                'stop_logging':      False,
                'stop_cleanup_done': False,
                'elapsed_s':         0.0,
                'last_tick_time':    None,
            })
            print(f"  ↺ Vehicle {v.id} changed route → UNKNOWN")

    for vehicle_id in existing_ids - fresh_ids:
        del tracked_vehicles[USF_SYSTEM_ID][vehicle_id]
        print(f"  - Vehicle {vehicle_id} disappeared → removed")

# ── JOB 2: GLOBAL TICKER (every 5s) ───────────────────────────────────────────

def global_tick():
    system = tracked_systems.get(USF_SYSTEM_ID)
    if system is None:
        return

    try:
        fresh_vehicles = system.getVehicles()
    except Exception as e:
        print(f"  API error: {e}")
        return

    fresh_by_id = {v.id: v for v in fresh_vehicles}
    vehicles    = tracked_vehicles.get(USF_SYSTEM_ID, {})

    for vehicle_id, state in list(vehicles.items()):
        v = fresh_by_id.get(vehicle_id)
        if v is None:
            continue

        state['vehicle_obj'] = v

        if state['status'] == 'UNKNOWN':
            cold_start_tick(USF_SYSTEM_ID, vehicle_id, state, v)

        elif state['status'] == 'DETERMINED':
            live_tracking_tick(USF_SYSTEM_ID, vehicle_id, state, v)

# ── SCHEDULER ──────────────────────────────────────────────────────────────────

scheduler = BackgroundScheduler()
scheduler.add_job(roster_check, 'interval', seconds=90, id='roster_check')
scheduler.add_job(global_tick,  'interval', seconds=5,  id='global_tick')

# ── BOOT ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    setup_db()

    system = passiogo.getSystemFromID(USF_SYSTEM_ID)
    tracked_systems[USF_SYSTEM_ID] = system
    print(f"Loaded system: {system.name}")

    roster_check()

    scheduler.start()
    print("Logger started. Logging every 5s for DETERMINED vehicles. Press Ctrl+C to stop.")

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        scheduler.shutdown()
        log_conn.close()
        route_conn.close()
        print("Stopped.")


# CODE WRITTEN HEAVILY BY CLAUDE AFTER GIVING HIM MY EXISTING CODE AND EXPLAINING HOW TO MAKE THE LOGGER. 
# NOT FULLY REVIEWED NOR TESTED BY ME YET.