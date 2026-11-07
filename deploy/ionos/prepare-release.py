#!/usr/bin/env python3
"""Prepare the deployed directory layout from versioned sources, without secrets."""
import argparse
import json
from pathlib import Path
import shutil
import subprocess


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('destination', type=Path, help='A new directory outside the repository')
    args = parser.parse_args()
    config = Path(__file__).resolve().parent
    repository = config.parents[1]
    destination = args.destination.resolve()
    if destination == repository or repository in destination.parents:
        parser.error('The release directory must be outside the repository')
    destination.mkdir(parents=True, exist_ok=False, mode=0o700)
    source = destination / 'source'
    source.mkdir()
    for name in ('app.py', 'remote_fetch.py', 'gpx_utils.py', 'gpx_export.py', 'spatial.py', 'water.gpx', 'toilets_all.osm.gpx'):
        shutil.copy2(repository / name, source / name)
    shutil.copytree(repository / 'templates', source / 'templates')
    shutil.copy2(repository / 'requirements.lock', source / 'requirements.txt')
    for name in ('Dockerfile', 'wsgi.py', '.dockerignore'):
        shutil.copy2(config / name, source / name)
    for name in ('compose.yaml', 'Caddyfile', 'backup.py', 'bootstrap-vps.sh', 'README.md', 'runtime.env.example',
                 'smoke-test.py', 'check-live.py', 'check-smtp.py', 'notify-release.py', 'release-notes.txt', 'watermap-backup.service', 'watermap-backup.timer'):
        shutil.copy2(config / name, destination / name)
    shutil.copy2(repository / 'requirements.lock', destination / 'requirements.lock')
    commit = subprocess.check_output(['git', '-C', str(repository), 'rev-parse', 'HEAD'], text=True).strip()
    changes = subprocess.check_output(['git', '-C', str(repository), 'status', '--porcelain', '--untracked-files=normal'], text=True)
    manifest = {'source_commit': commit, 'working_tree_dirty': bool(changes.strip()), 'contains_secrets_or_user_data': False}
    (destination / 'release.json').write_text(json.dumps(manifest, indent=2) + '\n')
    print(json.dumps({'destination': str(destination), **manifest}))


if __name__ == '__main__':
    main()
