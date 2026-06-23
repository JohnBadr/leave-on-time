from utils import get_distance_m, compute_bearing, angle_diff, project_onto_shape
import sqlite3
import time
import json
import math
from passiogo_fix import passiogo
from geopy.distance import geodesic
from apscheduler.schedulers.background import BackgroundScheduler

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

def _compute_vehicle_eta(state, stop_sequence, boarding_stop_index, dest_stop_index, walk_to_stop_s, walk_to_dest_s):

    if not state['coords1']:
        return None, None
    lat, lon = state['coords1']
    idx = state['index']
    current_pair = stop_sequence[idx]
    shape_points = json.loads(current_pair[5])
    _, progress_pct, _ = project_onto_shape(lat, lon, shape_points)
    distance_left_in_segment = (1 - progress_pct) * current_pair[6]
    time_left_in_segment = (1 - progress_pct) * current_pair[7]

    time_until_boarding = time_left_in_segment
    distance_until_boarding = distance_left_in_segment
    
    highest_idx = len(stop_sequence)

    while idx != boarding_stop_index:
        idx = (idx + 1) % highest_idx
        current_pair = stop_sequence[idx]
        current_pair_distance = current_pair[6]
        current_pair_time = current_pair[7]
        time_until_boarding += current_pair_time
        distance_until_boarding += current_pair_distance
        
    modified_boarding_stop_index = boarding_stop_index
    ride_distance_m = 0
    ride_time_s = 0

    while modified_boarding_stop_index != dest_stop_index:
        modified_boarding_stop_index = (modified_boarding_stop_index + 1) % highest_idx
        current_pair = stop_sequence[modified_boarding_stop_index]
        current_pair_distance = current_pair[6]
        current_pair_time = current_pair[7]
        ride_distance_m += current_pair_distance
        ride_time_s += current_pair_time
    
    total_distance_m = distance_until_boarding + ride_distance_m
    total_time_s = time_until_boarding + ride_time_s

    if len(state['last_speeds']) >= 100:
        avg_speed_kmh = sum(state['last_speeds']) / len(state['last_speeds'])
        avg_speed_ms = avg_speed_kmh / 3.6
        travel_time_s = total_distance_m / avg_speed_ms
        time_until_boarding_s = distance_until_boarding / avg_speed_ms
    else:
        # fallback: sum road_duration_s for remaining segments
        travel_time_s = total_time_s
        time_until_boarding_s = time_until_boarding
    
    full_journey_s = travel_time_s + walk_to_stop_s + 90 + walk_to_dest_s
    eta = time.time() + full_journey_s
    return eta, time_until_boarding_s

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