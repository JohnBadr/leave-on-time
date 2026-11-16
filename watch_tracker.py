"""Watch the real tracker locally, seeded from a downloaded completed backup.

Original backups are read only. Each run gets new route/history copies under
Documents/LeaveOnTime-LocalTests and then uses logger's shared online pipeline.
"""

import argparse
import gzip
import hashlib
import json
import math
import re
import shutil
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

import logger


def prepare_local_run(backup_root, output_root, backup=None):
    backup_root, output_root = Path(backup_root).expanduser().resolve(), Path(output_root).expanduser().resolve()
    if backup is None:
        if not backup_root.is_dir():
            raise ValueError(f'Download a backup first; backup folder not found: {backup_root}')
        completed = sorted(path for path in backup_root.iterdir()
                           if path.is_dir() and re.fullmatch(r'\d{8}T\d{12}Z', path.name)
                           and (path / 'COMPLETE').is_file())
        if not completed:
            raise ValueError(f'No completed downloaded backups in {backup_root}')
        source = completed[-1]
    else:
        source = Path(backup).expanduser().resolve()
    if not (source / 'COMPLETE').is_file():
        raise ValueError(f'Backup is missing its completion marker: {source}')
    manifest = json.loads((source / 'manifest.json').read_text(encoding='utf-8'))
    names = ('routegraph.db', 'bullrunner.db')
    for name in names:
        compressed = name + '.gz'
        digest = hashlib.sha256()
        with (source / compressed).open('rb') as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b''):
                digest.update(chunk)
        if digest.hexdigest() != manifest['files'][compressed]['sha256'].lower():
            raise ValueError(f'Backup checksum failed: {compressed}')
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    run = output_root / stamp
    if run == source or source in run.parents:
        raise ValueError('Local test output must be outside the source backup folder')
    run.mkdir(parents=True, exist_ok=False)
    for name in names:
        target = run / name
        with gzip.open(source / (name + '.gz'), 'rb') as src, target.open('xb') as dst:
            shutil.copyfileobj(src, dst, length=1024 * 1024)
        with closing(sqlite3.connect(target)) as conn:
            conn.execute('PRAGMA journal_mode=DELETE')
            checks = [row[0] for row in conn.execute('PRAGMA quick_check')]
            if checks != ['ok']:
                raise ValueError(f'Backup database check failed for {name}: {checks}')
    return run, source


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--backup-root', type=Path, default=Path.home() / 'Documents/LeaveOnTime-Backups')
    parser.add_argument('--backup', type=Path, help='Use this specific completed backup folder instead of the newest')
    parser.add_argument('--output-root', type=Path, default=Path.home() / 'Documents/LeaveOnTime-LocalTests')
    parser.add_argument('--system-id', type=int, default=2343)
    parser.add_argument('--route', action='append', help='Show only this route; repeat for several routes')
    parser.add_argument('--vehicle', action='append', help='Show only this vehicle ID; repeat for several buses')
    parser.add_argument('--interval', type=float, default=5)
    parser.add_argument('--duration', type=float, help='Stop after this many seconds; otherwise Ctrl+C stops the local test')
    args = parser.parse_args(argv)
    if not math.isfinite(args.interval) or args.interval < 1:
        parser.error('--interval must be at least one second')
    if args.duration is not None and (not math.isfinite(args.duration) or args.duration <= 0):
        parser.error('--duration must be positive')
    try:
        run, source = prepare_local_run(args.backup_root, args.output_root, args.backup)
    except (OSError, ValueError, KeyError, sqlite3.Error) as error:
        parser.exit(1, f'Cannot prepare local tracker: {error}\n')
    print(f'History copied from: {source}', flush=True)
    print(f'Local test files: {run}', flush=True)
    print('Live display uses USF local time. Ctrl+C stops this local test.', flush=True)
    command = ['--route-db', str(run / 'routegraph.db'), '--db', str(run / 'bullrunner.db'),
               '--system-id', str(args.system_id), '--interval', str(args.interval), '--watch']
    for route in args.route or []:
        command.extend(['--watch-route', route])
    for vehicle in args.vehicle or []:
        command.extend(['--watch-vehicle', vehicle])
    if args.duration is not None:
        command.extend(['--duration', str(args.duration)])
    logger.main(command)


if __name__ == '__main__':
    main()
