"""Shared, timestamp-aware bus tracker. No network, database, or clock I/O."""

import json
import math
from datetime import datetime

from utils import angle_diff, compute_bearing, get_distance_m, project_onto_shape

MATCH_RADIUS_M = 40.0
ARRIVAL_RADIUS_M = 20.0
DEPARTURE_RADIUS_M = 35.0
MAX_FIX_GAP_S = 120.0
STALE_AFTER_S = 30.0
MAX_SPEED_M_S = 45.0


def source_timestamp(value):
    """Naive provider date strings have unknown timezone: retain, don't guess."""
    if value is None:
        return None
    try:
        timestamp = float(value)
        if timestamp > 1e12:
            timestamp /= 1000
        return timestamp if math.isfinite(timestamp) and timestamp > 1e9 else None
    except (ValueError, TypeError):
        try:
            parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
            return parsed.timestamp() if parsed.tzinfo is not None else None
        except ValueError:
            return None


def out_of_service(vehicle):
    return str(getattr(vehicle, 'outOfService', '')).lower() in ('1', 'true', 'yes')


def observation_timestamp(vehicle):
    # Formatting alone does not establish that "created" means GPS fix time.
    # Production collection opts in only after that provider field is checked.
    if not getattr(vehicle, 'timestamp_verified', False):
        return None
    return source_timestamp(getattr(vehicle, 'created', None))


def new_state(vehicle, route_name, now):
    return {
        'route_name': route_name, 'status': 'UNKNOWN', 'index': None,
        'confidence': None, 'headings': [], 'last_speeds': [], 'coords1': (),
        'vehicle_obj': vehicle, 'cold_start_time': now, 'last_update_time': now,
        'moving': None, 'last_moved': now, 'stop_logging': False,
        'stop_cleanup_done': False, 'progress_pct': 0.0,
        'segment_entry_time': None, 'segment_valid': False,
        'stop_arrival_time': None, 'events': [], 'lap': 0,
        'stop_arrival_valid': False,
        'last_fix_time': None, 'last_received': None, 'data_status': 'unknown',
    }


def _projection(coords, pair):
    return project_onto_shape(*coords, json.loads(pair[5]))


def _reset_match(state, now):
    state.update(status='UNKNOWN', index=None, confidence=None,
                 segment_entry_time=None, segment_valid=False,
                 stop_arrival_time=None, cold_start_time=now,
                 stop_arrival_valid=False,
                 progress_pct=0.0)


def _resolve(state, sequence, coords, heading, now):
    candidates = []
    for index, pair in enumerate(sequence):
        error, progress, bearing = _projection(coords, pair)
        if error > MATCH_RADIUS_M or (heading is not None and angle_diff(heading, bearing) > 65):
            continue
        candidates.append((error, index, progress))
    candidates.sort()
    # Parallel/overlapping paths remain uncertain until later fixes resolve them.
    if not candidates or (len(candidates) > 1 and candidates[1][0] - candidates[0][0] < 5):
        return
    error, index, progress = candidates[0]
    state.update(status='DETERMINED', index=index, confidence=1 if heading is not None else 2,
                 progress_pct=progress, cross_track_m=error,
                 segment_entry_time=None, segment_valid=False, last_update_time=now)


def _arrive(state, sequence, index, at, inferred=False, observed=True):
    if state.get('stop_arrival_time') is not None:
        return
    pair = sequence[index]
    state['stop_arrival_time'] = at
    state['stop_arrival_valid'] = observed
    state['progress_pct'] = 1.0
    state['events'].append({'type': 'arrival', 'segment_index': index,
                            'stop_id': pair[2], 'timestamp': at, 'inferred': inferred,
                            'partial': not observed})
    entry = state.get('segment_entry_time')
    if state.get('segment_valid') and entry is not None and at > entry and pair[7] > 0:
        duration = at - entry
        state['events'].append({'type': 'segment', 'segment_index': index,
                                'timestamp': at, 'duration_s': duration,
                                'baseline_s': pair[7], 'ratio': duration / pair[7]})
    state['segment_valid'] = False


def _depart(state, sequence, index, at):
    arrival = state['stop_arrival_time']
    state['events'].append({'type': 'dwell' if state.get('stop_arrival_valid') else 'dwell_partial', 'segment_index': index,
                            'stop_id': sequence[index][2], 'timestamp': at,
                            'duration_s': max(0.0, at - arrival)})
    next_index = (index + 1) % len(sequence)
    state['events'].append({'type': 'departure', 'segment_index': index,
                            'stop_id': sequence[index][2], 'timestamp': at})
    state.update(index=next_index, segment_entry_time=at, segment_valid=True,
                 stop_arrival_time=None, last_update_time=at)
    state['stop_arrival_valid'] = False
    if next_index == 0:
        state['lap'] = state.get('lap', 0) + 1


