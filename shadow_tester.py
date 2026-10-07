"""
BUILT WITH CLAUDE ALONG WITH MY SPECIFIC INSTRUCTIONS AND HELP/DEBUGGING.
shadow_tester.py

Standalone process. Does NOT touch tracking.db or reuse roster_check/global_tick/
add_tracked_route from bus_track.py — those are wired to the shared production
tracking.db, and calling them here would leak whatever random systems this
process picks (KSU, MIT, AUC, ...) into the production tracker's own route
list. Instead this file owns its own session bookkeeping end to end and only
imports the pieces of bus_track.py that are either pure functions or reference
read-only precomputed data (routegraph.db).

Four objects ARE imported directly (not copied) and must remain the same
objects bus_track.py's own functions close over internally:
  - tracked_vehicles      (_resolve_cold_start indexes into this by reference)
  - stop_sequence_cache   (cold_start_tick / live_tracking_tick index into
                            this directly, not via get_stop_sequence())
  - segment_observations  (so the ETA engine's ratio lookups see real data
                            from this process's own tracking, not an empty dict)
  - stop_dwell_observations (same reasoning — live_tracking_tick appends dwell
                            observations here during the overshoot-advancement
                            loop, and eta.py's dwell lookups need to see this
                            process's own real data too, not an empty dict)

This process's own copies of that data (once it overflows the 100-entry cap,
or on clean shutdown) are archived to observations_shadow_test.db — a separate
file from bus_track.py's real observations.db, so shadow-test data from random
test universities never pollutes the real USF ML archive.

Toggle FORCE_USF_LOCKED below to switch between random-system mode and
USF-only mode.
"""

import sqlite3
import time
import random
import uuid
import threading
import queue
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, TimeoutError

from passiogo_fix import passiogo
from apscheduler.schedulers.background import BackgroundScheduler
from eta import _compute_vehicle_eta, RATIO_MAX_AGE_S  # CHANGED — added RATIO_MAX_AGE_S import, shared constant instead of a second 
# hardcoded copy that can drift out of sync with eta.py's real windowing (this is exactly what happened with the old 2700-vs-3600 mismatch)

from bus_track import (
    tracked_vehicles,
    stop_sequence_cache,
    segment_observations,
    stop_dwell_observations,
    cold_start_tick,
    live_tracking_tick,
    get_stop_sequence,
    get_period_type,
    route_conn,
)
from eta import _compute_vehicle_eta

# ── CONFIG ─────────────────────────────────────────────────────────────────────

FORCE_USF_LOCKED = True   # True = every session is a USF route; False = random system each time
USF_SYSTEM_ID    = 2343
TARGET_POOL_SIZE = 4         # 4 concurrent sessions, per plan

SESSION_MAX_TARGETS = 10     # retire a session after this many completed targets...
SESSION_MAX_AGE_S   = 7200   # ...or this much wall-clock age, whichever comes first
WARMUP_S            = 4200   # cold-start warmup window before a session starts testing
MAINTENANCE_INTERVAL_S = 45  # how often the slower session-management pass runs
VEHICLE_PRUNE_MISSES   = 24  # ~2 min of consecutive misses at 5s cadence before a vehicle is dropped
SESSION_START_RETRIES  = 10  # cap on attempts to find a live system/route when (re)starting a session
TARGET_PICK_RETRIES    = 20  # cap on attempts to find a valid random target index
TARGET_SELECTION_MODE = 'farthest'  # ADDED — 'random' (default, existing behavior) or 'farthest'
                                    # ADDED — 'farthest': target = (reference vehicle's current index + n//2) % n,
                                    # ADDED — the stop diametrically opposite the bus right now, maximizing test distance


CHECKPOINT_ORDER = {'start': 0, 'mid': 1, 'near': 2}  # drives the checkpoint_order column + shadow_checkpoints_sorted view

fetch_executor = ThreadPoolExecutor(max_workers=10)
session_queue = queue.Queue()
shadow_conn = sqlite3.connect("shadow_tests.db", check_same_thread=False)
shadow_obs_conn = sqlite3.connect("observations_shadow_test.db", check_same_thread=False)  # ADDED — separate file, mirrors bus_track.py's observations.db schema but never touches the real one

