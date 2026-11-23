import hashlib
import hmac
import json
from pathlib import Path
import time
from urllib.parse import parse_qs, urlsplit

import pytest

from conftest import csrf
from test_oidc import sso  # noqa: F401 -- réutilisation du fournisseur OIDC de test
from test_security import wait_for

GPX = '<gpx><trk><trkseg><trkpt lat="45.566" lon="5.921"><ele>250</ele></trkpt><trkpt lat="45.57" lon="5.922"><ele>260</ele></trkpt></trkseg></trk></gpx>'
TOKEN = 'a' * 64
SECRET = 'x' * 48
PATH = '/imports/cyclerouter/' + TOKEN


@pytest.fixture
def integration(module, sso, monkeypatch):
    monkeypatch.setitem(module.app.config, 'CYCLEROUTER_TRANSFER_SECRET', SECRET)
    monkeypatch.setattr(module, '_water_points_cache', ({'lat': 45.568, 'lon': 5.9215, 'name': 'Fontaine'},))
    monkeypatch.setattr(module, '_toilets_cache', ())
    return sso[0]


def push(client, state, *, changes=None, timestamp=None, secret=SECRET, raw=None):
    data = {'issuer': 'https://auth.example/auth/v1', 'subject': state['sub'],
            'token': TOKEN, 'name': 'Tour du lac', 'gpx': GPX, **(changes or {})}
    body = raw if raw is not None else json.dumps(data).encode()
    timestamp = str(int(time.time()) if timestamp is None else timestamp)
    signature = hmac.new(secret.encode(), b'POST\n/api/imports/cyclerouter\n' + timestamp.encode() + b'\n' + body,
                         hashlib.sha256).hexdigest()
    return client.post('/api/imports/cyclerouter', data=body, content_type='application/json',
        headers={'X-CycleRouter-Timestamp': timestamp, 'X-CycleRouter-Signature': signature})


def authenticate(client, state, login_path='/auth/login'):
    response = client.get(login_path)
    query = parse_qs(urlsplit(response.location).query)
    state['nonce'] = query['nonce'][0]
    return client.get('/auth/callback?code=valid&state=' + query['state'][0])


def test_disabled_by_default(module, client):
    assert push(client, {'sub': 'alice'}).status_code == 404
    assert client.get(PATH).status_code == 404


def test_signature_freshness_issuer_and_gpx_are_checked(module, client, integration):
    assert push(client, integration, secret='wrong').status_code == 401
    assert push(client, integration, timestamp=int(time.time()) - 120).status_code == 401
    assert push(client, integration, changes={'issuer': 'https://prod.example'}).status_code == 400
    assert push(client, integration, raw=b'[]').status_code == 400
    assert push(client, integration, changes={'gpx': '<html/>'}).status_code == 422
    assert push(client, integration, changes={'gpx': '<!DOCTYPE gpx [<!ENTITY x "boom">]><gpx>&x;</gpx>'}).status_code == 422
    assert module.db.session.query(module.CycleTransfer).count() == 0


def test_import_preserves_trace_and_returns_through_sso_without_duplicates(module, client, integration):
    assert push(client, integration).status_code == 201
    assert push(client, integration).status_code == 200
    assert module.db.session.query(module.CycleTransfer).count() == 1
    response = client.get(PATH)
    assert response.status_code == 302 and 'transfer=' + TOKEN in response.location
    assert authenticate(client, integration, response.location).location == PATH
    assert client.get(PATH).status_code == 200
    assert client.post(PATH, data={'page_name': 'Tour'}).status_code == 400
    token = csrf(client, PATH)
    response = client.post(PATH, data={'csrf_token': token, 'page_name': 'Tour', 'max_distance': '500'})
    assert response.status_code == 202
    task = response.json['task_id']
    result = wait_for(client, task)
    assert not result['error'], result
    user = module.db.session.execute(module.db.select(module.User)).scalar_one()
    assert Path(module.get_user_upload_folder(user.id), 'Tour.gpx').read_bytes() == GPX.encode()
    assert 'Fontaine' in client.get(result['page_url']).text
    repeat = client.post(PATH, data={'csrf_token': csrf(client, '/'), 'page_name': 'Duplicate'})
    assert repeat.status_code == 200 and repeat.json['page_url'] == result['page_url']
    assert client.get(PATH).location == result['page_url']
    assert not Path(module.get_user_upload_folder(user.id), 'Duplicate.gpx').exists()


def test_transfer_is_private_and_expires(module, client, integration):
    assert push(client, integration).status_code == 201
    other = module.app.test_client()
    original = integration['sub'], integration['email']
    integration.update(sub='other-user', email='other@example.test')
    assert authenticate(other, integration).status_code == 302
    assert other.get(PATH).status_code == 403
    assert other.post(PATH, data={'csrf_token': csrf(other, '/'), 'page_name': 'stolen'}).status_code == 403
    integration['sub'], integration['email'] = original
    assert authenticate(client, integration).status_code == 302
    row = module.db.session.query(module.CycleTransfer).one()
    row.expires = time.time() - 1
    module.db.session.commit()
    assert client.get(PATH).status_code == 410


def test_pending_quota_and_invalid_import_leave_transfer_available(module, client, integration):
    for char in 'abcd':
        assert push(client, integration, changes={'token': char * 64}).status_code == 201
    assert push(client, integration, changes={'token': 'e' * 64}).status_code == 429
    authenticate(client, integration)
    response = client.post(PATH, data={'csrf_token': csrf(client, PATH), 'page_name': 'Tour', 'max_distance': '-1'})
    assert response.status_code == 400
    assert module.db.session.get(module.CycleTransfer, hashlib.sha256(TOKEN.encode()).hexdigest()).task_id is None


def test_linking_existing_account_keeps_transfer_destination(module, client, integration):
    from conftest import account
    account(module, integration['email'])
    assert push(client, integration).status_code == 201
    response = authenticate(client, integration, client.get(PATH).location)
    assert response.location == '/auth/link'
    token = csrf(client, '/auth/link')
    response = client.post('/auth/link', data={'csrf_token': token, 'password': 'valid-password-123'})
    assert response.location == PATH
    assert client.get(PATH).status_code == 200
    assert module.db.session.query(module.User).count() == 1


def test_return_destination_cannot_be_an_external_url(module, client, integration):
    response = authenticate(client, integration, '/auth/login?transfer=https://evil.example')
    assert response.location == '/'
