"""One shared online/replay pipeline, with raw capture before inference."""

import bus_track
from eta import _compute_vehicle_eta, set_calibration_provider
from evaluation import ArrivalLabeler
from tracking_core import MAX_FIX_GAP_S, new_state, out_of_service


class Collector:
    def __init__(self, store, prediction_interval_s=15, evaluate_from=float('-inf')):
        self.store = store
        self.prediction_interval_s = prediction_interval_s
        self.evaluate_from = evaluate_from
        self.states = {}
        self.last_prediction = {}
        self.labeler = ArrivalLabeler()
        bus_track.observation_store = store
        set_calibration_provider(store.calibrated_offsets)

    def process(self, sid, vehicle, sequence, at):
        key = sid, str(vehicle.id)
        self.store.snapshot(sid, vehicle.routeName, sequence, at)
        raw_id = self.store.raw(sid, vehicle, sequence.version, at)
        state = self.states.get(key)
        identity = (vehicle.routeName, sequence.version, sequence.fingerprint,
                    str(getattr(vehicle, 'routeId', '')), str(getattr(vehicle, 'tripId', '')))
        if state is None or state.get('collector_identity') != identity:
            if state is not None:
                self.store.censor(sid, vehicle.id, 'route_changed')
            state = new_state(vehicle, vehicle.routeName, at)
            state['collector_identity'] = identity
            self.states[key] = state
            self.last_prediction.pop(key, None)
        bus_track.process_vehicle(sid, vehicle.id, state, vehicle, sequence, at)
        self.store.inference(raw_id, sid, vehicle.id, state)
        arrivals = self.labeler.observe(sid, vehicle, sequence, at)
        for target, arrival_at, details in arrivals:
            self.store.label(sid, vehicle.id, vehicle.routeName, sequence.version, target, arrival_at, details=details)
        if out_of_service(vehicle):
            self.store.censor(sid, vehicle.id, 'out_of_service')
        if at < self.evaluate_from or at - self.last_prediction.get(key, float('-inf')) < self.prediction_interval_s:
            return state
        arrived = {target for target, arrival_at, details in arrivals}
        if state.get('stop_arrival_time') is not None:
            arrived.add(state['index'])
        for target in range(len(sequence)):
            if target in arrived:
                continue  # Already at this stop; do not score it against next lap.
            result = _compute_vehicle_eta(sid, state, sequence, target, now=at)
            self.store.prediction(sid, vehicle.id, vehicle.routeName, sequence.version, target, at, result)
        self.last_prediction[key] = at
        return state

    def expire(self, now):
        self.store.expire(now)
        for (sid, vid), state in list(self.states.items()):
            last = state.get('last_fix_time')
            if now - (last if last is not None else state['cold_start_time']) > MAX_FIX_GAP_S:
                self.store.censor(sid, vid, 'feed_lost')
            if now - (state.get('last_received') or now) > 7200:
                del self.states[(sid, vid)]
        bus_track.trim_segment_observations(now)

    def finish(self, reason='shutdown'):
        for sid, vid in self.states:
            self.store.censor(sid, vid, reason)
