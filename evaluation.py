"""Independent proximity labels, an accuracy scorecard, and manual auditing."""

import argparse
import json
import math
import sqlite3
import statistics
from contextlib import closing
from pathlib import Path

from tracking_core import MAX_FIX_GAP_S, MAX_SPEED_M_S, out_of_service, observation_timestamp
from utils import angle_diff, compute_bearing, get_distance_m


class ArrivalLabeler:
    """Conservative geofence entries, independent of tracker segment indices.

    Automatic labels remain provisional. A fix outside 35m followed by one
    within 20m, on the expected approach, bounds an arrival. Ambiguous stop
    occurrences, feed gaps, and pass-throughs without a nearby fix are unscored.
    """
    def __init__(self):
        self.previous = {}
        self.armed = {}

    def observe(self, sid, vehicle, sequence, received_at):
        key = sid, str(vehicle.id), vehicle.routeName, sequence.version
        if out_of_service(vehicle):
            self.previous.pop(key, None)
            self.armed.pop(key, None)
            return []
        try:
            coords = float(vehicle.latitude), float(vehicle.longitude)
            if not all(math.isfinite(c) for c in coords):
                return []
        except (ValueError, TypeError, AttributeError):
            return []
        source = observation_timestamp(vehicle)
        at = source if source is not None else received_at
        signature = coords, str(getattr(vehicle, 'created', None))
        if not -30 <= received_at - at <= MAX_FIX_GAP_S:
            return []
        previous = self.previous.get(key)
        if previous is not None and (at <= previous[0] or signature == previous[2]):
            return []
        if previous is None:
            self.previous[key] = at, coords, signature
            self.armed[key] = {i for i, pair in enumerate(sequence)
                               if get_distance_m(*coords, pair[11], pair[12]) >= 35}
            return []
        delta = at - previous[0]
        moved = get_distance_m(*previous[1], *coords)
        if delta > MAX_FIX_GAP_S or moved / delta > MAX_SPEED_M_S:
            self.previous.pop(key, None)
            self.armed.pop(key, None)
            return []
        self.previous[key] = at, coords, signature
        heading = compute_bearing(*previous[1], *coords) if moved > 5 else None
        candidates = []
        for index, pair in enumerate(sequence):
            current_distance = get_distance_m(*coords, pair[11], pair[12])
            if current_distance >= 35:
                self.armed.setdefault(key, set()).add(index)
            if current_distance > 20 or index not in self.armed.get(key, set()):
                continue
            points = json.loads(pair[5])
            approach = None
            for a, b in reversed(list(zip(points, points[1:]))):
                if a != b:
                    approach = compute_bearing(a[1], a[0], b[1], b[0])
                    break
            if heading is None or approach is None or angle_diff(heading, approach) > 65:
                continue
            candidates.append(index)
        if len(candidates) != 1:
            return []
        self.armed[key].discard(candidates[0])
        return [(candidates[0], at, {'earliest': previous[0], 'latest': at,
                                   'source_time_verified': source is not None})]


def horizon_bucket(seconds):
    for boundary, label in [(120, '0-2 min'), (300, '2-5 min'), (600, '5-10 min'), (1200, '10-20 min')]:
        if seconds < boundary:
            return label
    return '20+ min'


def _metrics(rows):
    errors = [actual - predicted for predicted, actual, lower, upper, journey in rows]
    ordered = sorted(abs(error) for error in errors)
    bounded = [(actual, lower, upper) for predicted, actual, lower, upper, journey in rows if lower is not None and upper is not None]
    return {
        'predictions': len(rows), 'arrivals': len({row[4] for row in rows}),
        'median_abs_error_s': statistics.median(ordered),
        'mean_abs_error_s': sum(ordered) / len(ordered),
        'p90_abs_error_s': ordered[math.ceil(.9 * len(ordered)) - 1],
        'bias_s_actual_minus_predicted': sum(errors) / len(errors),
        'errors_over_5_min': sum(error > 300 for error in ordered),
        'interval_coverage': sum(lower <= actual <= upper for actual, lower, upper in bounded) / len(bounded) if bounded else None,
    }


def summarize(conn, quality='manual'):
    counts = dict(conn.execute('SELECT outcome,COUNT(*) FROM eta_predictions GROUP BY outcome'))
    total = sum(counts.values())
    result = {'outcomes': counts, 'prediction_availability': (total - counts.get('unavailable', 0)) / total if total else None,
              'label_quality': quality, 'by_predicted_horizon': {}, 'by_actual_horizon': {},
              'collector_health': dict(conn.execute('SELECT status,COUNT(*) FROM collector_health GROUP BY status'))}
    groups = [{}, {}]
    for row in conn.execute('''SELECT prediction_time,predicted_arrival,actual_arrival,
            lower_ts,upper_ts,vehicle_id,target_index,label_quality,route_version,route_name,system_id
            FROM eta_predictions WHERE outcome='completed' '''):
        issued, predicted, actual, lower, upper, vehicle, target, label, version, route, sid = row
        if quality != 'all' and label != quality:
            continue
        journey = sid, route, version, vehicle, target, actual
        values = predicted, actual, lower, upper, journey
        groups[0].setdefault(horizon_bucket(predicted - issued), []).append(values)
        groups[1].setdefault(horizon_bucket(actual - issued), []).append(values)
    for name, group in zip(('by_predicted_horizon', 'by_actual_horizon'), groups):
        result[name] = {bucket: _metrics(rows) for bucket, rows in group.items()}
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    report = sub.add_parser('report')
    report.add_argument('--db', default='bullrunner.db')
    report.add_argument('--quality', choices=['manual', 'automatic', 'all'], default='manual')
    label = sub.add_parser('label', help='Add a manually checked arrival and the start of its prediction window')
    label.add_argument('--db', default='bullrunner.db')
    label.add_argument('--system-id', type=int, default=2343)
    for name in ('vehicle-id', 'route', 'version'):
        label.add_argument('--' + name, required=True)
    label.add_argument('--target-index', type=int, required=True)
    label.add_argument('--arrival-time', type=float, required=True, help='UTC Unix seconds')
    label.add_argument('--prediction-start', type=float, required=True, help='UTC seconds after the previous visit to this stop')
    args = parser.parse_args()
    if args.command == 'report':
        with closing(sqlite3.connect(Path(args.db).resolve().as_uri() + '?mode=ro', uri=True)) as conn:
            print(json.dumps(summarize(conn, args.quality), indent=2))
    else:
        from observation_store import ObservationStore
        store = ObservationStore(args.db)
        try:
            store.audit_label(args.system_id, args.vehicle_id, args.route, args.version,
                              args.target_index, args.arrival_time, args.prediction_start)
        finally:
            store.close()


if __name__ == '__main__':
    main()
