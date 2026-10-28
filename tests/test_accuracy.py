import hashlib
import importlib.util
import json
import sqlite3
import tempfile
import unittest
import os
import subprocess
import sys
import time
from datetime import datetime
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import bus_track
import eta
from collector_runtime import Collector
from evaluation import ArrivalLabeler, summarize
from observation_store import ObservationStore
from passio_client import PassioClient
from replay import replay
from route_store import RouteSequence, read_route
from tracking_core import new_state, process_fix, source_timestamp
from service_calendar import get_period_type, local_datetime
from utils import get_distance_m, project_onto_shape

BASE = 1800000000.0
POINTS = [(-82.0, 28.0), (-81.998, 28.0), (-81.998, 28.002), (-82.0, 28.002)]


def route(version='1'):
    rows = []
    for i, (a, b) in enumerate(zip(POINTS, POINTS[1:] + POINTS[:1])):
        rows.append((i, str(i), str((i + 1) % 4), 0, 0, json.dumps([a, b]),
                     get_distance_m(a[1], a[0], b[1], b[0]), 20., a[1], a[0],
                     'Origin', b[1], b[0], 'Destination'))
    return RouteSequence(rows, version)


def vehicle(at, lon=-81.9995, lat=28., name='Red', identifier='bus'):
    return SimpleNamespace(id=identifier, routeName=name, routeId='r', tripId='t',
                           latitude=lat, longitude=lon, created=str(at),
                           speed=None, calculatedCourse=None, outOfService=False,
                           timestamp_verified=True)


class GeometryTests(unittest.TestCase):
    def test_calendar_uses_usf_event_date_and_handles_dst(self):
        holiday = datetime.fromisoformat('2026-09-08T02:00:00+00:00').timestamp()
        self.assertEqual(get_period_type(holiday), 'holiday')
        before = local_datetime(datetime.fromisoformat('2026-11-01T05:30:00+00:00').timestamp())
        after = local_datetime(datetime.fromisoformat('2026-11-01T06:30:00+00:00').timestamp())
        self.assertEqual((before.hour, after.hour), (1, 1))
        self.assertNotEqual(before.utcoffset(), after.utcoffset())
    def test_uneven_vertices_measure_meters(self):
        sparse = [(-82., 28.), (-81.999, 28.), (-81.99, 28.)]
        dense = [(-82. + i * .001, 28.) for i in range(11)]
        self.assertAlmostEqual(project_onto_shape(28., -81.999, sparse)[1], .1, places=5)
        self.assertAlmostEqual(project_onto_shape(28., -81.995, sparse)[1], .5, places=5)
        self.assertAlmostEqual(project_onto_shape(28., -81.995, sparse)[1],
                               project_onto_shape(28., -81.995, dense)[1], places=5)

    def test_degenerate_shape_does_not_match_everywhere(self):
        self.assertGreater(project_onto_shape(29., -82., [(-82., 28.)])[0], 100000)
        self.assertEqual(project_onto_shape(28., -82., [(-82., 28.)] * 2)[1], 0)
        self.assertEqual(project_onto_shape(28., -82., [])[0], float('inf'))