active_sessions = {
#   session_id: {
#       'session_id', 'system_id', 'system_name', 'route_name', 'system_obj',
#       'stop_sequence', 'created_at', 'warmup_deadline', 'targets_completed',
#       'vehicle_ids', 'current_test',
#   }
}

# ── DB SETUP ───────────────────────────────────────────────────────────────────

def setup_shadow_db():
    cursor = shadow_conn.cursor()
    cursor.executescript("""
        CREATE TABLE IF NOT EXISTS shadow_sessions (
            session_id        TEXT PRIMARY KEY,
            system_id         INTEGER,
            system_name       TEXT,
            route_name        TEXT,
            mode              TEXT,
            created_at        REAL,
            ended_at          REAL,
            targets_completed INTEGER DEFAULT 0,
            status            TEXT
        );

        CREATE TABLE IF NOT EXISTS shadow_tests (
            test_id           TEXT PRIMARY KEY,
            session_id        TEXT,
            system_id         INTEGER,
            system_name       TEXT,
            route_name        TEXT,
            target_pair_index INTEGER,
            target_stop_name  TEXT,
            vehicle_ids       TEXT,
            started_at        REAL,
            ended_at          REAL,
            period_type       TEXT,
            status            TEXT
        );

        CREATE TABLE IF NOT EXISTS shadow_test_vehicle_outcomes (
            test_id            TEXT,
            vehicle_id         TEXT,
            initial_distance_m REAL,
            actual_arrival_ts  REAL,
            status             TEXT,
            PRIMARY KEY (test_id, vehicle_id)
        );

        CREATE TABLE IF NOT EXISTS shadow_checkpoints (
            checkpoint_id             INTEGER PRIMARY KEY AUTOINCREMENT,
            test_id                   TEXT,
            vehicle_id                TEXT,
            checkpoint_label          TEXT,
            checkpoint_order          INTEGER,
            prediction_time           REAL,
            prediction_time_readable  TEXT,
            predicted_eta_ts          REAL,
            predicted_eta_readable    TEXT,
            time_to_dest_s            REAL,
            distance_to_dest_m        REAL,
            ratio_source              TEXT,
            confidence_tier           INTEGER,
            error_s                   REAL,
            abs_error_s               REAL,
            pct_error                 REAL
        );

        CREATE VIEW IF NOT EXISTS shadow_checkpoints_sorted AS
            SELECT * FROM shadow_checkpoints
            ORDER BY vehicle_id, test_id, checkpoint_order;
    """)
    shadow_conn.commit()
    cursor.close()

def setup_shadow_obs_db():  # ADDED
    cursor = shadow_obs_conn.cursor()
    cursor.executescript("""
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
        );

        CREATE TABLE IF NOT EXISTS stop_dwell_observations (
            system_id     INTEGER,
            route_name    TEXT,
            stop_id       TEXT,
            dwell_s       REAL,
            timestamp     REAL,
            period_type   TEXT,
            PRIMARY KEY (system_id, route_name, stop_id, timestamp)
        );
    """)
    shadow_obs_conn.commit()
    cursor.close()

# ── LOGGING HELPERS ────────────────────────────────────────────────────────────

def _format_ts(ts):
    # Human-readable local time for the checkpoint table. prediction_time /
    # predicted_eta_ts stay as REAL unix epoch above for arithmetic in
    # _backfill_checkpoint_errors — these _readable columns exist purely so
    # the raw table is legible when queried directly instead of raw epoch floats.
    if ts is None:
        return None
    return datetime.fromtimestamp(ts).strftime('%Y-%m-%d %H:%M:%S')

def _log_session_start(session):
    cursor = shadow_conn.cursor()
    cursor.execute("""
        INSERT INTO shadow_sessions
            (session_id, system_id, system_name, route_name, mode, created_at, targets_completed, status)
        VALUES (?, ?, ?, ?, ?, ?, 0, 'active')
    """, (session['session_id'], session['system_id'], session['system_name'],
          session['route_name'], session['mode'], session['created_at']))
    shadow_conn.commit()
    cursor.close()