def process_fix(state, vehicle, sequence, received_at):
    """Mutate one state, return this fix's events. Raw capture is caller-owned."""
    state['events'] = []
    state['last_received'] = received_at
    state['vehicle_obj'] = vehicle
    if not sequence or out_of_service(vehicle):
        _reset_match(state, received_at)
        state['data_status'] = 'out_of_service' if out_of_service(vehicle) else 'no_route'
        return []
    identity = (str(getattr(sequence, 'version', 'legacy')), getattr(sequence, 'fingerprint', None))
    trip = (str(getattr(vehicle, 'routeId', '')), str(getattr(vehicle, 'tripId', '')))
    if (state.get('route_identity') not in (None, identity)
            or state.get('trip_identity') not in (None, trip)):
        _reset_match(state, received_at)
        state['coords1'] = ()
        state['last_fix_time'] = None
        state.pop('report_signature', None)
    state['route_identity'], state['trip_identity'] = identity, trip
    state['route_version'] = identity[0]
    try:
        coords = float(vehicle.latitude), float(vehicle.longitude)
        if not all(math.isfinite(c) for c in coords) or not (-90 <= coords[0] <= 90 and -180 <= coords[1] <= 180):
            raise ValueError('invalid coordinates')
    except (ValueError, TypeError, AttributeError):
        state['data_status'] = 'invalid_coordinates'
        state['segment_valid'] = False
        return []
    source_at = observation_timestamp(vehicle)
    if source_at is not None and (source_at > received_at + 30 or received_at - source_at > MAX_FIX_GAP_S):
        state['data_status'] = 'stale'
        state['segment_valid'] = False
        return []
    observed_at = received_at if source_at is None else source_at
    signature = (coords, str(getattr(vehicle, 'created', None)), trip)
    previous_at = state.get('last_fix_time')
    if signature == state.get('report_signature') or (previous_at is not None and observed_at <= previous_at):
        state['data_status'] = 'stale' if previous_at is None or received_at - previous_at > STALE_AFTER_S else 'repeated'
        return []
    previous = state.get('coords1')
    delta = observed_at - previous_at if previous_at is not None else None
    distance = get_distance_m(*previous, *coords) if previous else 0.0
    speed = distance / delta if delta and previous else 0.0
    if speed > MAX_SPEED_M_S:
        state['data_status'] = 'gps_jump'
        state['segment_valid'] = False
        return []
    if delta is not None and delta > MAX_FIX_GAP_S:
        _reset_match(state, observed_at)
    heading = compute_bearing(*previous, *coords) if previous and distance > 5 else None
    if heading is None:
        try:
            provider_speed = float(getattr(vehicle, 'speed', 0) or 0)
            course = getattr(vehicle, 'calculatedCourse', None)
            if provider_speed > 5 and course is not None:
                heading = float(course) % 360
        except (ValueError, TypeError):
            pass
    state.update(coords1=coords, last_fix_time=observed_at, source_time=source_at,
                 report_signature=signature, data_status='live' if source_at is not None else 'receive_time',
                 speed_m_s=speed, moving=speed > 0.5)
    if speed > 0.5:
        state['last_moved'] = observed_at
    state['stop_logging'] = observed_at - state.get('last_moved', observed_at) >= 150
    state['last_speeds'] = (state.get('last_speeds', []) + [speed * 3.6])[-300:]
    if state['status'] != 'DETERMINED':
        _resolve(state, sequence, coords, heading, observed_at)
        return state['events']
    index = state['index']
    pair, next_pair = sequence[index], sequence[(index + 1) % len(sequence)]
    error, progress, bearing = _projection(coords, pair)
    next_error, next_progress, next_bearing = _projection(coords, next_pair)
    distance_to_stop = get_distance_m(*coords, pair[11], pair[12])
    previous_progress = state.get('progress_pct', 0.0)
    reach = min((delta or 0) * MAX_SPEED_M_S + 30, max(40.0, distance * 2 + 30))
    forward_distance = (1 - previous_progress) * pair[6] + next_progress * next_pair[6]
    next_heading_ok = heading is None or angle_diff(heading, next_bearing) <= 65
    at_stop = error <= MATCH_RADIUS_M and distance_to_stop <= ARRIVAL_RADIUS_M and progress * pair[6] >= pair[6] - 40
    if at_stop:
        if previous is not None and delta is not None:
            # First observed entry bounds arrival; retain source-time resolution.
            previous_distance = get_distance_m(*previous, pair[11], pair[12])
            _arrive(state, sequence, index, observed_at, observed=previous_distance > ARRIVAL_RADIUS_M)
        state['cross_track_m'] = error
        return state['events']
    next_matches = (next_error <= MATCH_RADIUS_M and next_progress * next_pair[6] >= DEPARTURE_RADIUS_M
                    and distance_to_stop > DEPARTURE_RADIUS_M and next_heading_ok
                    and (next_error + 3 < error or state.get('stop_arrival_time') is not None))
    if next_matches and forward_distance <= reach:
        if state.get('stop_arrival_time') is None:
            # A pass-through between two fresh fixes, not a multi-stop teleport.
            if delta is None or delta > 30 or previous_progress < 0.5:
                _reset_match(state, observed_at)
                _resolve(state, sequence, coords, heading, observed_at)
                return []
            denominator = max(1.0, forward_distance)
            arrival_at = previous_at + delta * ((1 - previous_progress) * pair[6]) / denominator
            _arrive(state, sequence, index, arrival_at, inferred=True)
            departure_at = arrival_at
        else:
            # Departure lies between the last at-stop fix and this moving fix.
            departure_at = max(state['stop_arrival_time'], previous_at or observed_at)
        _depart(state, sequence, index, departure_at)
        state.update(progress_pct=next_progress, cross_track_m=next_error)
        return state['events']
    heading_ok = heading is None or angle_diff(heading, bearing) <= 80
    backwards_m = (previous_progress - progress) * pair[6]
    if error > MATCH_RADIUS_M or not heading_ok or backwards_m > 35:
        _reset_match(state, observed_at)
        _resolve(state, sequence, coords, heading, observed_at)
        return []
    state.update(progress_pct=max(previous_progress, progress), cross_track_m=error)
    return state['events']
