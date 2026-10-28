"""Continuous, restartable Passio recorder using the shared tracking pipeline."""

import argparse
import signal
import threading
import time

import bus_track
from collector_runtime import Collector
from passio_client import PassioClient
from route_store import RouteSequence
from service_calendar import get_period_type  # retained for existing callers


def main(argv=None, default_db='bullrunner.db'):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', default=default_db, help='Raw reports, history, predictions, and labels')
    parser.add_argument('--route-db', default='routegraph.db', help='Opened read-only; never migrated by the collector')
    parser.add_argument('--system-id', action='append', type=int, help='Repeat for additional systems; defaults to USF 2343')
    parser.add_argument('--interval', type=float, default=5)
    parser.add_argument('--prediction-interval', type=float, default=15)
    parser.add_argument('--duration', type=float, help='Optional run time in seconds')
    parser.add_argument('--trust-created-time', action='store_true',
                        help='Use created as source fix time only after its semantics/timezone have been verified')
    args = parser.parse_args(argv)
    if args.interval < 1 or args.prediction_interval < 1 or (args.duration is not None and args.duration <= 0):
        parser.error('Intervals and duration must be positive; intervals must be at least one second')
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda signum, frame: stop.set())
    # The recorder never opens or alters tracking.db. All its data is isolated
    # in the explicitly selected --db output, including restart history.
    store = bus_track.configure(args.route_db, args.db, ':memory:')
    collector = Collector(store, args.prediction_interval)
    clients = {sid: PassioClient(sid, trust_created_time=args.trust_created_time) for sid in (args.system_id or [2343])}
    route_cache, failures, retry_at = {}, {}, {}
    started = time.monotonic()
    print(f'Recording {list(clients)} to {args.db}; route graph is read-only.', flush=True)
    try:
        while not stop.is_set() and (args.duration is None or time.monotonic() - started < args.duration):
            tick = time.monotonic()
            for sid, client in clients.items():
                now = time.time()
                if now < retry_at.get(sid, 0):
                    continue
                try:
                    vehicles = client.getVehicles()
                except Exception as error:
                    failures[sid] = failures.get(sid, 0) + 1
                    retry_at[sid] = now + min(60, 2 ** min(failures[sid], 6))
                    store.health(now, sid, 'feed_error', str(error))
                    print(f'[feed {sid}] {error}', flush=True)
                    continue
                failures[sid] = 0
                now = time.time()
                store.health(now, sid, 'ok' if vehicles else 'no_vehicles', str(len(vehicles)))
                for vehicle in vehicles:
                    key = sid, vehicle.routeName
                    try:
                        cached = route_cache.get(key)
                        refresh_after = 5 if cached is not None and cached[1].version == 'unavailable' else 90
                        if cached is None or now - cached[0] >= refresh_after:
                            try:
                                sequence = bus_track.get_stop_sequence(sid, vehicle.routeName)
                                route_cache[key] = now, sequence
                            except Exception as error:
                                # Route locks/errors must not discard raw reports.
                                store.health(now, sid, 'route_error', str(error))
                                sequence = RouteSequence(version='unavailable')
                                route_cache[key] = now, sequence
                        else:
                            sequence = cached[1]
                        collector.process(sid, vehicle, sequence, now)
                    except Exception as error:
                        store.health(now, sid, 'processing_error', f'{vehicle.id}: {error}')
                        print(f'[vehicle {vehicle.id}] {error}', flush=True)
            collector.expire(time.time())
            wait_s = max(0, args.interval - (time.monotonic() - tick))
            if args.duration is not None:
                wait_s = min(wait_s, max(0, args.duration - (time.monotonic() - started)))
            stop.wait(wait_s)
    finally:
        collector.finish()
        store.health(time.time(), None, 'shutdown')
        for client in clients.values():
            client.close()
        bus_track.close()
    print('Recorder stopped; observations were committed as they arrived.', flush=True)


if __name__ == '__main__':
    main()