class TrackingTests(unittest.TestCase):
    def setUp(self):
        self.sequence = route()
        self.state = new_state(vehicle(BASE), 'Red', BASE)

    def step(self, seconds, lon, lat=28.):
        v = vehicle(BASE + seconds, lon, lat)
        return process_fix(self.state, v, self.sequence, BASE + seconds)

    def test_cold_start_does_not_label_partial_segment(self):
        self.step(0, -81.9995)
        self.assertEqual(self.state['status'], 'DETERMINED')
        events = self.step(20, -81.998)
        self.assertTrue(any(e['type'] == 'arrival' for e in events))
        self.assertFalse(any(e['type'] == 'segment' for e in events))

    def test_discovered_already_at_stop_does_not_label_partial_dwell(self):
        self.step(0, -81.99815)
        self.step(10, -81.998)
        self.step(40, -81.998)
        events = self.step(50, -81.998, 28.0005)
        self.assertFalse(any(e['type'] == 'dwell' for e in events))
        self.assertTrue(any(e['type'] == 'dwell_partial' for e in events))

    def test_wait_is_measured_until_departure_and_counted_once(self):
        self.step(0, -81.9995)
        self.step(20, -81.998)
        self.step(50, -81.998)
        self.assertEqual(self.state['index'], 0)
        events = self.step(60, -81.998, 28.0005)
        dwell = next(e for e in events if e['type'] == 'dwell')
        self.assertEqual(dwell['duration_s'], 30)
        self.assertEqual(self.state['index'], 1)
        events = self.step(90, -81.998, 28.002)
        travel = next(e for e in events if e['type'] == 'segment')
        self.assertEqual(travel['duration_s'], 40)
        self.assertEqual(travel['ratio'], 2)

    def test_long_hold_is_retained(self):
        self.step(0, -81.9995)
        self.step(20, -81.998)
        for t in range(80, 321, 60):
            self.step(t, -81.998)
        events = self.step(330, -81.998, 28.0005)
        self.assertEqual(next(e['duration_s'] for e in events if e['type'] == 'dwell'), 300)

    def test_actual_delta_and_repeated_out_of_order_reports(self):
        self.step(0, -81.9995)
        self.step(10, -81.999)
        expected = get_distance_m(28., -81.9995, 28., -81.999) / 10
        self.assertAlmostEqual(self.state['speed_m_s'], expected)
        v = vehicle(BASE + 10, -81.999)
        self.assertEqual(process_fix(self.state, v, self.sequence, BASE + 20), [])
        self.assertEqual(self.state['last_fix_time'], BASE + 10)
        process_fix(self.state, vehicle(BASE + 5), self.sequence, BASE + 21)
        self.assertEqual(self.state['last_fix_time'], BASE + 10)

    def test_stale_gps_is_not_a_driver_hold(self):
        self.step(0, -81.9995)
        process_fix(self.state, vehicle(BASE), self.sequence, BASE + 200)
        self.assertEqual(self.state['data_status'], 'stale')
        self.assertEqual(self.state['events'], [])

    def test_route_change_and_gap_invalidate_training(self):
        self.step(0, -81.9995)
        self.state.update(segment_valid=True, segment_entry_time=BASE)
        process_fix(self.state, vehicle(BASE + 5), route('2'), BASE + 5)
        self.assertFalse(self.state['segment_valid'])
        self.assertEqual(self.state['route_version'], '2')
        process_fix(self.state, vehicle(BASE + 200, -81.998), route('2'), BASE + 200)
        self.assertFalse(any(e['type'] == 'segment' for e in self.state['events']))

    def test_off_route_does_not_walk_through_stops(self):
        self.step(0, -81.9995)
        events = self.step(20, -81.997, 28.001)
        self.assertEqual(events, [])
        self.assertEqual(self.state['status'], 'UNKNOWN')

    def test_source_timestamps_do_not_guess_naive_timezone(self):
        self.assertIsNone(source_timestamp('2026-10-25 08:00:00'))
        self.assertEqual(source_timestamp('2026-10-25T08:00:00Z'), 1792915200)

    def test_roster_keeps_two_routes(self):
        bus_track.tracked_vehicles.clear()
        vehicles = [vehicle(BASE, name='Red'), vehicle(BASE, name='Blue', identifier='second')]
        bus_track.reconcile_vehicles(2343, {'Red', 'Blue'}, vehicles, BASE)
        state = bus_track.tracked_vehicles[2343]['bus']
        bus_track.reconcile_vehicles(2343, {'Red', 'Blue'}, vehicles, BASE + 90)
        self.assertEqual(len(bus_track.tracked_vehicles[2343]), 2)
        self.assertIs(bus_track.tracked_vehicles[2343]['bus'], state)

    def test_unverified_created_does_not_make_a_live_bus_stale(self):
        v = vehicle(BASE)
        v.timestamp_verified = False
        v.created = str(BASE - 86400)  # Could mean assignment creation, not GPS time.
        process_fix(self.state, v, self.sequence, BASE)
        self.assertEqual(self.state['data_status'], 'receive_time')
        self.assertEqual(self.state['last_fix_time'], BASE)
        self.assertIsNone(self.state['source_time'])

    def test_skipped_stop_pass_through_and_loop_boundary(self):
        self.step(0, -81.9983)
        events = self.step(10, -81.998, 28.0004)
        self.assertEqual(self.state['index'], 1)
        self.assertTrue(any(e['type'] == 'arrival' and e['inferred'] for e in events))
        self.assertEqual(next(e['duration_s'] for e in events if e['type'] == 'dwell'), 0)
        state = new_state(vehicle(BASE, -82., 28.0003), 'Red', BASE)
        process_fix(state, vehicle(BASE, -82., 28.0003), self.sequence, BASE)
        process_fix(state, vehicle(BASE + 10, -81.9996, 28.), self.sequence, BASE + 10)
        self.assertEqual(state['index'], 0)
        self.assertEqual(state['lap'], 1)


