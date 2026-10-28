"""Replay saved raw reports without network access or writes to source data."""

import argparse
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import bus_track
from collector_runtime import Collector
from evaluation import summarize
from observation_store import ObservationStore
from eta import clear_caches, set_calibration_provider
from route_store import RouteSequence


def replay(source_path, output_path, evaluate_from=float('-inf')):
    output = Path(output_path)
    if output.exists():
        raise ValueError('Replay output must be a new file; existing data is never overwritten')
    source = sqlite3.connect(Path(source_path).resolve().as_uri() + '?mode=ro', uri=True)
    store = ObservationStore(str(output))
    bus_track.segment_observations.clear()
    bus_track.stop_dwell_observations.clear()
    clear_caches()
    collector = Collector(store, evaluate_from=evaluate_from)
    try:
        snapshots = {}
        for sid, route, version, fingerprint, sequence in source.execute('''
            SELECT system_id,route_name,route_version,fingerprint,sequence_json FROM route_snapshots'''):
            snapshots[(sid, route, version)] = RouteSequence(json.loads(sequence), version, fingerprint)
        for sid, route, version, at, payload in source.execute('''
            SELECT system_id,route_name,route_version,received_at,payload_json
            FROM raw_vehicle_reports ORDER BY received_at,id'''):
            sequence = snapshots[(sid, route, version)]
            vehicle = SimpleNamespace(**json.loads(payload))
            collector.process(sid, vehicle, sequence, at)
            collector.expire(at)
        collector.finish('dataset_end')
        # Manual audits score the same journeys after replay, without exposing
        # their future outcomes to the forecast/history calculations.
        for sid, vid, route, version, target, at, details in source.execute('''
            SELECT system_id,vehicle_id,route_name,route_version,target_index,arrival_time,details_json
            FROM arrival_labels WHERE quality='manual' ORDER BY arrival_time'''):
            start = json.loads(details).get('prediction_start')
            if start is not None:
                store.audit_label(sid, vid, route, version, target, at, start)
        return summarize(store.conn)
    finally:
        source.close()
        store.close()
        bus_track.observation_store = None
        set_calibration_provider(None)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', required=True)
    parser.add_argument('--output', required=True)
    parser.add_argument('--evaluate-from', type=float, default=float('-inf'),
                        help='UTC Unix seconds; earlier reports warm up history but are not scored')
    args = parser.parse_args()
    print(json.dumps(replay(args.input, args.output, args.evaluate_from), indent=2))


if __name__ == '__main__':
    main()
