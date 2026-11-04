#!/usr/bin/env python3
"""Back up the deployment and a consistent data snapshot; retain seven days."""
import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import time

ROOT = Path('/opt/watermap')
BACKUPS = Path('/var/backups/watermap')


def command(*args):
    return subprocess.check_output(args, cwd=ROOT, text=True, timeout=30).strip()


def interrupted(signum, frame):
    raise RuntimeError('Backup interrupted')


def main():
    os.umask(0o077)
    signal.signal(signal.SIGTERM, interrupted)
    BACKUPS.mkdir(mode=0o700, exist_ok=True)
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    name = f'watermap-{stamp}.tar.gz'
    final = BACKUPS / name
    partial = BACKUPS / (name + '.partial')
    with tempfile.TemporaryDirectory(prefix='.staging-', dir=BACKUPS) as temp:
        saved = Path(temp) / 'data'
        container = command('docker', 'compose', 'ps', '-q', 'app')
        paused = False
        try:
            if container:
                command('docker', 'pause', container)
                paused = True
            shutil.copytree(ROOT / 'data', saved)
        finally:
            if paused:
                command('docker', 'unpause', container)
        if container:
            # Pausing suspends Docker health probes. Wait for the next successful
            # probe so the backup finishes only after the service has recovered.
            for _ in range(45):
                health = command('docker', 'inspect', '--format', '{{.State.Health.Status}}', container)
                if health == 'healthy':
                    break
                time.sleep(1)
            else:
                raise RuntimeError('The application did not recover after the snapshot')
        connection = sqlite3.connect(saved / 'db/users.db')
        connection.execute('pragma wal_checkpoint(TRUNCATE)')
        if connection.execute('pragma integrity_check').fetchone()[0] != 'ok':
            raise RuntimeError('SQLite integrity check failed')
        users = connection.execute('select count(*) from user').fetchone()[0]
        connection.close()
        manifest = {'timestamp': stamp, 'users': users, 'files': {}}
        for path in sorted(saved.rglob('*')):
            if path.is_file():
                with path.open('rb') as source:
                    manifest['files'][str(Path('data') / path.relative_to(saved))] = hashlib.file_digest(source, 'sha256').hexdigest()
        manifest_path = Path(temp) / 'manifest.json'
        manifest_path.write_text(json.dumps(manifest, indent=2) + '\n')
        with tarfile.open(partial, 'x:gz') as archive:
            archive.add(saved, arcname='data')
            archive.add(manifest_path, arcname='manifest.json')
            for item in ('source', 'compose.yaml', 'Caddyfile', 'runtime.env', 'requirements.lock', 'backup.py', 'watermap-backup.service', 'watermap-backup.timer', 'README.md'):
                path = ROOT / item
                if path.exists():
                    archive.add(path, arcname=item)
        partial.replace(final)
    (BACKUPS / 'latest.tmp').write_text(name + '\n')
    (BACKUPS / 'latest.tmp').replace(BACKUPS / 'latest')
    for path in BACKUPS.iterdir():
        if re.fullmatch(r'watermap-\d{8}T\d{6}Z\.tar\.gz', path.name) and time.time() - path.stat().st_mtime > 7 * 86400:
            path.unlink()
    print(json.dumps({'archive': name, 'users': users, 'data_files': len(manifest['files']), 'sqlite': 'ok'}))


if __name__ == '__main__':
    if '--resume-only' in sys.argv:
        container = command('docker', 'compose', 'ps', '-q', 'app')
        if container and command('docker', 'inspect', '--format', '{{.State.Paused}}', container) == 'true':
            command('docker', 'unpause', container)
    else:
        main()
