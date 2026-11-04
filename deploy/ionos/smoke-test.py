"""Exercise any release against fresh disposable data, without sending email."""
import io
import os
from pathlib import Path
import re
import secrets
import sys
import tempfile
import time
from unittest.mock import patch

SOURCE = Path('/app') if Path('/app/app.py').exists() else Path(__file__).resolve().parents[2]
sys.path.insert(0, str(SOURCE))
BASE = 'https://watermap.dolisi.fr'


def main():
    with tempfile.TemporaryDirectory(prefix='watermap-smoke-') as directory:
        os.environ['WATERMAP_DATA_DIR'] = directory
        os.environ.setdefault('SECRET_KEY', secrets.token_urlsafe(48))
        os.environ['MAIL_SUPPRESS_SEND'] = 'true'
        import app as module
        if (SOURCE / 'wsgi.py').exists():
            from wsgi import app
        else:
            app = module.app
            app.config.update(SESSION_COOKIE_SECURE=True, PUBLIC_BASE_URL=BASE)
        app.config.update(TESTING=True)
        client = app.test_client()

        def get(path, browser=client):
            return browser.get(path, base_url=BASE)

        def form(path='/', browser=client):
            response = get(path, browser)
            assert response.status_code == 200, (path, response.status_code)
            csrf_token = re.search(rb'name="csrf_token" value="([^"]+)"', response.data).group(1).decode()
            # Follow the document policy as a browser would. Forcing Referer
            # independently of it previously hid HTTPS-only CSRF regressions.
            headers = {} if response.headers.get('Referrer-Policy') == 'no-referrer' else {'Referer': BASE+'/'}
            return csrf_token, headers

        def token(path='/', browser=client):
            return form(path,browser)[0]

        def post(path, data=None, browser=client):
            csrf_token, headers = form(browser=browser)
            return browser.post(path, data={'csrf_token':csrf_token, **(data or {})}, base_url=BASE, headers=headers)

        assert get('/').status_code == 200
        assert get('/generated/absent').status_code == 302
        assert client.post('/login', base_url=BASE, data={}).status_code == 400
        password = secrets.token_urlsafe(20)
        email = 'smoke@example.invalid'
        assert post('/register', {'name':'Smoke', 'email':email, 'password':password}).status_code == 302
        response = post('/login', {'email':email, 'password':password})
        assert response.status_code == 302
        cookie = response.headers.get('Set-Cookie','')
        assert 'Secure' in cookie and 'HttpOnly' in cookie and 'SameSite=Lax' in cookie
        assert len(module.parse_water_points()) > 0 and len(module.parse_toilets()) > 0
        point = module.parse_water_points()[0]
        lat, lon = point['lat'], point['lon']
        gpx = f'<gpx><trk><trkseg><trkpt lat="{lat}" lon="{lon}"/><trkpt lat="{lat+0.0001}" lon="{lon+0.0001}"/></trkseg></trk></gpx>'.encode()
        response = post('/upload', {'page_name':'smoke', 'max_distance':'1000', 'gpx_file':(io.BytesIO(gpx),'trace.gpx')})
        assert response.status_code == 202, response.data
        task = response.json['task_id']
        for _ in range(300):
            state = get('/status/'+task)
            assert state.status_code == 200
            if state.json['done']:
                break
            time.sleep(0.1)
        assert state.json['done'] and not state.json['error'], state.json
        page = get('/generated/smoke')
        assert page.status_code == 200 and b'L.polyline' in page.data
        assert 'no-store' in page.headers['Cache-Control']
        with app.app_context():
            user = module.find_user(email)
            owner_id, reset = user.id, user.get_reset_token()
        assert get(f'/uploads/{owner_id}/smoke.gpx').data == gpx
        other = app.test_client()
        assert post('/register', {'name':'Other','email':'other@example.invalid','password':password}, other).status_code == 302
        assert post('/login', {'email':'other@example.invalid','password':password}, other).status_code == 302
        assert get('/status/'+task,other).status_code == 404
        assert get('/generated/smoke',other).status_code == 403
        assert get(f'/uploads/{owner_id}/smoke.gpx',other).status_code == 403
        with patch.object(module.mail,'send') as send:
            assert post('/forgot-password',{'email':email}).status_code == 200
            module.mail_executor.submit(lambda: None).result(timeout=5)
            send.assert_called_once()
            assert BASE+'/reset-password/' in send.call_args.args[0].body
        reset_path = '/reset-password/'+reset
        reset_csrf, reset_headers = form(reset_path)
        response = client.post(reset_path,base_url=BASE,headers=reset_headers,data={'csrf_token':reset_csrf,'password':'new-valid-password-123'})
        assert response.status_code == 302
        assert get('/change-password').status_code == 302
        with app.app_context():
            assert module.User.verify_reset_token(reset) is None
        assert post('/login',{'email':email,'password':'new-valid-password-123'}).status_code == 302
        assert post('/delete/smoke').status_code == 302
        assert post('/logout').status_code == 302
        module.executor.shutdown(wait=True)
        module.mail_executor.shutdown(wait=True)
        print('PASS: CSRF, cookies HTTPS, comptes, isolation, références, GPX, carte, téléchargement, suppression, courriel simulé et réinitialisation à usage unique. Données réelles inchangées.')


if __name__ == '__main__':
    main()
