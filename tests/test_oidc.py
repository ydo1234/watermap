import re
import time
from urllib.parse import parse_qs, urlsplit

import pytest
from joserfc import jwt
from joserfc.jwk import RSAKey

from conftest import account


@pytest.fixture
def sso(module, monkeypatch):
    issuer = 'https://auth.example/auth/v1'
    monkeypatch.setitem(module.app.config, 'OIDC_ENABLED', True)
    monkeypatch.setitem(module.app.config, 'OIDC_ISSUER', issuer)
    monkeypatch.setitem(module.app.config, 'OIDC_CLIENT_ID', 'watermap')
    remote = module.oidc['client']
    remote.client_id = 'watermap'
    key = RSAKey.generate_key(2048, parameters={'kid': 'test'})
    metadata = {'issuer': issuer, 'authorization_endpoint': issuer + '/authorize',
        'token_endpoint': issuer + '/token', 'jwks': {'keys': [key.as_dict(private=False)]},
        'end_session_endpoint': issuer + '/logout', 'id_token_signing_alg_values_supported': ['RS256']}
    monkeypatch.setattr(remote, 'load_server_metadata', lambda: metadata)
    state = {'sub': 'sso-user', 'email': 'new@example.test', 'changes': {}}
    def token(**kwargs):
        assert kwargs['code_verifier']
        claims = {'iss': issuer, 'aud': 'watermap', 'sub': state['sub'], 'email': state['email'],
            'email_verified': True, 'name': 'SSO user', 'nonce': state['nonce'], 'sid': 'remote-session',
            'iat': int(time.time()), 'exp': int(time.time()) + 300, **state['changes']}
        return {'access_token': 'private-token', 'token_type': 'Bearer',
                'id_token': jwt.encode({'alg': 'RS256', 'kid': 'test'}, claims, key)}
    monkeypatch.setattr(remote, 'fetch_access_token', token)
    return state, key, metadata


def callback(client, state):
    response = client.get('/auth/login')
    query = parse_qs(urlsplit(response.location).query)
    assert query['code_challenge_method'] == ['S256']
    state['nonce'] = query['nonce'][0]
    return client.get('/auth/callback?code=valid&state=' + query['state'][0])


def test_new_account_uses_sso_and_preserves_local_auth_when_disabled(module, client, sso):
    state, _, _ = sso
    assert client.get('/').location.endswith('/auth/login')
    response = callback(client, state)
    assert response.status_code == 302 and response.location == '/'
    user = module.db.session.execute(module.db.select(module.User)).scalar_one()
    assert user.email == state['email']
    assert not user.is_admin
    assert client.get('/').status_code == 200
    with client.session_transaction() as session:
        assert 'id_token' not in session
        assert session.get('oidc_session')


def test_existing_account_requires_old_password_and_keeps_id_and_admin(module, client, sso):
    user = account(module, 'owner@example.test')
    module.db.session.add(module.AdminAccess(user_id=user.id))
    module.db.session.commit()
    folder = module.get_user_upload_folder(user.id)
    from pathlib import Path
    Path(folder, 'original.gpx').write_text('unchanged')
    state, _, _ = sso
    state['email'] = user.email
    original_id = user.id
    assert callback(client, state).location == '/auth/link'
    assert module.db.session.query(module.oidc['identities']).count() == 0
    page = client.get('/auth/link')
    csrf = re.search(rb'name="csrf_token" value="([^"]+)"', page.data).group(1).decode()
    response = client.post('/auth/link', data={'csrf_token': csrf, 'password': 'wrong'})
    assert response.status_code == 400
    assert module.db.session.query(module.oidc['identities']).count() == 0
    assert client.post('/auth/link', data={'csrf_token': csrf, 'password': 'valid-password-123'}).status_code == 302
    assert module.db.session.query(module.User).count() == 1
    assert module.db.session.get(module.User, original_id).is_admin
    assert Path(folder, 'original.gpx').read_text() == 'unchanged'
    assert client.get('/').status_code == 200


@pytest.mark.parametrize('changes', [{'aud': 'cycle'}, {'nonce': 'forged'}, {'email_verified': False}])
def test_invalid_tokens_create_no_account(module, client, sso, changes):
    sso[0]['changes'] = changes
    assert callback(client, sso[0]).status_code == 400
    assert module.db.session.query(module.User).count() == 0


def test_legacy_login_cannot_bypass_sso(module, client, sso):
    user = account(module)
    with client.session_transaction() as session:
        session['_user_id'] = user.get_id()
        session['_fresh'] = True
    assert client.get('/').location.endswith('/auth/login')
    assert client.get('/register').location.endswith('/auth/login')


def test_backchannel_logout_revokes_session(module, client, sso):
    state, key, metadata = sso
    assert callback(client, state).status_code == 302
    claims = {'iss': metadata['issuer'], 'aud': 'watermap', 'sub': state['sub'], 'jti': 'logout',
              'iat': int(time.time()), 'events': {'http://schemas.openid.net/event/backchannel-logout': {}}}
    for changes in ({'aud': 'cycle'}, {'nonce': 'not-a-logout-token'}):
        encoded = jwt.encode({'alg': 'RS256', 'kid': 'test'}, {**claims, **changes}, key)
        assert client.post('/auth/backchannel-logout', data={'logout_token': encoded}).status_code == 400
        assert client.get('/').status_code == 200
    encoded = jwt.encode({'alg': 'RS256', 'kid': 'test'}, claims, key)
    assert client.post('/auth/backchannel-logout', data={'logout_token': encoded}).status_code == 200
    assert client.get('/').location.endswith('/auth/login')