class EtaTests(unittest.TestCase):
    def setUp(self):
        bus_track.segment_observations.clear()
        bus_track.stop_dwell_observations.clear()
        eta._eta_smoothing_state.clear()
        eta.clear_caches()
        eta.set_calibration_provider(None)
        self.sequence = route()
        self.state = new_state(vehicle(BASE), 'Red', BASE)
        process_fix(self.state, vehicle(BASE), self.sequence, BASE)

    def test_old_history_is_used_and_future_samples_are_not(self):
        for i in range(4):
            bus_track.segment_observations[(2343, 'Red', '1', i)] = [(BASE - 7 * 86400, 60, 20, 3)]
        normal = eta._compute_vehicle_eta(2343, self.state, self.sequence, 1, BASE)
        self.assertGreater(normal['time_to_dest_s'], 100)
        bus_track.segment_observations[(2343, 'Red', '1', 0)].append((BASE + 1, 2000, 20, 100))
        result = eta._compute_vehicle_eta(2343, self.state, self.sequence, 1, BASE)
        self.assertEqual(normal['time_to_dest_s'], result['time_to_dest_s'])

    def test_other_route_version_cannot_influence_eta(self):
        before = eta._compute_vehicle_eta(2343, self.state, self.sequence, 1, BASE)
        bus_track.segment_observations[(2343, 'Red', 'old', 0)] = [(BASE - 5, 2000, 20, 100)]
        after = eta._compute_vehicle_eta(2343, self.state, self.sequence, 1, BASE)
        self.assertEqual(before['time_to_dest_s'], after['time_to_dest_s'])

    def test_hold_remaining_is_conditioned_on_elapsed(self):
        bus_track.stop_dwell_observations[(2343, 'Red', '1', 0)] = [
            (BASE - 1000, 60), (BASE - 900, 300), (BASE - 800, 600)]
        remaining, _ = eta._stop_duration(2343, 'Red', '1', 0, BASE, BASE, 250)
        self.assertGreater(remaining, 100)

    def test_arrived_stop_has_zero_arrival_and_later_stop_includes_wait(self):
        process_fix(self.state, vehicle(BASE + 20, -81.998), self.sequence, BASE + 20)
        here = eta._compute_vehicle_eta(2343, self.state, self.sequence, 0, BASE + 20)
        later = eta._compute_vehicle_eta(2343, self.state, self.sequence, 1, BASE + 20)
        self.assertEqual(here['time_to_dest_s'], 0)
        self.assertGreater(later['time_to_dest_s'], 20)

    def test_stale_or_unresolved_prediction_unavailable(self):
        self.assertIsNone(eta._compute_vehicle_eta(2343, self.state, self.sequence, 1, BASE + 121))
        self.state['status'] = 'UNKNOWN'
        self.assertIsNone(eta._compute_vehicle_eta(2343, self.state, self.sequence, 1, BASE))

    def test_countdown_smoothing_keeps_correct_arrival(self):
        for elapsed in (0, 60, 120):
            raw = {'prediction_timestamp': BASE + elapsed, 'time_to_dest_s': 300 - elapsed,
                   'eta_timestamp': BASE + 300, 'route_version': '1', 'distance_to_dest_m': 100}
            shown = eta.get_smoothed_eta('bus', 1, raw)
            self.assertEqual(shown['eta_timestamp'], BASE + 300)

    def test_smoothing_separates_systems_with_identical_vehicle_ids(self):
        for sid, duration in [(2343, 300), (99, 200)]:
            raw = {'prediction_timestamp': BASE, 'time_to_dest_s': duration,
                   'eta_timestamp': BASE + duration, 'route_version': '1', 'system_id': sid,
                   'route_name': 'Red', 'distance_to_dest_m': 100}
            self.assertEqual(eta.get_smoothed_eta('bus', 1, raw)['time_to_dest_s'], duration)


class StorageAndReplayTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'observations.db'
        self.store = ObservationStore(self.path)
        bus_track.segment_observations.clear()
        bus_track.stop_dwell_observations.clear()
        eta.clear_caches()

    def tearDown(self):
        self.store.close()
        bus_track.observation_store = None
        eta.set_calibration_provider(None)
        self.tmp.cleanup()

    def test_legacy_observations_not_assigned_a_current_version(self):
        self.store.conn.execute('CREATE TABLE segment_observations(ratio REAL)')
        self.store.conn.execute('INSERT INTO segment_observations VALUES(100)')
        self.store.conn.commit()
        self.assertEqual(self.store.load_samples(BASE), ({}, {}))
        self.assertEqual(self.store.conn.execute('SELECT ratio FROM segment_observations').fetchone()[0], 100)

    def test_samples_survive_reopen_and_remain_versioned(self):
        event = {'type': 'segment', 'segment_index': 0, 'timestamp': BASE,
                 'duration_s': 120, 'baseline_s': 20, 'ratio': 6}
        self.store.sample(2343, 'Red', '1', 'bus', event)
        self.store.close()
        self.store = ObservationStore(self.path)
        samples, _ = self.store.load_samples(BASE)
        self.assertEqual(samples[(2343, 'Red', '1', 0)][0][3], 6)

    def test_independent_label_ignores_tracker_index(self):
        labeler = ArrivalLabeler()
        self.assertEqual(labeler.observe(2343, vehicle(BASE), route(), BASE), [])
        labels = labeler.observe(2343, vehicle(BASE + 20, -81.998), route(), BASE + 20)
        self.assertEqual(labels[0][0], 0)

    def test_independent_label_retains_armed_stop_during_slow_approach(self):
        labeler = ArrivalLabeler()
        labeler.observe(2343, vehicle(BASE, -81.9986), route(), BASE)
        self.assertEqual(labeler.observe(2343, vehicle(BASE + 10, -81.9983), route(), BASE + 10), [])
        labels = labeler.observe(2343, vehicle(BASE + 20, -81.9981), route(), BASE + 20)
        self.assertEqual(labels[0][0], 0)
        self.assertEqual(labeler.observe(2343, vehicle(BASE + 30, -81.998), route(), BASE + 30), [])

    def test_raw_report_is_durable_even_when_inference_fails(self):
        collector = Collector(self.store)
        with patch.object(bus_track, 'process_vehicle', side_effect=ValueError('bad route')):
            with self.assertRaises(ValueError):
                collector.process(2343, vehicle(BASE), route(), BASE)
        self.assertEqual(self.store.conn.execute('SELECT COUNT(*) FROM raw_vehicle_reports').fetchone()[0], 1)

    def test_calibration_counts_arrivals_not_repeated_predictions(self):
        for i in range(35):
            result = {'eta_timestamp': BASE + 90, 'time_to_dest_s': 90 - i, 'status': 'uncertain'}
            self.store.prediction(2343, 'bus', 'Red', '1', 0, BASE + i, result)
        self.store.audit_label(2343, 'bus', 'Red', '1', 0, BASE + 120, BASE)
        self.assertIsNone(self.store.calibrated_offsets(2343, 'Red', '1', 90, BASE + 121))

    def test_unknown_reports_are_saved_before_inference(self):
        collector = Collector(self.store)
        v = vehicle(BASE, -81.99, 28.01)
        collector.process(2343, v, route(), BASE)
        self.assertEqual(self.store.conn.execute('SELECT COUNT(*) FROM raw_vehicle_reports').fetchone()[0], 1)
        self.assertEqual(self.store.conn.execute('SELECT tracking_status FROM raw_vehicle_reports').fetchone()[0], 'UNKNOWN')
        self.assertGreater(self.store.conn.execute("SELECT COUNT(*) FROM eta_predictions WHERE outcome='unavailable'").fetchone()[0], 0)

    def test_censoring_and_manual_correction(self):
        result = {'eta_timestamp': BASE + 60, 'time_to_dest_s': 60, 'status': 'uncertain'}
        self.store.prediction(2343, 'bus', 'Red', '1', 0, BASE, result)
        self.store.censor(2343, 'bus', 'feed_lost')
        self.assertEqual(summarize(self.store.conn)['outcomes']['censored_feed_lost'], 1)
        self.store.audit_label(2343, 'bus', 'Red', '1', 0, BASE + 90, BASE)
        summary = summarize(self.store.conn)
        self.assertEqual(summary['by_predicted_horizon']['0-2 min']['mean_abs_error_s'], 30)

    def test_replay_is_repeatable_and_does_not_change_source(self):
        collector = Collector(self.store)
        for elapsed, lon, lat in [(0, -81.9995, 28.), (20, -81.998, 28.),
                                   (50, -81.998, 28.), (60, -81.998, 28.0005), (90, -81.998, 28.002)]:
            collector.process(2343, vehicle(BASE + elapsed, lon, lat), route(), BASE + elapsed)
        collector.finish()
        self.store.conn.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        before = hashlib.sha256(self.path.read_bytes()).hexdigest()
        outputs = [Path(self.tmp.name) / f'replay{i}.db' for i in (1, 2)]
        reports = [replay(self.path, output) for output in outputs]
        self.assertEqual(reports[0], reports[1])
        self.assertEqual(hashlib.sha256(self.path.read_bytes()).hexdigest(), before)
        with self.assertRaises(ValueError):
            replay(self.path, outputs[0])

    def test_client_has_timeouts_and_keeps_provider_payload(self):
        session = SimpleNamespace(post=unittest.mock.Mock())
        response = session.post.return_value
        response.json.return_value = {'buses': {'bus': [{'busId': 'bus', 'route': 'Red',
                                                        'latitude': 28, 'longitude': -82, 'extra': 'kept'}]}}
        client = PassioClient(2343, session)
        self.assertEqual(client.getVehicles()[0].provider_payload['extra'], 'kept')
        self.assertEqual(session.post.call_args.kwargs['timeout'], (3, 10))


