import io
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from unittest.mock import patch

import pytest

from conftest import account, csrf, login, post


def test_headers_csrf_logout(client, module, owner):
    response = client.get('/')
    assert response.headers['Cache-Control'] == 'private, no-store'
    assert response.headers['X-Content-Type-Options'] == 'nosniff'
    assert response.headers['Referrer-Policy'] == 'no-referrer'
    assert "frame-ancestors 'self'" in response.headers['Content-Security-Policy']
    assert client.get('/logout').status_code == 405
    assert client.post('/logout').status_code == 400
    assert post(client, '/logout').status_code == 302
    assert client.get('/status/missing').status_code == 302


def test_reset_token_single_use_and_session_revocation(module, client, owner):
    other = module.app.test_client()
    assert login(other).status_code == 302
    token = owner.get_reset_token()
    assert module.User.verify_reset_token(token).id == owner.id
    path = '/reset-password/' + token
    assert client.post(path, data={'csrf_token': csrf(client, path), 'password': 'replacement-password'}).status_code == 302
    assert module.User.verify_reset_token(token) is None
    assert other.get('/status/missing').status_code == 302
    assert login(other, password='replacement-password').status_code == 302
    assert module.User.verify_reset_token('not-a-token') is None


@pytest.mark.parametrize('password', ['', 'short', 'x'*513])
def test_change_rejects_weak_or_huge_password(module, client, owner, password):
    response = post(client, '/change-password', old_password='valid-password-123', new_password=password)
    assert response.status_code == 200
    assert owner.check_password('valid-password-123')


def test_change_keeps_current_session_and_revokes_others(module, client, owner):
    other = module.app.test_client()
    login(other)
    token = owner.get_reset_token()
    assert post(client, '/change-password', old_password='valid-password-123', new_password='replacement-password').status_code == 302
    assert client.get('/change-password').status_code == 200
    assert other.get('/change-password').status_code == 302
    assert module.User.verify_reset_token(token) is None


def test_email_normalization_and_missing_password(module, client):
    assert post(client, '/register', name='Test', email=' OWNER@EXAMPLE.TEST ', password='valid-password-123').status_code == 302
    assert module.db.session.execute(module.db.select(module.User)).scalar_one().email == 'owner@example.test'
    assert post(client, '/login', email='owner@example.test').status_code == 200
    assert login(client, email='OWNER@EXAMPLE.TEST').status_code == 302


def test_recovery_generic_message_and_canonical_link(module, client):
    account(module)
    with patch.object(module.mail, 'send') as send:
        first = post(client, '/forgot-password', email='owner@example.test')
        # Wait for the bounded mail worker; no network mail is sent in this test.
        module.mail_executor.submit(lambda: None).result(timeout=2)
        assert send.call_count == 1
        assert 'https://watermap.example/reset-password/' in send.call_args.args[0].body
        second = post(client, '/forgot-password', email='absent@example.test')
    assert b'Email non trouv' not in second.data
    assert first.data == second.data


def test_malformed_session_and_login_rate_limit(module, client):
    with client.session_transaction() as session:
        session['_user_id'] = 'broken'
    assert client.get('/').status_code == 200
    token = csrf(client)
    for _ in range(11):
        response = client.post('/login', data={'csrf_token': token, 'email': 'absent@example.test', 'password': 'wrong'})
    assert response.status_code == 429
    assert response.is_json


def gpx(points=((48.0, 2.0), (48.01, 2.01))):
    return ('<gpx><trk><trkseg>' + ''.join(f'<trkpt lat="{lat}" lon="{lon}"/>' for lat, lon in points) + '</trkseg></trk></gpx>').encode()


def wait_for(client, task):
    for _ in range(200):
        response = client.get('/status/' + task)
        assert response.status_code == 200
        if response.json['done']:
            return response.json
        time.sleep(0.02)
    pytest.fail('Task did not finish')


