"""Durable observations and evaluation data. Legacy tables are left intact."""

import json
import sqlite3
import threading

from service_calendar import get_period_type
from tracking_core import source_timestamp

MODEL_VERSION = 'history-v1'


class ObservationStore:
    def __init__(self, path):
        self.conn = sqlite3.connect(path, timeout=10, check_same_thread=False)
        self.lock = threading.RLock()
        # A process only opens this explicit output file; never the route graph.
        self.conn.execute('PRAGMA journal_mode=WAL')
        self.conn.executescript('''
            CREATE TABLE IF NOT EXISTS eta_segment_samples (
                system_id INTEGER, route_name TEXT, route_version TEXT,
                segment_index INTEGER, timestamp REAL, vehicle_id TEXT,
                duration_s REAL, baseline_s REAL, ratio REAL, period_type TEXT,
                PRIMARY KEY(system_id, route_name, route_version, segment_index, timestamp, vehicle_id)
            );
            CREATE TABLE IF NOT EXISTS eta_stop_samples (
                system_id INTEGER, route_name TEXT, route_version TEXT,
                segment_index INTEGER, stop_id TEXT, timestamp REAL,
                vehicle_id TEXT, duration_s REAL, period_type TEXT,
                PRIMARY KEY(system_id, route_name, route_version, segment_index, timestamp, vehicle_id)
            );
            CREATE TABLE IF NOT EXISTS route_snapshots (
                system_id INTEGER, route_name TEXT, route_version TEXT,
                fingerprint TEXT, recorded_at REAL, sequence_json TEXT,
                PRIMARY KEY(system_id, route_name, route_version)
            );
            CREATE TABLE IF NOT EXISTS raw_vehicle_reports (
                id INTEGER PRIMARY KEY, system_id INTEGER, vehicle_id TEXT,
                route_name TEXT, route_version TEXT, received_at REAL,
                source_time REAL, payload_json TEXT, tracking_status TEXT,
                data_status TEXT, segment_index INTEGER, progress REAL
            );
            CREATE INDEX IF NOT EXISTS raw_reports_time ON raw_vehicle_reports(received_at, id);
            CREATE TABLE IF NOT EXISTS tracking_events (
                id INTEGER PRIMARY KEY, raw_report_id INTEGER,
                system_id INTEGER, vehicle_id TEXT, route_name TEXT,
                route_version TEXT, event_time REAL, event_type TEXT, payload_json TEXT
            );
            CREATE TABLE IF NOT EXISTS eta_predictions (
                id INTEGER PRIMARY KEY, system_id INTEGER, vehicle_id TEXT,
                route_name TEXT, route_version TEXT, target_index INTEGER,
                prediction_time REAL, predicted_arrival REAL, horizon_s REAL,
                data_age_s REAL, status TEXT, outcome TEXT DEFAULT 'pending',
                actual_arrival REAL, label_quality TEXT, lower_ts REAL, upper_ts REAL,
                model_version TEXT
            );
            CREATE INDEX IF NOT EXISTS prediction_target ON eta_predictions
                (system_id, vehicle_id, route_name, route_version, target_index, outcome);
            CREATE INDEX IF NOT EXISTS prediction_calibration ON eta_predictions
                (system_id,route_name,route_version,label_quality,model_version,actual_arrival,horizon_s);
            CREATE TABLE IF NOT EXISTS arrival_labels (
                id INTEGER PRIMARY KEY, system_id INTEGER, vehicle_id TEXT,
                route_name TEXT, route_version TEXT, target_index INTEGER,
                arrival_time REAL, quality TEXT, details_json TEXT
            );
            CREATE TABLE IF NOT EXISTS collector_health (
                id INTEGER PRIMARY KEY, timestamp REAL, system_id INTEGER,
                status TEXT, detail TEXT
            );
        ''')
        self.conn.commit()

    def close(self):
        self.conn.close()

    def health(self, at, system_id, status, detail=''):
        with self.lock, self.conn:
            self.conn.execute('INSERT INTO collector_health(timestamp,system_id,status,detail) VALUES(?,?,?,?)',
                              (at, system_id, status, detail))

    def snapshot(self, system_id, route_name, sequence, at):
        with self.lock, self.conn:
            self.conn.execute('INSERT OR IGNORE INTO route_snapshots VALUES(?,?,?,?,?,?)',
                              (system_id, route_name, sequence.version, sequence.fingerprint,
                               at, json.dumps(list(sequence), separators=(',', ':'))))

    def raw(self, system_id, vehicle, route_version, at):
        # Preserve all provider fields exposed by passiogo_fix, including an
        # uninterpreted created value. Avoid serializing its parent system object.
        payload = {k: v for k, v in vars(vehicle).items() if k != 'system'}
        with self.lock, self.conn:
            result = self.conn.execute('''INSERT INTO raw_vehicle_reports
                (system_id,vehicle_id,route_name,route_version,received_at,source_time,payload_json)
                VALUES(?,?,?,?,?,?,?)''', (system_id, str(vehicle.id), vehicle.routeName,
                    route_version, at, source_timestamp(getattr(vehicle, 'created', None)), json.dumps(payload, default=str)))
            return result.lastrowid

    def inference(self, raw_id, system_id, vehicle_id, state):
        with self.lock, self.conn:
            self.conn.execute('''UPDATE raw_vehicle_reports SET tracking_status=?,
                data_status=?, segment_index=?, progress=? WHERE id=?''',
                (state['status'], state.get('data_status'),
                 state.get('index'), state.get('progress_pct'), raw_id))
            for event in state.get('events', []):
                self.conn.execute('''INSERT INTO tracking_events
                    (raw_report_id,system_id,vehicle_id,route_name,route_version,event_time,event_type,payload_json)
                    VALUES(?,?,?,?,?,?,?,?)''', (raw_id, system_id, str(vehicle_id), state['route_name'],
                    state['route_version'], event['timestamp'], event['type'], json.dumps(event)))

    def sample(self, system_id, route_name, version, vehicle_id, event):
        base = (system_id, route_name, str(version), event['segment_index'])
        with self.lock, self.conn:
            if event['type'] == 'segment':
                self.conn.execute('INSERT OR IGNORE INTO eta_segment_samples VALUES(?,?,?,?,?,?,?,?,?,?)',
                    (*base, event['timestamp'], str(vehicle_id), event['duration_s'],
                     event['baseline_s'], event['ratio'], get_period_type(event['timestamp'])))
            elif event['type'] == 'dwell':
                self.conn.execute('INSERT OR IGNORE INTO eta_stop_samples VALUES(?,?,?,?,?,?,?,?,?)',
                    (*base, str(event['stop_id']), event['timestamp'], str(vehicle_id),
                     event['duration_s'], get_period_type(event['timestamp'])))

    def load_samples(self, now, max_age_s=56 * 86400):
        segments, stops = {}, {}
        with self.lock:
            for sid, route, version, idx, at, duration, baseline, ratio in self.conn.execute('''
                SELECT system_id,route_name,route_version,segment_index,timestamp,duration_s,baseline_s,ratio
                FROM eta_segment_samples WHERE timestamp BETWEEN ? AND ? ORDER BY timestamp
            ''', (now - max_age_s, now)):
                segments.setdefault((sid, route, version, idx), []).append((at, duration, baseline, ratio))
            for sid, route, version, idx, at, duration in self.conn.execute('''
                SELECT system_id,route_name,route_version,segment_index,timestamp,duration_s
                FROM eta_stop_samples WHERE timestamp BETWEEN ? AND ? ORDER BY timestamp
            ''', (now - max_age_s, now)):
                stops.setdefault((sid, route, version, idx), []).append((at, duration))
        return segments, stops

    def prediction(self, sid, vehicle_id, route, version, target, at, result):
        available = result is not None
        with self.lock, self.conn:
            self.conn.execute('''INSERT INTO eta_predictions
                (system_id,vehicle_id,route_name,route_version,target_index,prediction_time,
                 predicted_arrival,horizon_s,data_age_s,status,outcome,lower_ts,upper_ts,model_version)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                (sid, str(vehicle_id), route, str(version), target, at,
                 result['eta_timestamp'] if available else None,
                 result['time_to_dest_s'] if available else None,
                 result.get('data_age_s') if available else None,
                 result['status'] if available else 'unavailable',
                 'pending' if available else 'unavailable',
                 result.get('arrival_lower_ts') if available else None,
                 result.get('arrival_upper_ts') if available else None, MODEL_VERSION))

    def label(self, sid, vehicle_id, route, version, target, at, quality='automatic', details=None):
        with self.lock, self.conn:
            self.conn.execute('''INSERT INTO arrival_labels
                (system_id,vehicle_id,route_name,route_version,target_index,arrival_time,quality,details_json)
                VALUES(?,?,?,?,?,?,?,?)''',
                (sid, str(vehicle_id), route, str(version), target, at, quality, json.dumps(details or {})))
            self.conn.execute('''UPDATE eta_predictions SET actual_arrival=?, label_quality=?, outcome='completed'
                WHERE system_id=? AND vehicle_id=? AND route_name=? AND route_version=? AND target_index=?
                AND outcome='pending' AND prediction_time<?''',
                (at, quality, sid, str(vehicle_id), route, str(version), target, at))

    def censor(self, sid, vehicle_id, reason, version=None):
        query = "UPDATE eta_predictions SET outcome=? WHERE system_id=? AND vehicle_id=? AND outcome='pending'"
        values = ['censored_' + reason, sid, str(vehicle_id)]
        if version is not None:
            query += ' AND route_version=?'
            values.append(str(version))
        with self.lock, self.conn:
            self.conn.execute(query, values)

    def audit_label(self, sid, vehicle_id, route, version, target, at, prediction_start):
        if prediction_start >= at:
            raise ValueError('Prediction window must begin before the arrival')
        with self.lock, self.conn:
            self.conn.execute('''INSERT INTO arrival_labels
                (system_id,vehicle_id,route_name,route_version,target_index,arrival_time,quality,details_json)
                VALUES(?,?,?,?,?,?,'manual',?)''',
                (sid, str(vehicle_id), route, str(version), target, at,
                 json.dumps({'prediction_start': prediction_start})))
            # An explicit journey window permits correction of provisional
            # labels without changing predictions for an earlier/later lap.
            self.conn.execute('''UPDATE eta_predictions
                SET actual_arrival=?, label_quality='manual', outcome='completed'
                WHERE system_id=? AND vehicle_id=? AND route_name=? AND route_version=?
                AND target_index=? AND predicted_arrival IS NOT NULL
                AND prediction_time>=? AND prediction_time<?''',
                (at, sid, str(vehicle_id), route, str(version), target, prediction_start, at))

    def expire(self, now, max_age_s=7200):
        with self.lock, self.conn:
            self.conn.execute("UPDATE eta_predictions SET outcome='censored_timeout' WHERE outcome='pending' AND prediction_time<?",
                              (now - max_age_s,))

    def calibrated_offsets(self, sid, route, version, horizon, now):
        # Only independently audited labels can calibrate public uncertainty.
        with self.lock:
            errors = [row[0] for row in self.conn.execute('''SELECT AVG(actual_arrival-predicted_arrival)
                FROM eta_predictions WHERE system_id=? AND route_name=? AND route_version=?
                AND outcome='completed' AND label_quality='manual' AND model_version=?
                AND actual_arrival<=? AND actual_arrival>? AND horizon_s BETWEEN ? AND ?
                GROUP BY vehicle_id,target_index,actual_arrival
            ''', (sid, route, str(version), MODEL_VERSION, now, now - 28 * 86400,
                  max(0, horizon * 0.5), max(120, horizon * 1.5)))]
        if len(errors) < 30:
            return None
        errors.sort()
        return errors[int((len(errors) - 1) * .1)], errors[int((len(errors) - 1) * .9)]
