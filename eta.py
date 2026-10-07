from utils import get_distance_m, compute_bearing, angle_diff, project_onto_shape
import sqlite3
import time
import json
import math
from passiogo_fix import passiogo
from geopy.distance import geodesic
from apscheduler.schedulers.background import BackgroundScheduler
from datetime import datetime
from bus_track import segment_observations, stop_dwell_observations


DEFAULT_STOP_DWELL_S = 3.0

RATIO_HALF_LIFE_S  = 900    # ADDED — weight halves every 15 min; recent observations dominate, old ones fade smoothly instead of a hard cutoff
RATIO_MAX_AGE_S     = 10800  # ADDED — hard ceiling: anything older than 3h is excluded entirely, even at near-zero decayed weight
RATIO_SHRINKAGE_K   = 5      # ADDED — shrinkage constant: with n own-observations, segment trusts itself at weight n/(n+K) and leans on the route-wide ratio for the rest


def _get_fallback_ratio():
    hour = datetime.now().hour
    if 7 <= hour < 9:
        return 1.3
    elif 15 <= hour < 18:
        return 1.25
    elif 9 <= hour < 15:
        return 1.15
    elif 18 <= hour < 22:
        return 1.1
    elif 5 <= hour < 7:
        return 1.05
    else:
        return 1.0

def _get_stop_dwell_s(system_id, route_name, stop_id):
    key = (system_id, route_name, stop_id)
    observations = stop_dwell_observations.get(key, [])
    pairs = [(t, d) for (t, d) in observations]  # ADDED — already (timestamp, value) shaped, just naming it for 
    # clarity going into the shared helper
    
    weighted = _weighted_average(pairs, time.time())  # CHANGED — was flat <3600s-then-<10800s window average
    return weighted if weighted is not None else DEFAULT_STOP_DWELL_S  # CHANGED — was `sum(window)/len(window) 
    # if window else DEFAULT_STOP_DWELL_S`

def _weighted_average(ts_value_pairs, now, half_life_s=RATIO_HALF_LIFE_S, max_age_s=RATIO_MAX_AGE_S):  # ADDED
    # Generic exponential-decay weighted average over (timestamp, value) pairs.
    # Shared by segment-ratio weighting and route-wide weighting below so both
    # use identical decay math.
    weight_sum = 0.0
    weighted_total = 0.0
    for t, v in ts_value_pairs:
        age_s = now - t
        if age_s >= max_age_s:
            continue
        w = 0.5 ** (age_s / half_life_s)
        weight_sum += w
        weighted_total += w * v
    return weighted_total / weight_sum if weight_sum > 0 else None

def _segment_ratio_weighted(observations, now):  # ADDED
    pairs = [(t, r) for (t, _, _, r) in observations]
    return _weighted_average(pairs, now)

def _route_wide_ratio(system_id, route_name, now):  # ADDED
    # Aggregates every segment's observations on this route into one weighted
    # ratio — the "prior" that sparse segments blend toward. Computed once per
    # _compute_vehicle_eta call (not once per segment) to avoid rescanning the
    # whole dict repeatedly in the downstream-segment loop.
    pairs = []
    for (sid, rname, seg_idx), obs in segment_observations.items():
        if sid == system_id and rname == route_name:
            pairs.extend((t, r) for (t, _, _, r) in obs)
    return _weighted_average(pairs, now)

def _blended_segment_ratio(system_id, route_name, segment_index, route_ratio, now):  # ADDED
    key = (system_id, route_name, segment_index)
    observations = segment_observations.get(key, [])
    own_ratio = _segment_ratio_weighted(observations, now)
    n = sum(1 for (t, _, _, _) in observations if now - t < RATIO_MAX_AGE_S)

    if own_ratio is None and route_ratio is None:
        return _get_fallback_ratio()
    if own_ratio is None:
        return route_ratio
    if route_ratio is None:
        return own_ratio

    own_weight = n / (n + RATIO_SHRINKAGE_K)
    return own_weight * own_ratio + (1 - own_weight) * route_ratio