spec = importlib.util.spec_from_file_location('route_updater', Path(__file__).parents[1] / 'precomputing-logic' / 'update_routes.py')
updater = importlib.util.module_from_spec(spec)
spec.loader.exec_module(updater)


class UpdaterTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(':memory:')
        updater.ensure_schema(self.conn)
        self.stops = [SimpleNamespace(id=str(i), latitude=lat, longitude=lon, name=str(i),
                     routesAndPositions={'r': [i]}) for i, (lon, lat) in enumerate(POINTS)]
        self.system = SimpleNamespace(id=2343, name='USF', getStops=lambda: self.stops,
            getRoutes=lambda: [SimpleNamespace(myid='r', name='Red', groupColor='red')])

    def tearDown(self):
        self.conn.close()

    @staticmethod
    def fetch(a, b, *args):
        return 100., 20., json.dumps([[a.longitude, a.latitude], [b.longitude, b.latitude]])

    def update(self, **kwargs):
        with patch.object(updater, 'fetch_segment', self.fetch):
            return updater.update_system(self.conn, self.system, 'unused', 1, 1, 0, False, **kwargs)

    def version(self):
        return self.conn.execute('SELECT version FROM route_graphs').fetchone()[0]

    def test_new_unchanged_changed_and_failed_updates(self):
        self.update()
        self.assertEqual(self.version(), 1)
        self.update()
        self.assertEqual(self.version(), 1)
        self.stops[1].latitude += .001
        self.update()
        self.assertEqual(self.version(), 2)
        before = self.conn.execute('SELECT * FROM stop_pairs').fetchall()
        self.stops[1].latitude += .001
        with patch.object(updater, 'fetch_segment', side_effect=RuntimeError('offline')):
            with self.assertRaises(RuntimeError):
                updater.update_system(self.conn, self.system, 'unused', 1, 1, 0, False)
        self.assertEqual(self.version(), 2)
        self.assertEqual(self.conn.execute('SELECT * FROM stop_pairs').fetchall(), before)

    def test_repeated_stop_occurrences_are_retained(self):
        self.stops[0].routesAndPositions['r'] = [0, 2]
        self.stops[1].routesAndPositions['r'] = [1, 3]
        self.stops[2].routesAndPositions['r'] = [4]
        self.stops[3].routesAndPositions['r'] = [5]
        self.update()
        ids = [row[0] for row in self.conn.execute('SELECT origin_stop_id FROM stop_pairs ORDER BY position')]
        self.assertEqual(ids, ['0', '1', '0', '1', '2', '3'])

    def test_refetch_does_not_bump_version_for_unchanged_geometry(self):
        self.update()
        self.update(force_geometry=True)
        self.assertEqual(self.version(), 1)

    def test_geometry_only_change_bumps_version(self):
        self.update()
        def detour(a, b, *args):
            return 150., 30., json.dumps([[a.longitude, a.latitude], [b.longitude, b.latitude]])
        with patch.object(updater, 'fetch_segment', detour):
            updater.update_system(self.conn, self.system, 'unused', 1, 1, 0, False, force_geometry=True)
        self.assertEqual(self.version(), 2)

    def test_dry_run_does_not_write_route_changes(self):
        self.update()
        before = self.conn.iterdump()
        before = list(before)
        self.stops[0].latitude += .001
        with patch.object(updater, 'fetch_segment', side_effect=AssertionError('dry run fetched geometry')):
            updater.update_system(self.conn, self.system, 'unused', 1, 1, 0, True)
        self.assertEqual(list(self.conn.iterdump()), before)

    def test_readers_get_version_and_snapshot_together(self):
        self.update()
        sequence = read_route(self.conn, 2343, 'Red')
        self.assertEqual(sequence.version, '1')
        self.assertEqual(len(sequence), 4)

    def test_legacy_constraint_migration_preserves_rows(self):
        old = sqlite3.connect(':memory:')
        try:
            old.executescript('''CREATE TABLE route_graphs(id INTEGER PRIMARY KEY,system_id INTEGER,route_name TEXT,color TEXT);
                CREATE TABLE stop_pairs(id INTEGER PRIMARY KEY,route_graph_id INTEGER,position INTEGER,
                origin_stop_id TEXT,dest_stop_id TEXT,road_distance_m REAL,road_duration_s REAL,
                UNIQUE(route_graph_id,origin_stop_id,dest_stop_id));
                INSERT INTO route_graphs VALUES(1,2343,'Red','red');
                INSERT INTO stop_pairs VALUES(1,1,0,'a','b',100,20);''')
            updater.ensure_schema(old)
            self.assertEqual(old.execute('SELECT version FROM route_graphs').fetchone()[0], 1)
            old.execute("INSERT INTO stop_pairs(route_graph_id,position,origin_stop_id,dest_stop_id) VALUES(1,1,'a','b')")
            self.assertEqual(old.execute('SELECT COUNT(*) FROM stop_pairs').fetchone()[0], 2)
        finally:
            old.close()


class CommandLineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.graph = self.root / 'graph.db'
        conn = sqlite3.connect(self.graph)
        updater.ensure_schema(conn)
        conn.execute("INSERT INTO systems(system_id,name) VALUES(2343,'USF')")
        conn.execute("INSERT INTO route_graphs(id,system_id,route_name,color) VALUES(1,2343,'Red','red')")
        for i, (lon, lat) in enumerate(POINTS):
            conn.execute('INSERT INTO stops(system_id,stop_id,name,lat,lon) VALUES(?,?,?,?,?)', (2343, str(i), str(i), lat, lon))
        for pair in route():
            conn.execute('''INSERT INTO stop_pairs(route_graph_id,position,origin_stop_id,dest_stop_id,
                bearing,is_complex_zone,shape_points,road_distance_m,road_duration_s) VALUES(1,?,?,?,?,?,?,?,?)''', pair[:8])
        conn.commit()
        conn.close()

    def tearDown(self):
        bus_track.close()
        eta.set_calibration_provider(None)
        self.tmp.cleanup()

    def test_module_imports_create_no_databases(self):
        env = dict(os.environ, PYTHONPATH=str(Path(__file__).parents[1]))
        working = self.root / 'imports'
        working.mkdir()
        result = subprocess.run([sys.executable, '-B', '-c',
            'import bus_track, eta, logger, shadow_tester, replay; import pathlib; print(list(pathlib.Path().iterdir()))'],
            cwd=working, env=env, capture_output=True, text=True, check=True)
        self.assertEqual(result.stdout.strip(), '[]')

    def test_recorder_cli_with_fake_feed_never_changes_route_graph(self):
        import logger
        before = self.graph.read_bytes()
        output = self.root / 'recorded.db'
        client = SimpleNamespace(getVehicles=lambda: [vehicle(time.time())], close=lambda: None)
        with patch.object(logger, 'PassioClient', return_value=client), patch.object(logger.signal, 'signal'):
            logger.main(['--route-db', str(self.graph), '--db', str(output), '--duration', '.2'])
        self.assertEqual(self.graph.read_bytes(), before)
        with closing(sqlite3.connect(output)) as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM raw_vehicle_reports').fetchone()[0], 1)
            self.assertGreater(conn.execute('SELECT COUNT(*) FROM eta_predictions').fetchone()[0], 0)
        report = subprocess.run([sys.executable, '-B', str(Path(__file__).parents[1] / 'evaluation.py'),
            'report', '--db', str(output), '--quality', 'automatic'], capture_output=True, text=True, check=True)
        self.assertIn('prediction_availability', json.loads(report.stdout))

    def test_recorder_keeps_raw_report_when_route_is_locked(self):
        import logger
        output = self.root / 'locked.db'
        client = SimpleNamespace(getVehicles=lambda: [vehicle(time.time())], close=lambda: None)
        with patch.object(logger, 'PassioClient', return_value=client), patch.object(logger.signal, 'signal'), \
             patch.object(bus_track, 'get_stop_sequence', side_effect=sqlite3.OperationalError('database is locked')):
            logger.main(['--route-db', str(self.graph), '--db', str(output), '--duration', '.2'])
        with closing(sqlite3.connect(output)) as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM raw_vehicle_reports').fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM collector_health WHERE status='route_error'").fetchone()[0], 1)

    def test_migrate_only_does_not_call_passio(self):
        args = SimpleNamespace(db=str(self.graph), workers=1, delay=0, timeout=1,
            dry_run=False, migrate_only=True, refresh_geometry=False)
        with patch.object(updater, 'parse_args', return_value=args), \
             patch.object(updater.passiogo, 'getSystems', side_effect=AssertionError('unexpected network')):
            updater.main()

    def test_cli_dry_run_does_not_migrate_database(self):
        self.graph.unlink()
        conn = sqlite3.connect(self.graph)
        conn.executescript('''CREATE TABLE systems(system_id INTEGER,name TEXT);
            CREATE TABLE route_graphs(id INTEGER,system_id INTEGER,route_name TEXT,color TEXT);
            CREATE TABLE stops(system_id INTEGER,stop_id TEXT,lat REAL,lon REAL,name TEXT);''')
        conn.close()
        before = self.graph.read_bytes()
        args = SimpleNamespace(db=str(self.graph), workers=1, delay=0, timeout=1,
            dry_run=True, migrate_only=False, refresh_geometry=False, system_id=None)
        with patch.object(updater, 'parse_args', return_value=args), patch.object(updater.passiogo, 'getSystems', return_value=[]):
            updater.main()
        self.assertEqual(self.graph.read_bytes(), before)


if __name__ == '__main__':
    unittest.main()
