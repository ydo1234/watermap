"""HTTPS acceptance instance, isolated from the production configuration."""
import os
from werkzeug.middleware.proxy_fix import ProxyFix
from app import app

if os.environ.get('WATERMAP_PUBLIC_URL') != 'https://stag.watermap.dolisi.fr':
    raise RuntimeError('The staging instance requires its dedicated public URL')
if os.environ.get('MAIL_SUPPRESS_SEND') != 'true':
    raise RuntimeError('Staging must suppress outgoing mail')
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1)
app.config.update(
    SESSION_COOKIE_SECURE=True, REMEMBER_COOKIE_SECURE=True,
    SESSION_COOKIE_HTTPONLY=True, REMEMBER_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE='Lax', REMEMBER_COOKIE_SAMESITE='Lax',
    SESSION_COOKIE_NAME='watermap_stag_session',
    REMEMBER_COOKIE_NAME='watermap_stag_remember',
    TRUSTED_HOSTS=['stag.watermap.dolisi.fr', 'localhost', '127.0.0.1'],
    PUBLIC_BASE_URL='https://stag.watermap.dolisi.fr', MAIL_SUPPRESS_SEND=True,
)

@app.get('/_health')
def health():
    return {'status': 'ok', 'instance': 'stag'}
