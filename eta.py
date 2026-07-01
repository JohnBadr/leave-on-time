from utils import get_distance_m, compute_bearing, angle_diff, project_onto_shape
import sqlite3
import time
import json
import math
from passiogo_fix import passiogo
from geopy.distance import geodesic
from apscheduler.schedulers.background import BackgroundScheduler
from datetime import datetime
from bus_track import segment_observations


# note for the function below, the user will give us the boarding stop index and the destination stop index but not the pair indexes so we
# are gonna have to convert to pair indexes once when the schedule is initialized and store it and
# then pass in the pair indexes to this function. 
# it should be done this way:
# boarding_pair_index = next(
#     i for i, sp in enumerate(stop_sequence)
#     if sp[2] == boarding_stop_id
# )
#
# dest_pair_index = next(
#     i for i, sp in enumerate(stop_sequence)
#     if sp[2] == dest_stop_id
# )

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


def _compute_vehicle_eta(system_id, state, stop_sequence, dest_idx):

    if not state['coords1']:
        return None

    lat, lon = state['coords1']
    idx = state['index']

    # ── ACTIVE SEGMENT ──
    current_pair = stop_sequence[idx]
    shape_points = json.loads(current_pair[5])
    _, progress_pct, _ = project_onto_shape(lat, lon, shape_points)

    remaining_distance_m  = (1.0 - progress_pct) * current_pair[6]
    remaining_osrm_time_s = (1.0 - progress_pct) * current_pair[7]

    # ratio for active segment
    key = (system_id, state['route_name'], idx)
    observations = segment_observations.get(key, [])
    window = [r for (t, _, _, r) in observations if time.time() - t < 2700]
    if not window:
        window = [r for (t, _, _, r) in observations if time.time() - t < 10800]
    ratio = sum(window) / len(window) if window else _get_fallback_ratio()

    distance_to_dest_m  = remaining_distance_m
    time_to_dest_s      = remaining_osrm_time_s * ratio

    # ── DOWNSTREAM SEGMENTS ──
    highest_idx = len(stop_sequence)
    loops_checked = 0
    while idx != dest_idx and loops_checked < highest_idx:
        idx = (idx + 1) % highest_idx
        loops_checked += 1

        current_pair = stop_sequence[idx]

        key = (system_id, state['route_name'], idx)
        observations = segment_observations.get(key, [])
        window = [r for (t, _, _, r) in observations if time.time() - t < 2700]
        if not window:
            window = [r for (t, _, _, r) in observations if time.time() - t < 10800]
        ratio = sum(window) / len(window) if window else _get_fallback_ratio()

        distance_to_dest_m += current_pair[6]
        time_to_dest_s     += current_pair[7] * ratio

    if loops_checked == highest_idx:
        return None  # dest_idx never found, bus may have left route

    return {
        'eta_timestamp':    time.time() + time_to_dest_s,
        'time_to_dest_s':   time_to_dest_s,
        'distance_to_dest_m': distance_to_dest_m,
    }
        

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