def _compute_vehicle_eta(system_id, state, stop_sequence, dest_idx):

    if not state['coords1']:
        return None

    now = time.time()  # ADDED — single timestamp reused for route-wide + every per-segment weighting call this invocation, keeps all of them consistent with each other

    lat, lon = state['coords1']
    idx = state['index']

    route_ratio = _route_wide_ratio(system_id, state['route_name'], now)  # ADDED — computed once, reused below instead of rescanning per segment

    # ── ACTIVE SEGMENT ──
    current_pair = stop_sequence[idx]
    shape_points = json.loads(current_pair[5])
    _, progress_pct, _ = project_onto_shape(lat, lon, shape_points)

    remaining_distance_m  = (1.0 - progress_pct) * current_pair[6]
    remaining_osrm_time_s = (1.0 - progress_pct) * current_pair[7]

    ratio = _blended_segment_ratio(system_id, state['route_name'], idx, route_ratio, now)  # CHANGED — was flat-window average, now recency-weighted + route-blended

    distance_to_dest_m  = remaining_distance_m
    time_to_dest_s      = remaining_osrm_time_s * ratio

    if idx != dest_idx:
        time_to_dest_s += _get_stop_dwell_s(system_id, state['route_name'], current_pair[2])

    # ── DOWNSTREAM SEGMENTS ──
    highest_idx = len(stop_sequence)
    loops_checked = 0
    while idx != dest_idx and loops_checked < highest_idx:
        idx = (idx + 1) % highest_idx
        loops_checked += 1

        current_pair = stop_sequence[idx]

        ratio = _blended_segment_ratio(system_id, state['route_name'], idx, route_ratio, now)  # CHANGED — same as above

        distance_to_dest_m += current_pair[6]
        time_to_dest_s     += current_pair[7] * ratio

        if idx != dest_idx:
            time_to_dest_s += _get_stop_dwell_s(system_id, state['route_name'], current_pair[2])

    if loops_checked == highest_idx:
        return None  # dest_idx never found, bus may have left route

    return {
        'eta_timestamp':    now + time_to_dest_s,  # CHANGED — reuses `now` from top of function instead of a fresh time.time() call; negligible difference, just consistent
        'time_to_dest_s':   time_to_dest_s,
        'distance_to_dest_m': distance_to_dest_m,
    }


# ── LIVE ETA SMOOTHING ──────────────────────────────────────────────────────── (ADDED — whole section)
# NOT used by shadow_tester.py. shadow_tester calls _compute_vehicle_eta
# directly and must keep doing so — its whole purpose is measuring the raw
# engine's accuracy, and smoothed values would make checkpoint error_s reflect
# smoothing lag instead of true per-tick prediction error. This wrapper is for
# the future live user-facing scheduler only.

_eta_smoothing_state = {
#   (vehicle_id, dest_idx): {'smoothed_time_to_dest_s': float, 'last_updated': float}
}

ETA_SMOOTHING_ALPHA      = 0.3  # lower = smoother but slower to react to real change; higher = jumpier but more current
ETA_SMOOTHING_SNAP_RATIO = 0.5  # if the raw value differs from the smoothed value by more than this fraction, snap instead of blend — avoids lagging behind a real correction (e.g. a vehicle just reacquired after cold-start)

