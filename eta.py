"""USF history + live fleet corrections. All forecasts accept a replay clock."""

import math
import time
from bisect import bisect_left, bisect_right
from collections import OrderedDict

from bus_track import segment_observations, stop_dwell_observations
from observation_store import MODEL_VERSION
from service_calendar import get_period_type, local_datetime
from tracking_core import MAX_FIX_GAP_S, STALE_AFTER_S, out_of_service

DEFAULT_STOP_DWELL_S = 10.0
RATIO_HALF_LIFE_S = 900
RATIO_MAX_AGE_S = 10800
RATIO_SHRINKAGE_K = 5
HISTORY_MAX_AGE_S = 56 * 86400
_calibration_provider = None
_pair_cache = OrderedDict()
_bin_cache = OrderedDict()


def clear_caches():
    _pair_cache.clear()
    _bin_cache.clear()


def _cached(cache, key, build):
    if key not in cache:
        cache[key] = build()
        if len(cache) > 512:
            cache.popitem(last=False)
    cache.move_to_end(key)
    return cache[key]


def _sample_token(key, samples):
    return key, id(samples), len(samples), tuple(samples[-1]) if samples else ()


def _recent(pairs, now):
    # Cached pairs are sorted, so weeks of old history are not scanned per tick.
    start = bisect_left(pairs, (now - RATIO_MAX_AGE_S, float('-inf')))
    end = bisect_right(pairs, (now, float('inf')))
    return pairs[start:end]


def set_calibration_provider(provider):
    global _calibration_provider
    _calibration_provider = provider


def _get_fallback_ratio(at=None):
    hour = local_datetime(at).hour
    if 7 <= hour < 9:
        return 1.3
    if 15 <= hour < 18:
        return 1.25
    if 9 <= hour < 15:
        return 1.15
    if 18 <= hour < 22:
        return 1.1
    return 1.05 if 5 <= hour < 7 else 1.0


def _summary(pairs):
    total = sum(weight for weight, value in pairs)
    if not total:
        return None, 0.0
    mean = sum(weight * value for weight, value in pairs) / total
    effective_n = total * total / sum(weight * weight for weight, value in pairs)
    return mean, effective_n


def _weighted_average(pairs, now, half_life_s=RATIO_HALF_LIFE_S, max_age_s=RATIO_MAX_AGE_S):
    return _summary([(0.5 ** ((now - at) / half_life_s), value)
                     for at, value in pairs if 0 <= now - at < max_age_s])[0]