def _log_session_end(session):
    cursor = shadow_conn.cursor()
    cursor.execute("""
        UPDATE shadow_sessions
        SET ended_at = ?, targets_completed = ?, status = 'retired'
        WHERE session_id = ?
    """, (time.time(), session['targets_completed'], session['session_id']))
    shadow_conn.commit()
    cursor.close()

def _log_test_start(session, test):
    cursor = shadow_conn.cursor()
    cursor.execute("""
        INSERT INTO shadow_tests
            (test_id, session_id, system_id, system_name, route_name,
             target_pair_index, target_stop_name, vehicle_ids, started_at, period_type, status)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'in_progress')
    """, (test['test_id'], session['session_id'], session['system_id'], session['system_name'],
          session['route_name'], test['target_pair_index'], test['target_stop_name'],
          ",".join(str(vid) for vid in test['vehicle_ids']), test['started_at'], get_period_type()))
    for vid in test['vehicle_ids']:
        cursor.execute("""
            INSERT OR IGNORE INTO shadow_test_vehicle_outcomes (test_id, vehicle_id, status)
            VALUES (?, ?, 'in_progress')
        """, (test['test_id'], vid))
    shadow_conn.commit()
    cursor.close()

def _log_test_end(test):
    cursor = shadow_conn.cursor()
    all_done = set(test['vehicle_ids']) <= test['vehicles_done']

    cursor.execute("""
        SELECT status FROM shadow_test_vehicle_outcomes WHERE test_id = ?
    """, (test['test_id'],))
    outcome_statuses = [row[0] for row in cursor.fetchall()]
    all_succeeded = all_done and all(s == 'completed' for s in outcome_statuses)

    status = 'completed' if all_succeeded else ('ended_with_timeouts' if all_done else 'in_progress')

    cursor.execute("""
        UPDATE shadow_tests SET ended_at = ?, status = ? WHERE test_id = ?
    """, (time.time(), status, test['test_id']))
    shadow_conn.commit()
    cursor.close()

def _log_vehicle_outcome(test_id, vehicle_id, initial_distance_m, actual_arrival_ts, status):
    cursor = shadow_conn.cursor()
    cursor.execute("""
        UPDATE shadow_test_vehicle_outcomes
        SET initial_distance_m = ?, actual_arrival_ts = ?, status = ?
        WHERE test_id = ? AND vehicle_id = ?
    """, (initial_distance_m, actual_arrival_ts, status, test_id, vehicle_id))
    shadow_conn.commit()
    cursor.close()