def test_processing_isolation_escaping_and_distance(module, client, owner, monkeypatch):
    malicious = '</script><script>alert(1)</script>\\"'
    monkeypatch.setattr(module, '_water_points_cache', ({'lat': 48.005, 'lon': 2.005, 'name': malicious},))
    monkeypatch.setattr(module, '_toilets_cache', ())
    uploaded = post(client, '/upload', page_name='map', max_distance='1000', gpx_file=(io.BytesIO(gpx()), 'trace.gpx'))
    assert uploaded.status_code == 202
    result = wait_for(client, uploaded.json['task_id'])
    assert result['error'] is None, result
    page = client.get('/generated/map')
    assert malicious.encode() not in page.data
    assert b'\\u003c/script\\u003e' in page.data
    assert b'L.polyline' in page.data and b'L.GPX' not in page.data
    assert b'1' in page.data and b'1000 m' in page.data
    download = client.get(f'/uploads/{owner.id}/map.gpx')
    assert download.data == gpx()
    assert download.mimetype == 'application/gpx+xml'
    assert download.headers['Content-Disposition'].startswith('attachment;')
    second = module.app.test_client()
    account(module, 'other@example.test')
    login(second, 'other@example.test')
    assert second.get('/status/' + uploaded.json['task_id']).status_code == 404
    assert second.get('/generated/map').status_code == 403
    assert second.get(f'/uploads/{owner.id}/map.gpx').status_code == 403
    assert post(client, '/delete/map').status_code == 302
    assert not Path(module.get_user_page_folder(owner.id), 'map.html').exists()


@pytest.mark.parametrize('data', [{'max_distance':'-1'}, {'max_distance':'nan'}, {'max_distance':'5001'}, {'gpx_url':'x'*4097}])
def test_invalid_upload_fields(client, owner, data):
    response = post(client, '/upload', page_name='test', gpx_file=(io.BytesIO(gpx()), 'trace.gpx'), **data)
    assert response.status_code == 400


def test_upload_extension_and_both_sources(client, owner):
    assert post(client, '/upload', page_name='test', gpx_file=(io.BytesIO(gpx()), 'trace.html')).status_code == 400
    assert post(client, '/upload', page_name='test', gpx_url='https://example.test/trace.gpx', gpx_file=(io.BytesIO(gpx()), 'trace.gpx')).status_code == 400


def test_global_capacity_released_and_unique_names(module, client, owner):
    with patch.object(module.executor, 'submit') as submit:
        first = post(client, '/upload', page_name='same', gpx_file=(io.BytesIO(gpx()), 'trace.gpx'))
        second = post(client, '/upload', page_name='same', gpx_file=(io.BytesIO(gpx()), 'trace.gpx'))
        third = post(client, '/upload', page_name='same', gpx_file=(io.BytesIO(gpx()), 'trace.gpx'))
        assert first.status_code == second.status_code == 202
        assert third.status_code == 503
        names = [call.args[3] for call in submit.call_args_list]
        assert names == ['same', 'same-1']
        assert post(client, '/delete/same').status_code == 409
    for call in submit.call_args_list:
        module.process_gpx_task(*call.args[1:])
    assert module.processing_slots.acquire(blocking=False)
    module.processing_slots.release()


def test_invalid_file_cleanup_and_retry(module, client, owner):
    response = post(client, '/upload', page_name='bad', gpx_file=(io.BytesIO(b'<html/>'), 'trace.gpx'))
    assert wait_for(client, response.json['task_id'])['error']
    assert not Path(module.get_user_upload_folder(owner.id), 'bad.gpx').exists()
    assert module.processing_slots.acquire(blocking=False)
    module.processing_slots.release()


def test_no_default_secret(tmp_path):
    environment = dict(os.environ, SECRET_KEY='', WATERMAP_DATA_DIR=str(tmp_path))
    response = subprocess.run([sys.executable, '-c', 'import app'], capture_output=True, env=environment)
    assert response.returncode != 0
    assert b'SECRET_KEY' in response.stderr


def test_csp_nonce_matches_and_changes(client, owner):
    import re
    first, second = client.get('/'), client.get('/')
    nonce = re.search(rb'<script nonce="([^"]+)"', first.data).group(1).decode()
    assert f"'nonce-{nonce}'" in first.headers['Content-Security-Policy']
    assert nonce.encode() not in second.data
    script_policy = first.headers['Content-Security-Policy'].split('script-src',1)[1].split(';',1)[0]
    assert "'unsafe-inline'" not in script_policy