def _historical_summary(pairs, now, traversal_at, cache_key=None):
    target = local_datetime(traversal_at)
    period = get_period_type(traversal_at)
    target_minute = target.hour * 60 + target.minute
    def bins():
        groups = {}
        for at, value in pairs:
            age = now - at
            if not 0 <= age < HISTORY_MAX_AGE_S or not math.isfinite(value) or value < 0:
                continue
            local = local_datetime(at)
            key = local.weekday(), (local.hour * 60 + local.minute) // 30, get_period_type(at)
            weight = .5 ** (age / (14 * 86400))
            sums = groups.setdefault(key, [0., 0., 0.])
            sums[0] += weight
            sums[1] += weight * value
            sums[2] += weight * weight
        return groups
    # Rebuild on new samples or once a minute. This is a pooled, half-hour
    # baseline; replay never caches samples not yet available at issue time.
    groups = bins() if cache_key is None else _cached(_bin_cache, (cache_key, int(now // 60)), bins)
    weight_sum = weighted_total = squared_sum = 0.
    for (weekday, slot, historical_period), (weights, values, squared) in groups.items():
        minute_delta = abs(slot * 30 + 15 - target_minute)
        minute_delta = min(minute_delta, 1440 - minute_delta)
        day_weight = 1.0 if weekday == target.weekday() else (
            .35 if (weekday < 5) == (target.weekday() < 5) else .1)
        period_weight = 1.0 if historical_period == period else .25
        # Nearby time-of-day samples dominate; sparse bins pool smoothly.
        time_weight = .05 + .95 * math.exp(-.5 * (minute_delta / 90) ** 2)
        context_weight = day_weight * period_weight * time_weight
        weight_sum += context_weight * weights
        weighted_total += context_weight * values
        squared_sum += context_weight * context_weight * squared
    return (weighted_total / weight_sum, weight_sum * weight_sum / squared_sum) if weight_sum else (None, 0.)


def _route_pairs(system_id, route_name, version):
    selected = [(key, samples) for key, samples in segment_observations.items()
                if (key[0], key[1], str(key[2])) == (system_id, route_name, str(version))]
    token = 'route', tuple(_sample_token(key, samples) for key, samples in selected)
    pairs = _cached(_pair_cache, token,
        lambda: sorted((at, ratio) for key, samples in selected for at, duration, baseline, ratio in samples))
    return pairs, token


def _segment_ratio(system_id, route_name, version, index, now, traversal_at):
    key = system_id, route_name, str(version), index
    samples = segment_observations.get(key, [])
    own_token = _sample_token(key, samples)
    own = _cached(_pair_cache, own_token,
                  lambda: sorted((at, ratio) for at, duration, baseline, ratio in samples))
    route, route_token = _route_pairs(system_id, route_name, version)
    route_history, _ = _historical_summary(route, now, traversal_at, route_token)
    own_history, historical_n = _historical_summary(own, now, traversal_at, own_token)
    prior = route_history if route_history is not None else _get_fallback_ratio(traversal_at)
    if own_history is not None:
        weight = historical_n / (historical_n + RATIO_SHRINKAGE_K)
        prior = weight * own_history + (1 - weight) * prior
    own_recent = [(0.5 ** ((now - at) / RATIO_HALF_LIFE_S), ratio)
                  for at, ratio in _recent(own, now)]
    recent, recent_n = _summary(own_recent)
    if recent is None:
        route_recent = _weighted_average(_recent(route, now), now)
        route_base = route_history
        if route_recent is not None and route_base:
            recent, recent_n = prior * route_recent / route_base, 1.0
    if recent is not None:
        horizon = max(0.0, traversal_at - now)
        live_weight = recent_n / (recent_n + RATIO_SHRINKAGE_K) * math.exp(-horizon / 900)
        prior = live_weight * recent + (1 - live_weight) * prior
    return max(0.01, prior), historical_n


def _stop_duration(system_id, route_name, version, index, now, traversal_at, elapsed=0):
    key = system_id, route_name, str(version), index
    samples = stop_dwell_observations.get(key, [])
    token = _sample_token(key, samples)
    pairs = _cached(_pair_cache, token, lambda: sorted(samples))
    # Survival conditioning: a bus held for 5 minutes needs a remaining-time
    # estimate from holds that actually lasted longer, not mean minus 5 minutes.
    remaining = pairs if elapsed == 0 else [(at, duration - elapsed) for at, duration in pairs if duration >= elapsed]
    historic, n = _historical_summary(remaining, now, traversal_at, token if elapsed == 0 else None)
    recent_remaining = [(at, duration - elapsed) for at, duration in _recent(pairs, now) if duration >= elapsed]
    recent = _weighted_average(recent_remaining, now)
    if historic is None:
        return DEFAULT_STOP_DWELL_S, 0
    weight = math.exp(-max(0, traversal_at - now) / 900) * n / (n + 5)
    return max(0.0, (1 - weight) * historic + weight * (recent if recent is not None else historic)), n


def _compute_vehicle_eta(system_id, state, stop_sequence, dest_idx, now=None):
    now = time.time() if now is None else now
    if (not stop_sequence or state.get('status') != 'DETERMINED' or not state.get('coords1')
            or state.get('index') is None or not 0 <= dest_idx < len(stop_sequence)):
        return None
    vehicle = state.get('vehicle_obj')
    if vehicle is not None and out_of_service(vehicle):
        return None
    observed_at = state.get('last_fix_time')
    if observed_at is None or not 0 <= now - observed_at <= MAX_FIX_GAP_S:
        return None
    if state.get('data_status') in ('gps_jump', 'invalid_coordinates', 'stale', 'out_of_service'):
        return None
    version = str(getattr(stop_sequence, 'version', state.get('route_version', 'legacy')))
    if state.get('route_version') != version:
        return None
    index = state['index']
    progress = max(0.0, min(1.0, state.get('progress_pct', 0.0)))
    remaining_distance = (1 - progress) * stop_sequence[index][6]
    ratio, n = _segment_ratio(system_id, state['route_name'], version, index, now, now)
    travel = (1 - progress) * stop_sequence[index][7] * ratio
    sample_counts = [n]
    arrival = state.get('stop_arrival_time')
    if arrival is not None:
        travel = remaining_distance = 0.0
    elif state.get('moving') is False and state.get('segment_entry_time') is not None:
        elapsed = now - state['segment_entry_time']
        durations = [(at, duration - elapsed) for at, duration, baseline, r in
                     segment_observations.get((system_id, state['route_name'], version, index), [])
                     if duration > elapsed]
        residual, _ = _historical_summary(durations, now, now)
        if residual is not None:
            travel = max(travel, residual)
    if state.get('moving'):
        travel = max(0.0, travel - (now - observed_at))
    if index != dest_idx:
        elapsed_stop = max(0.0, now - arrival) if arrival is not None else 0.0
        dwell, count = _stop_duration(system_id, state['route_name'], version, index,
                                      now, now + travel, elapsed_stop)
        sample_counts.append(count)
        travel += dwell
    for _ in range(len(stop_sequence)):
        if index == dest_idx:
            break
        index = (index + 1) % len(stop_sequence)
        pair = stop_sequence[index]
        ratio, n = _segment_ratio(system_id, state['route_name'], version, index, now, now + travel)
        sample_counts.append(n)
        travel += pair[7] * ratio
        remaining_distance += pair[6]
        if index != dest_idx:
            dwell, count = _stop_duration(system_id, state['route_name'], version, index, now, now + travel)
            sample_counts.append(count)
            travel += dwell
    offsets = _calibration_provider(system_id, state['route_name'], version, travel, now) if _calibration_provider else None
    age = now - observed_at
    uncertain = (age > STALE_AFTER_S or state.get('source_time') is None
                 or min(sample_counts, default=0) < 3 or state.get('stop_logging', False))
    return {
        'eta_timestamp': now + travel, 'time_to_dest_s': travel,
        'distance_to_dest_m': remaining_distance, 'prediction_timestamp': now,
        'data_age_s': age, 'status': 'uncertain' if uncertain else 'live',
        'route_version': version, 'model_version': MODEL_VERSION,
        'system_id': system_id, 'route_name': state['route_name'],
        'arrival_lower_ts': max(now, now + travel + offsets[0]) if offsets else None,
        'arrival_upper_ts': max(now, now + travel + offsets[1]) if offsets else None,
        'interval_calibrated': offsets is not None,
    }


_eta_smoothing_state = {}
ETA_SMOOTHING_ALPHA = .3
ETA_SMOOTHING_SNAP_RATIO = .5


def get_smoothed_eta(vehicle_id, dest_idx, raw_result, system_id=None, route_name=None):
    if raw_result is None:
        return None
    now = raw_result.get('prediction_timestamp', time.time())
    key = (system_id if system_id is not None else raw_result.get('system_id'),
           route_name if route_name is not None else raw_result.get('route_name'),
           vehicle_id, raw_result.get('route_version'), dest_idx)
    previous = _eta_smoothing_state.get(key)
    raw = raw_result['time_to_dest_s']
    previous_remaining = max(0, previous['arrival'] - now) if previous else raw
    if previous is None or abs(raw - previous_remaining) > max(30, previous_remaining * ETA_SMOOTHING_SNAP_RATIO):
        remaining = raw
    else:
        remaining = ETA_SMOOTHING_ALPHA * raw + (1 - ETA_SMOOTHING_ALPHA) * previous_remaining
    _eta_smoothing_state[key] = {'arrival': now + remaining, 'last_updated': now}
    result = dict(raw_result, eta_timestamp=now + remaining, time_to_dest_s=remaining)
    # Calibration belongs to raw predictions; do not imply the smoothed display
    # has independently calibrated bounds.
    result.update(arrival_lower_ts=None, arrival_upper_ts=None, interval_calibrated=False)
    return result


def cleanup_smoothing_state(max_age_s=1800, now=None):
    now = time.time() if now is None else now
    for key, value in list(_eta_smoothing_state.items()):
        if now - value['last_updated'] > max_age_s:
            del _eta_smoothing_state[key]