def _log_checkpoint(test, vehicle_id, label, eta_result, state):
    ratio_source = _ratio_diagnostics(test['system_id'], test['route_name'], state['index'])
    prediction_time  = time.time()
    predicted_eta_ts = eta_result['eta_timestamp']
    cursor = shadow_conn.cursor()
    cursor.execute("""
        INSERT INTO shadow_checkpoints
            (test_id, vehicle_id, checkpoint_label, checkpoint_order,
             prediction_time, prediction_time_readable,
             predicted_eta_ts, predicted_eta_readable,
             time_to_dest_s, distance_to_dest_m, ratio_source, confidence_tier)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (test['test_id'], vehicle_id, label, CHECKPOINT_ORDER.get(label, 99),
          prediction_time, _format_ts(prediction_time),
          predicted_eta_ts, _format_ts(predicted_eta_ts),
          eta_result['time_to_dest_s'], eta_result['distance_to_dest_m'],
          ratio_source, state.get('confidence')))
    shadow_conn.commit()
    cursor.close()

def _backfill_checkpoint_errors(test_id, vehicle_id, actual_arrival_ts):
    cursor = shadow_conn.cursor()
    cursor.execute("""
        SELECT checkpoint_id, predicted_eta_ts, time_to_dest_s
        FROM shadow_checkpoints
        WHERE test_id = ? AND vehicle_id = ?
    """, (test_id, vehicle_id))
    rows = cursor.fetchall()
    for checkpoint_id, predicted_eta_ts, time_to_dest_s in rows:
        error_s     = actual_arrival_ts - predicted_eta_ts
        abs_error_s = abs(error_s)
        pct_error   = (error_s / time_to_dest_s) if time_to_dest_s and time_to_dest_s > 0 else None
        cursor.execute("""
            UPDATE shadow_checkpoints
            SET error_s = ?, abs_error_s = ?, pct_error = ?
            WHERE checkpoint_id = ?
        """, (error_s, abs_error_s, pct_error, checkpoint_id))
    shadow_conn.commit()
    cursor.close()

def _ratio_diagnostics(system_id, route_name, segment_index):
    # CHANGED — was a binary 'observed'/'fallback' check against only the
    # segment's own observations. eta.py's ratios are now blended (own +
    # route-wide), so three states are meaningful: the segment has its own
    # real data ('own'), it has none but the route has data elsewhere to
    # blend from ('route_blended'), or neither exists and eta.py fell back
    # to the hardcoded time-of-day constant ('fallback'). This still never
    # affects the actual ETA math — purely a diagnostic label on checkpoints.
    now = time.time()

    key = (system_id, route_name, segment_index)
    own_observations = segment_observations.get(key, [])
    own_has_data = any(now - t < RATIO_MAX_AGE_S for (t, _, _, _) in own_observations)  # CHANGED — was two-tier 2700s/10800s window check
    if own_has_data:
        return 'own'  # CHANGED — was 'observed'

    route_has_data = any(  # ADDED
        now - t < RATIO_MAX_AGE_S  # ADDED
        for (sid, rname, seg_idx), obs in segment_observations.items()  # ADDED
        if sid == system_id and rname == route_name  # ADDED
        for (t, _, _, _) in obs  # ADDED
    )
    return 'route_blended' if route_has_data else 'fallback'  # ADDED

# ── OBSERVATION ARCHIVING (shadow-test-only, isolated from bus_track.py's real observations.db) ──

def _archive_segment_observations_shadow(key, observations):  # ADDED
    system_id, route_name, segment_index = key
    cursor = shadow_obs_conn.cursor()
    for (timestamp, observed_duration_s, osrm_duration_s, ratio) in observations:
        cursor.execute("""
            INSERT OR IGNORE INTO segment_observations
                (system_id, route_name, segment_index, observed_duration_s, osrm_duration_s, ratio, timestamp, period_type)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (system_id, route_name, segment_index, observed_duration_s, osrm_duration_s, ratio, timestamp, get_period_type()))
    shadow_obs_conn.commit()
    cursor.close()