def test_disk_and_user_quota_release_slots(module, client, owner):
    with patch.object(module.shutil,'disk_usage',return_value=type('Disk',(),{'free':0})()):
        response = post(client,'/upload',page_name='full',gpx_file=(io.BytesIO(gpx()),'track.gpx'))
        assert response.status_code == 503
    folder = Path(module.get_user_upload_folder(owner.id))
    for index in range(200):
        (folder/f'{index}.gpx').touch()
    response = post(client,'/upload',page_name='full',gpx_file=(io.BytesIO(gpx()),'track.gpx'))
    assert response.status_code == 413
    assert module.processing_slots.acquire(blocking=False)
    module.processing_slots.release()


def test_legacy_password_hash_compatibility(module, client):
    from werkzeug.security import generate_password_hash
    user = account(module)
    user.password_hash = generate_password_hash('legacy-password',method='pbkdf2:sha256:600000')
    module.db.session.commit()
    assert login(client,password='legacy-password').status_code == 302


def test_kml_preserves_separate_segments(module, tmp_path):
    output = module.kml_to_gpx(b'<kml><LineString><coordinates>2,48 2.1,48.1</coordinates></LineString><LineString><coordinates>4,50 4.1,50.1</coordinates></LineString></kml>')
    path = tmp_path/'converted.gpx'
    path.write_bytes(output)
    track = module.parse_gpx_track_points(path)
    assert len(track) == 4
    assert track.segment_starts == {0,2}


def test_osrm_failure_is_not_replaced_by_a_fictional_route(module,client,owner):
    with patch.object(module,'fetch_public_url',side_effect=ValueError('upstream failure')):
        response = post(client,'/upload',page_name='route',gpx_url='https://www.google.com/maps/dir/48.0,2.0/48.1,2.1/')
        result = wait_for(client,response.json['task_id'])
    assert result['error']
    assert not Path(module.get_user_page_folder(owner.id),'route.html').exists()


def test_upload_larger_than_multipart_read_buffer(module,client,owner,monkeypatch):
    monkeypatch.setattr(module,'_water_points_cache',())
    monkeypatch.setattr(module,'_toilets_cache',())
    content = gpx().replace(b'<gpx>', b'<gpx><!--'+b'a'*100_000+b'-->')
    result = post(client,'/upload',page_name='large',gpx_file=(io.BytesIO(content),'large.gpx'))
    assert result.status_code == 202,result.data
    assert wait_for(client,result.json['task_id'])['error'] is None


def test_request_size_limit(client,owner,module):
    with patch.dict(module.app.config,MAX_CONTENT_LENGTH=1024):
        result = post(client,'/upload',page_name='large',gpx_file=(io.BytesIO(b'x'*2000),'large.gpx'))
    assert result.status_code == 413
    assert result.is_json


def test_browser_script_syntax(client,owner):
    import re
    import shutil
    if not shutil.which('node'):
        pytest.skip('Node.js is optional for the JavaScript syntax check')
    response = client.get('/')
    script = re.search(rb'<script nonce="[^"]+">(.*?)</script>',response.data,re.S).group(1)
    result = subprocess.run(['node','--check'],input=script,capture_output=True)
    assert result.returncode == 0,result.stderr


def test_generated_page_size_limit_cleans_files(module,client,owner,monkeypatch):
    monkeypatch.setattr(module,'_water_points_cache',())
    monkeypatch.setattr(module,'_toilets_cache',())
    monkeypatch.setattr(module,'MAX_FILE_BYTES',32)
    response = post(client,'/upload',page_name='oversize',gpx_file=(io.BytesIO(gpx()),'track.gpx'))
    result = wait_for(client,response.json['task_id'])
    assert result['error']
    assert not Path(module.get_user_page_folder(owner.id),'oversize.html').exists()
    assert not Path(module.get_user_page_folder(owner.id),'oversize.html.tmp').exists()
    assert not Path(module.get_user_upload_folder(owner.id),'oversize.gpx').exists()
