#!/usr/bin/env python3
"""Pull the latest VPS backup to the RPi and verify all data plus SQLite restore."""
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import tarfile
import tempfile
import time

ROOT = Path(__file__).resolve().parent
LOCAL = ROOT / 'vps-backups'
SSH = ['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10', '-o', 'ServerAliveInterval=15', '-o', 'ServerAliveCountMax=3',
       '-o', 'StrictHostKeyChecking=yes', '-o', 'IdentitiesOnly=yes', '-i', '/home/yann/.ssh/watermap_ionos_ed25519', 'root@82.165.183.204']


def main():
    os.umask(0o077)
    LOCAL.mkdir(mode=0o700, exist_ok=True)
    name = subprocess.check_output(SSH + ['cat /var/backups/watermap/latest'], text=True, timeout=30).strip()
    assert re.fullmatch(r'watermap-\d{8}T\d{6}Z\.tar\.gz', name), 'Invalid archive name'
    final = LOCAL / name
    expected = subprocess.check_output(SSH + [f'sha256sum /var/backups/watermap/{name}'], text=True, timeout=30).split()[0]
    if not final.exists():
        partial = LOCAL / (name + '.partial')
        with partial.open('wb') as stream:
            subprocess.run(SSH + [f'cat /var/backups/watermap/{name}'], stdout=stream, check=True, timeout=180)
        assert hashlib.sha256(partial.read_bytes()).hexdigest() == expected, 'Transfer checksum mismatch'
        partial.replace(final)
    assert hashlib.sha256(final.read_bytes()).hexdigest() == expected
    with tarfile.open(final, 'r:gz') as archive:
        manifest = json.load(archive.extractfile('manifest.json'))
        for path, digest in manifest['files'].items():
            assert hashlib.sha256(archive.extractfile(path).read()).hexdigest() == digest, 'Data checksum mismatch'
        with tempfile.TemporaryDirectory(prefix='watermap-restore-') as directory:
            database = Path(directory) / 'users.db'
            database.write_bytes(archive.extractfile('data/db/users.db').read())
            connection = sqlite3.connect(database)
            assert connection.execute('pragma integrity_check').fetchone()[0] == 'ok'
            assert connection.execute('select count(*) from user').fetchone()[0] == manifest['users']
            connection.close()
    for path in LOCAL.iterdir():
        if re.fullmatch(r'watermap-\d{8}T\d{6}Z\.tar\.gz', path.name) and time.time() - path.stat().st_mtime > 14 * 86400:
            path.unlink()
    print(json.dumps({'archive': name, 'data_files_verified': len(manifest['files']), 'sqlite_restore': 'ok'}))


if __name__ == '__main__':
    main()