def _archive_stop_dwell_observations_shadow(key, observations):  # ADDED
    system_id, route_name, stop_id = key
    cursor = shadow_obs_conn.cursor()
    for (timestamp, dwell_s) in observations:
        cursor.execute("""
            INSERT OR IGNORE INTO stop_dwell_observations
                (system_id, route_name, stop_id, dwell_s, timestamp, period_type)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (system_id, route_name, stop_id, dwell_s, timestamp, get_period_type()))
    shadow_obs_conn.commit()
    cursor.close()

def flush_all_shadow_observations():  # ADDED
    for key, observations in segment_observations.items():
        _archive_segment_observations_shadow(key, observations)
    for key, observations in stop_dwell_observations.items():
        _archive_stop_dwell_observations_shadow(key, observations)
    print(f"  [shutdown] archived {len(segment_observations)} segment key(s) and "
          f"{len(stop_dwell_observations)} stop-dwell key(s) to observations_shadow_test.db")

# ── VEHICLE STATE ──────────────────────────────────────────────────────────────

def _new_vehicle_state(v, route_name, session_id):
    # Mirrors bus_track.py's tracked_vehicles state schema exactly (required —
    # cold_start_tick/live_tracking_tick expect these exact keys), plus
    # shadow-tester-only bookkeeping fields prefixed with underscore.
    return {
        'route_name':         route_name,
        'status':             'UNKNOWN',
        'index':              None,
        'confidence':         None,
        'headings':           [],
        'last_speeds':        [],
        'coords1':            (),
        'vehicle_obj':        v,
        'cold_start_time':    time.time(),
        'last_update_time':   time.time(),
        'moving':             None,
        'last_moved':         time.time(),
        'stop_logging':       False,
        'stop_cleanup_done':  False,
        'progress_pct':       0.0,
        'segment_entry_time': time.time(),
        'segment_stopped_s':  0.0,
        'stop_arrival_time':  None,
        'session_id':         session_id,   # shadow-tester bookkeeping, unused by bus_track functions
        '_missing_ticks':     0,            # shadow-tester bookkeeping, for pruning vanished vehicles
    }

# ── FETCHING (deduped + parallel across systems) ──────────────────────────────

def _fetch_vehicles_for_systems(system_id_to_obj):
    """
    One getVehicles() per distinct system_id, run in parallel across systems.
    Uses a long-lived executor so per-future timeouts actually prevent stalling.
    """
    results = {}
    if not system_id_to_obj:
        return results

    def _fetch(system_id, system_obj):
        try:
            return system_id, system_obj.getVehicles()
        except Exception as e:
            print(f"  [fetch] API error for system {system_id}: {e}")
            return system_id, None

    futures = {fetch_executor.submit(_fetch, sid, obj): sid for sid, obj in system_id_to_obj.items()}
    for future in futures:
        try:
            # 4.0s timeout ensures the 5s tick interval is never breached
            system_id, vehicles = future.result(timeout=4.0)
            if vehicles is not None:
                results[system_id] = vehicles
        except TimeoutError:
            system_id = futures[future]
            print(f"  [fetch] API timeout for system {system_id}")
        except Exception as e:
            system_id = futures[future]
            print(f"  [fetch] Unexpected error for system {system_id}: {e}")

    return results

# ── SESSION LIFECYCLE ──────────────────────────────────────────────────────────

def _pick_random_system_and_route():
    cursor = route_conn.cursor()

    if FORCE_USF_LOCKED:
        candidate_system_ids = [USF_SYSTEM_ID]
    else:
        cursor.execute("SELECT system_id FROM systems WHERE precomputed = 1")
        candidate_system_ids = [row[0] for row in cursor.fetchall()]
        random.shuffle(candidate_system_ids)

    for system_id in candidate_system_ids:
        cursor.execute("SELECT name FROM systems WHERE system_id = ?", (system_id,))
        row = cursor.fetchone()
        if row is None:
            continue
        system_name = row[0]

        cursor.execute("SELECT DISTINCT route_name FROM route_graphs WHERE system_id = ?", (system_id,))
        routes = [r[0] for r in cursor.fetchall()]
        if not routes:
            continue

        cursor.close()
        return system_id, system_name, random.choice(routes)

    cursor.close()
    return None

def _session_builder_loop():
    while True:
        # Build a new session if the active pool + queued pool drops below target
        if len(active_sessions) + session_queue.qsize() < TARGET_POOL_SIZE:
            _build_new_session()
        time.sleep(2)  # brief pause to prevent CPU spinning

def _build_new_session():
    for _ in range(SESSION_START_RETRIES):
        picked = _pick_random_system_and_route()
        if picked is None:
            continue
        system_id, system_name, route_name = picked

        try:
            system_obj = passiogo.getSystemFromID(system_id)
        except Exception as e:
            print(f"  [session] failed to load system {system_id}: {e}")
            continue

        # Fetch directly on this background thread, away from the ticker
        try:
            live_vehicles = system_obj.getVehicles()
        except Exception:
            continue
            
        live_vehicles = [v for v in (live_vehicles or []) if v.routeName == route_name]
        if not live_vehicles:
            continue  # no buses running on this route right now

        stop_sequence = get_stop_sequence(system_id, route_name)
        if not stop_sequence or len(stop_sequence) < 2:
            continue  # not enough precomputed segments

        session_id = str(uuid.uuid4())
        session = {
            'session_id':       session_id,
            'system_id':        system_id,
            'system_name':      system_name,
            'route_name':       route_name,
            'mode':             'usf_locked' if FORCE_USF_LOCKED else 'random',
            'system_obj':       system_obj,
            'stop_sequence':    stop_sequence,
            'created_at':       time.time(),
            'warmup_deadline':  time.time() + WARMUP_S,
            'targets_completed': 0,
            'vehicle_ids':      [],
            'current_test':     None,
        }
        
        # Hand off to the main ticker thread safely
        session_queue.put((session, live_vehicles))
        return

    print("  [session] failed to build a new session after retries — will try again later")

def _retire_session(session_id):
    session = active_sessions.pop(session_id, None)
    if session is None:
        return
    system_id = session['system_id']
    for vid in list(tracked_vehicles.get(system_id, {}).keys()):
        if tracked_vehicles[system_id][vid].get('session_id') == session_id:
            del tracked_vehicles[system_id][vid]
    _log_session_end(session)
    print(f"  [session] retired {session_id[:8]} ({session['targets_completed']} targets completed)")

# ── TARGET SELECTION ───────────────────────────────────────────────────────────

def _pick_new_target(session, vehicle_states):
    stop_sequence = session['stop_sequence']
    n = len(stop_sequence)
    if n < 2:
        return None

    if TARGET_SELECTION_MODE == 'farthest':  # ADDED
        reference_index = vehicle_states[0].get('index')  # ADDED — anchor off the first tracked vehicle; a session's single shared target has to pick one reference when 2 vehicles are tracked
        if reference_index is None:  # ADDED
            return None  # ADDED — reference vehicle hasn't resolved an index yet, try again next cycle
        target = (reference_index + n // 2) % n  # ADDED — stop halfway around the loop from the reference vehicle's current position

        for state in vehicle_states:  # ADDED — still must be valid for every tracked vehicle, not just the reference one
            current_index = state.get('index')  # ADDED
            if current_index is None:  # ADDED
                return None  # ADDED
            if target == current_index or target == (current_index - 1) % n:  # ADDED — same exclusion rule as random mode
                return None  # ADDED — farthest point collides with a vehicle's exclusion zone this cycle (only realistic on short routes); deterministic, so no point retrying now — _advance_session will call again next maintenance pass once positions shift
        return target  # ADDED

    for _ in range(TARGET_PICK_RETRIES):
        target = random.randrange(n)
        valid = True
        for state in vehicle_states:
            current_index = state.get('index')
            if current_index is None:
                valid = False
                break
            # exclude the bus's current segment (near-zero lead time) and the
            # segment one behind it (would already satisfy the arrival
            # condition — index == target+1 — before any prediction is made)
            if target == current_index or target == (current_index - 1) % n:
                valid = False
                break
        if valid:
            return target

    return None

def _new_test(session, vehicle_ids, target_pair_index):
    test_id = str(uuid.uuid4())
    target_stop_name = session['stop_sequence'][target_pair_index][13]  # s2.name — see get_stop_sequence's SELECT order
    test = {
        'test_id':            test_id,
        'session_id':          session['session_id'],
        'system_id':            session['system_id'],
        'route_name':           session['route_name'],
        'target_pair_index':    target_pair_index,
        'target_stop_name':     target_stop_name,
        'vehicle_ids':          list(vehicle_ids),
        'initial_distance_m':   {},
        'checkpoints_hit':      {vid: set() for vid in vehicle_ids},
        'vehicles_done':        set(),
        'started_at':           time.time(),
        'timeout_at':           None,   # set once the first real prediction gives us a duration to multiply
        'hard_cap_at':          time.time() + 2 * SESSION_MAX_AGE_S,  # absolute safety net if timeout_at never gets set
    }
    _log_test_start(session, test)
    return test

# ── PER-TICK TEST PROGRESS ─────────────────────────────────────────────────────

def _check_test_progress(system_id, vehicle_id, state):
    session_id = state.get('session_id')
    session = active_sessions.get(session_id)
    if session is None:
        return
    test = session['current_test']
    if test is None:
        return
    if vehicle_id not in test['vehicle_ids'] or vehicle_id in test['vehicles_done']:
        return

    result = _compute_vehicle_eta(system_id, state, session['stop_sequence'], test['target_pair_index'])
    if result is None:
        return  # bad tick, try again next one

    distance = result['distance_to_dest_m']

    if vehicle_id not in test['initial_distance_m']:
        test['initial_distance_m'][vehicle_id] = distance
        _log_checkpoint(test, vehicle_id, 'start', result, state)
        test['checkpoints_hit'][vehicle_id].add('start')

        proposed_timeout = time.time() + 3 * max(60.0, result['time_to_dest_s'])
        if test['timeout_at'] is None or proposed_timeout > test['timeout_at']:
            test['timeout_at'] = proposed_timeout

    initial_distance = test['initial_distance_m'][vehicle_id]
    hit = test['checkpoints_hit'][vehicle_id]

    if 'mid' not in hit and distance <= initial_distance * 0.5:
        _log_checkpoint(test, vehicle_id, 'mid', result, state)
        hit.add('mid')

    if 'near' not in hit and (distance <= initial_distance * 0.15 or distance <= 300):
        _log_checkpoint(test, vehicle_id, 'near', result, state)
        hit.add('near')

    n = len(session['stop_sequence'])
    if state['index'] == (test['target_pair_index'] + 1) % n:
        _finalize_test_for_vehicle(session, test, vehicle_id, time.time(), timed_out=False)

def _finalize_test_for_vehicle(session, test, vehicle_id, actual_arrival_ts, timed_out):
    test['vehicles_done'].add(vehicle_id)
    initial_distance = test['initial_distance_m'].get(vehicle_id)

    if timed_out:
        _log_vehicle_outcome(test['test_id'], vehicle_id, initial_distance, None, 'incomplete_timeout')
    else:
        _log_vehicle_outcome(test['test_id'], vehicle_id, initial_distance, actual_arrival_ts, 'completed')
        _backfill_checkpoint_errors(test['test_id'], vehicle_id, actual_arrival_ts)

    if set(test['vehicle_ids']) <= test['vehicles_done']:
        _log_test_end(test)
        session['targets_completed'] += 1
        session['current_test'] = None

# ── SESSION ADVANCEMENT (picks vehicles / new targets) ────────────────────────

def _advance_session(session):
    system_id = session['system_id']

    if not session['vehicle_ids']:
        if time.time() < session['warmup_deadline']:
            return  # still warming up
        determined = [
            vid for vid, st in tracked_vehicles.get(system_id, {}).items()
            if st.get('session_id') == session['session_id'] and st['status'] == 'DETERMINED'
        ]
        if not determined:
            return  # nothing resolved yet, try again next maintenance cycle
        session['vehicle_ids'] = determined[:2]
        print(f"  [session {session['session_id'][:8]}] locked in vehicle(s) {session['vehicle_ids']}")

    live_vehicle_ids = [
        vid for vid in session['vehicle_ids']
        if vid in tracked_vehicles.get(system_id, {})
        and tracked_vehicles[system_id][vid]['status'] == 'DETERMINED'
    ]
    if not live_vehicle_ids:
        _retire_session(session['session_id'])  # both/only chosen vehicles gone — simpler to retire than limp on
        return
    session['vehicle_ids'] = live_vehicle_ids

    vehicle_states = [tracked_vehicles[system_id][vid] for vid in live_vehicle_ids]
    target = _pick_new_target(session, vehicle_states)
    if target is None:
        return  # unlucky this cycle, retry next time

    session['current_test'] = _new_test(session, live_vehicle_ids, target)

# ── MAINTENANCE (slow pass: retirement, advancement, timeouts, pool refill) ───

def _trim_observations():
    # Archives overflow to observations_shadow_test.db before discarding it,
    # instead of throwing it away. Same 100-entry-per-key cap, same reasoning
    # as bus_track.py's own trim_segment_observations, applied to this
    # process's isolated shadow-test archive instead of the real one.
    for key, observations in list(segment_observations.items()):  # CHANGED — was list(segment_observations.keys())
        if len(observations) > 100:
            overflow = observations[:-100]  # ADDED
            _archive_segment_observations_shadow(key, overflow)  # ADDED
            segment_observations[key] = observations[-100:]

    for key, observations in list(stop_dwell_observations.items()):  # CHANGED — was list(stop_dwell_observations.keys())
        if len(observations) > 100:
            overflow = observations[:-100]  # ADDED
            _archive_stop_dwell_observations_shadow(key, overflow)  # ADDED
            stop_dwell_observations[key] = observations[-100:]

def _maintenance_tick():
    now = time.time()

    for session_id, session in list(active_sessions.items()):
        age = now - session['created_at']
        if session['targets_completed'] >= SESSION_MAX_TARGETS or age >= SESSION_MAX_AGE_S:
            _retire_session(session_id)
            continue

        test = session['current_test']
        if test is not None:
            effective_timeout = test['timeout_at'] if test['timeout_at'] is not None else test['hard_cap_at']
            if now > effective_timeout:
                for vid in list(test['vehicle_ids']):
                    if vid not in test['vehicles_done']:
                        _finalize_test_for_vehicle(session, test, vid, None, timed_out=True)
            continue  # still mid-test, nothing else to do this cycle

        _advance_session(session)

    _trim_observations()

    # Pull newly built sessions from the background queue and commit them to state
    while not session_queue.empty():
        try:
            session, live_vehicles = session_queue.get_nowait()
            session_id = session['session_id']
            system_id = session['system_id']

            stop_sequence_cache[(system_id, session['route_name'])] = session['stop_sequence']
            active_sessions[session_id] = session

            tracked_vehicles.setdefault(system_id, {})
            for v in live_vehicles:
                if v.id not in tracked_vehicles[system_id]:
                    tracked_vehicles[system_id][v.id] = _new_vehicle_state(v, session['route_name'], session_id)

            _log_session_start(session)
            print(f"  [session] started {session_id[:8]} on {session['system_name']} / {session['route_name']} "
                  f"({len(live_vehicles)} vehicles found, warming up for {WARMUP_S // 60} min)")
        except queue.Empty:
            break

# ── MAIN TICK (5s: fetch, cold-start/track, register newcomers, prune) ───────

_last_maintenance = 0.0

def global_shadow_tick():
    global _last_maintenance

    system_id_to_obj = {s['system_id']: s['system_obj'] for s in active_sessions.values()}
    fresh_by_system = _fetch_vehicles_for_systems(system_id_to_obj)

    for system_id, fresh_vehicles in fresh_by_system.items():
        fresh_by_id = {v.id: v for v in fresh_vehicles}

        # register any vehicle that appeared on a session's route after the
        # session started (e.g. a 2nd bus pulling out ten minutes into warmup)
        for session in active_sessions.values():
            if session['system_id'] != system_id:
                continue
            for v in fresh_vehicles:
                if v.routeName != session['route_name']:
                    continue
                if v.id not in tracked_vehicles.get(system_id, {}):
                    tracked_vehicles.setdefault(system_id, {})[v.id] = \
                        _new_vehicle_state(v, session['route_name'], session['session_id'])

        vehicles = tracked_vehicles.get(system_id, {})
        for vehicle_id, state in list(vehicles.items()):
            v = fresh_by_id.get(vehicle_id)

            if v is None:
                state['_missing_ticks'] = state.get('_missing_ticks', 0) + 1
                if state['_missing_ticks'] > VEHICLE_PRUNE_MISSES:
                    del vehicles[vehicle_id]
                    print(f"  [tick] pruning vehicle {vehicle_id} (missing {state['_missing_ticks']} ticks)")
                continue
            state['_missing_ticks'] = 0
            state['vehicle_obj'] = v

            try:
                if state['status'] == 'UNKNOWN':
                    cold_start_tick(system_id, vehicle_id, state, v)
                elif state['status'] == 'DETERMINED':
                    live_tracking_tick(system_id, vehicle_id, state, v)
                    _check_test_progress(system_id, vehicle_id, state)
            except Exception as e:
                print(f"  [tick] error processing vehicle {vehicle_id} on system {system_id}: {e}")

    if time.time() - _last_maintenance > MAINTENANCE_INTERVAL_S:
        try:
            _maintenance_tick()
        except Exception as e:
            print(f"  [maintenance] error: {e}")
        _last_maintenance = time.time()

# ── BOOT ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    setup_shadow_db()
    setup_shadow_obs_db()  # ADDED

    print(f"Starting shadow tester — mode: {'USF-locked' if FORCE_USF_LOCKED else 'random'}, "
          f"pool size: {TARGET_POOL_SIZE}")

    # Kick off the background builder thread
    threading.Thread(target=_session_builder_loop, daemon=True).start()

    scheduler = BackgroundScheduler()
    scheduler.add_job(global_shadow_tick, 'interval', seconds=5, id='global_shadow_tick', max_instances=1, coalesce=True)
    scheduler.start()
    print("Shadow tester running. Press Ctrl+C to stop.")

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        scheduler.shutdown()
        flush_all_shadow_observations()  # ADDED — persist whatever's left in memory before exit
        shadow_conn.close()
        shadow_obs_conn.close()  # ADDED
        print("Shadow tester stopped.")