def get_smoothed_eta(vehicle_id, dest_idx, raw_result):
    if raw_result is None:
        return None

    key = (vehicle_id, dest_idx)
    prev = _eta_smoothing_state.get(key)
    raw_time = raw_result['time_to_dest_s']

    if prev is None:
        smoothed_time = raw_time
    else:
        prev_time = prev['smoothed_time_to_dest_s']
        if prev_time > 0 and abs(raw_time - prev_time) / prev_time > ETA_SMOOTHING_SNAP_RATIO:
            smoothed_time = raw_time  # large jump — snap rather than EMA-lag behind a real correction
        else:
            smoothed_time = ETA_SMOOTHING_ALPHA * raw_time + (1 - ETA_SMOOTHING_ALPHA) * prev_time

    _eta_smoothing_state[key] = {
        'smoothed_time_to_dest_s': smoothed_time,
        'last_updated': time.time(),
    }

    return {
        'eta_timestamp':      time.time() + smoothed_time,
        'time_to_dest_s':     smoothed_time,
        'distance_to_dest_m': raw_result['distance_to_dest_m'],  # not smoothed — this is a direct GPS-derived measurement each tick, not a derived prediction like time
    }

def cleanup_smoothing_state(max_age_s=1800):
    # Call periodically from whatever becomes the real scheduler's maintenance
    # pass, same pattern as bus_track.py's trim functions — drops entries for
    # trips no longer being actively tracked so this dict doesn't grow forever.
    now = time.time()
    for key in list(_eta_smoothing_state.keys()):
        if now - _eta_smoothing_state[key]['last_updated'] > max_age_s:
            del _eta_smoothing_state[key]
        

# ── ETA ENGINE LOGIC ───────────────────────────────────────────────────────────
#
# Called by the scheduler every 60s for each active user schedule.
#
# INPUTS:
#   - target_arrival_time: the time the user needs to be at their destination
#   - all active vehicles on the relevant route (from tracked_vehicles)
#   - for each vehicle: state['index'], state['last_speeds'], state['coords1']
#   - walk_home_to_stop: seconds (passed in as param for now)
#   - walk_dest_stop_to_building: seconds (passed in as param for now)
#
# STEP 1 — COMPUTE CURRENT ETA FOR EACH BUS
#   For each vehicle on the route:
#     - Take remaining distance on current segment: (1 - progress_pct) * road_distance_m
#     - Sum full road_distance_m for every segment from (index + 1) to destination stop index
#     - If len(last_speeds) >= 100: use avg_speed from last_speeds buffer to compute travel time
#     - If len(last_speeds) < 100: fall back to sum of road_duration_s from stop_pairs (OSRM estimate)
#     - Add walk_home_to_stop + 90s stop arrival buffer + walk_dest_stop_to_building
#     - Result: current_eta for this vehicle
#
# STEP 2 — COMPUTE 1-LOOP-FORWARD PROJECTION FOR EACH BUS
#   For each vehicle:
#     - Compute full loop distance: sum of all road_distance_m across entire route
#     - Compute loop duration using same avg_speed / road_duration_s fallback logic
#     - projected_eta = current_eta + loop_duration
#
# STEP 3 — CHECK TRIGGER CONDITION
#   If ANY vehicle's projected_eta does NOT exceed target_arrival_time:
#     - Not all buses have exceeded the due date in projection yet
#     - Do nothing, return None, wait for next 60s cycle
#
# STEP 4 — ALL PROJECTIONS EXCEED TARGET, PICK A BUS
#   All projected_etas now exceed target_arrival_time, so we can commit
#   From the CURRENT loop ETAs (not projections):
#     - Filter to vehicles where current_eta <= target_arrival_time (on time)
#     - Pick the vehicle with the LATEST current_eta (closest to deadline without exceeding)
#
# STEP 5 — NO BUS GETS THERE ON TIME (fallback)
#   If no vehicle's current_eta <= target_arrival_time:
#     - Pick the vehicle with the smallest overshoot (closest to target_arrival_time)
#     - Flag it as a late notification
#     - Return it anyway so the scheduler can warn the user
#
# OUTPUT:
#   - selected_vehicle_id
#   - notify_at: timestamp = current_time - (total_journey_time - time_already_elapsed)
#   - estimated_arrival: the current_eta of the selected bus
#   - is_late: bool (True if no bus gets user there on time)
#   - notify_at = time.time() + time_until_boarding_s - walk_to_stop_s - 90 (should be when the user gets notified after 
#    the prefered bus for them gets picked.)