import hashlib
import importlib.util
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_release_includes_new_modules(tmp_path):
    destination = tmp_path/'release'
    result = subprocess.run([sys.executable,str(ROOT/'deploy/ionos/prepare-release.py'),str(destination)],capture_output=True,text=True,check=True)
    manifest = json.loads(result.stdout)
    assert manifest['contains_secrets_or_user_data'] is False
    for name in ('app.py','remote_fetch.py','gpx_utils.py','gpx_export.py','mail_safety.py','spatial.py'):
        assert (destination/'source'/name).read_bytes() == (ROOT/name).read_bytes()
    assert not (destination/'source'/'.env').exists()
    assert not (destination/'source'/'db').exists()


def test_production_entrypoint(tmp_path):
    environment = dict(os.environ,SECRET_KEY=secrets.token_urlsafe(48),WATERMAP_DATA_DIR=str(tmp_path))
    script = '''import runpy
app = runpy.run_path('deploy/ionos/wsgi.py')['app']
assert app.config['SESSION_COOKIE_SECURE'] is True
assert app.config['PUBLIC_BASE_URL'] == 'https://watermap.dolisi.fr'
client = app.test_client()
assert client.get('/',base_url='https://attacker.invalid').status_code == 400
assert client.get('/login',base_url='https://watermap.dolisi.fr').status_code == 200
assert client.get('/_health',base_url='http://127.0.0.1').status_code == 200
'''
    # A second process must start against the same already indexed database.
    for _ in range(2):
        result = subprocess.run([sys.executable,'-c',script],cwd=ROOT,env=environment,capture_output=True,text=True)
        assert result.returncode == 0,result.stderr


def test_backup_checksum_and_required_checks(tmp_path):
    spec = importlib.util.spec_from_file_location('pull_backup',ROOT/'deploy/ionos/pull-backup.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    path = tmp_path/'archive'
    path.write_bytes(b'backup-test')
    assert module.checksum(path) == hashlib.sha256(b'backup-test').hexdigest()
    with pytest.raises(RuntimeError,match='tampered'):
        module.require(False,'tampered')


@pytest.mark.parametrize('tamper',[False,True])
def test_restore_verification_with_uncheckpointed_wal(tmp_path,monkeypatch,tamper):
    import io
    import sqlite3
    import tarfile
    spec = importlib.util.spec_from_file_location('pull_backup_restore',ROOT/'deploy/ionos/pull-backup.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    source = tmp_path/'source.db'
    connection = sqlite3.connect(source)
    connection.execute('pragma journal_mode=wal')
    connection.execute('pragma wal_autocheckpoint=0')
    connection.execute('create table user(id integer primary key)')
    connection.execute('insert into user values(1)')
    connection.commit()
    local = tmp_path/'backups'
    local.mkdir()
    name = 'watermap-20261104T000000Z.tar.gz'
    archive_path = local/name
    files = {'data/db/users.db'+suffix:Path(str(source)+suffix).read_bytes() for suffix in ('','-wal','-shm')}
    manifest = {'users':1,'files':{name:hashlib.sha256(content).hexdigest() for name,content in files.items()}}
    if tamper:
        manifest['files']['data/db/users.db'] = '0'*64
    with tarfile.open(archive_path,'w:gz') as archive:
        for member,content in {**files,'manifest.json':json.dumps(manifest).encode()}.items():
            info = tarfile.TarInfo(member)
            info.size = len(content)
            archive.addfile(info,io.BytesIO(content))
    connection.close()
    digest = hashlib.sha256(archive_path.read_bytes()).hexdigest()
    monkeypatch.setattr(module,'LOCAL',local)
    answers = iter([name+'\n',digest+' '+name])
    monkeypatch.setattr(module.subprocess,'check_output',lambda *args,**kwargs:next(answers))
    if tamper:
        with pytest.raises(RuntimeError,match='Data checksum mismatch'):
            module.main()
    else:
        module.main()


def test_staging_entrypoint_is_isolated(tmp_path):
    environment = dict(os.environ, SECRET_KEY=secrets.token_urlsafe(48),
                       WATERMAP_DATA_DIR=str(tmp_path),
                       WATERMAP_PUBLIC_URL='https://stag.watermap.dolisi.fr',
                       MAIL_SUPPRESS_SEND='false', WATERMAP_INSTANCE='stag', MAIL_REDIRECT_TO='ydolisi@gmail.com')
    script = """import runpy
app=runpy.run_path('deploy/stag/wsgi.py')['app']
assert app.config['SESSION_COOKIE_SECURE'] is True
assert app.config['SESSION_COOKIE_NAME']=='watermap_stag_session'
assert app.config['MAIL_SUPPRESS_SEND'] is False
assert app.config['MAIL_REDIRECT_TO']=='ydolisi@gmail.com'
client=app.test_client()
assert client.get('/login',base_url='https://stag.watermap.dolisi.fr').status_code==200
assert client.get('/login',base_url='https://watermap.dolisi.fr').status_code==400
assert client.get('/_health',base_url='http://127.0.0.1').status_code==200
"""
    result=subprocess.run([sys.executable,'-c',script],cwd=ROOT,env=environment,capture_output=True,text=True)
    assert result.returncode==0,result.stderr
    environment['MAIL_REDIRECT_TO']='other@example.invalid'
    result=subprocess.run([sys.executable,'-c',script],cwd=ROOT,env=environment,capture_output=True,text=True)
    assert result.returncode!=0
    assert 'must redirect exclusively' in result.stderr
