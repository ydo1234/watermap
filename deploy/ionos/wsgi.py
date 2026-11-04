"""Production settings; only Caddy can reach the application's public upstream."""
import os

from werkzeug.middleware.proxy_fix import ProxyFix

from app import app

app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1)
app.config.update(
    SECRET_KEY=os.environ["SECRET_KEY"],
    SESSION_COOKIE_SECURE=True,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    REMEMBER_COOKIE_SECURE=True,
    REMEMBER_COOKIE_HTTPONLY=True,
    REMEMBER_COOKIE_SAMESITE="Lax",
    TRUSTED_HOSTS=["watermap.dolisi.fr", "localhost", "127.0.0.1"],
    PUBLIC_BASE_URL="https://watermap.dolisi.fr",
)


@app.get("/_health")
def health():
    return {"status": "ok"}
