"""Bounded requests to the same vehicle endpoint used by passiogo_fix."""

from types import SimpleNamespace

import requests


class PassioClient:
    def __init__(self, system_id, session=None, trust_created_time=False):
        self.id = system_id
        self.session = session or requests.Session()
        self.trust_created_time = trust_created_time

    def getVehicles(self):
        response = self.session.post('https://passiogo.com/mapGetData.php?getBuses=2',
                                     json={'s0': str(self.id), 'sA': 1}, timeout=(3, 10))
        response.raise_for_status()
        data = response.json()
        if data.get('error'):
            raise ValueError(f'Passio error: {data["error"]}')
        if not isinstance(data.get('buses'), dict):
            raise ValueError('Passio response contains no buses object')
        vehicles = []
        for key, reports in data['buses'].items():
            if key == '-1' or not reports:
                continue
            raw = reports[0]
            if not isinstance(raw, dict):
                raise ValueError('Invalid bus report')
            vehicles.append(SimpleNamespace(
                id=str(raw.get('busId') or key), name=raw.get('busName'),
                routeName=raw.get('route'), routeId=raw.get('routeId'),
                tripId=raw.get('tripId'), latitude=raw.get('latitude'),
                longitude=raw.get('longitude'), created=raw.get('created'),
                calculatedCourse=raw.get('calculatedCourse'), speed=raw.get('speed'),
                outOfService=raw.get('outOfService'), paxLoad=raw.get('paxLoad100'),
                provider_payload=raw,
                timestamp_verified=self.trust_created_time,
            ))
        return vehicles

    def close(self):
        self.session.close()
