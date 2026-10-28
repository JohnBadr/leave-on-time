"""Continuously evaluate the shared engine in an isolated shadow database.

Legacy shadow tables are preserved. New predictions are recorded for every
available vehicle/stop at fixed cadence, with provisional proximity labels,
explicit missing predictions, and terminal outcomes for unfinished journeys.
Use repeated --system-id arguments for a cross-system cohort.
"""

import argparse
import random

from logger import main as record
from route_store import open_routes


def main(argv=None):
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument('--random-systems', type=int, help='Choose a stable random cohort from the route graph')
    parser.add_argument('--seed', type=int)
    selection, remaining = parser.parse_known_args(argv)
    if selection.random_systems is not None:
        if selection.random_systems < 1 or '--system-id' in remaining:
            parser.error('--random-systems must be positive and cannot be combined with --system-id')
        route_db = 'routegraph.db'
        for index, argument in enumerate(remaining):
            if argument == '--route-db' and index + 1 < len(remaining):
                route_db = remaining[index + 1]
            elif argument.startswith('--route-db='):
                route_db = argument.split('=', 1)[1]
        conn = open_routes(route_db)
        try:
            systems = [row[0] for row in conn.execute('SELECT DISTINCT system_id FROM route_graphs ORDER BY system_id')]
        finally:
            conn.close()
        if not systems:
            parser.error('The route graph contains no systems')
        chosen = random.Random(selection.seed).sample(systems, min(selection.random_systems, len(systems)))
        for system in chosen:
            remaining.extend(['--system-id', str(system)])
    record(remaining, default_db='observations_shadow_test.db')


if __name__ == '__main__':
    main()
