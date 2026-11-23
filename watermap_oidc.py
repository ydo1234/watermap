"""Connexion OIDC facultative ; les identifiants et fichiers Watermap restent stables."""
import os
import re
import hashlib
import secrets
import threading
import time
from collections import OrderedDict
from urllib.parse import urlencode, urlsplit

from authlib.integrations.flask_client import OAuth
from flask import Blueprint, abort, current_app, redirect, render_template, request, session, url_for
from flask_login import current_user, login_user, logout_user
from joserfc import jwt
from joserfc.jwk import KeySet


def verified_logout(encoded, jwks, issuer, audience):
    claims = jwt.decode(encoded, KeySet.import_key_set(jwks), algorithms=['RS256', 'ES256', 'EdDSA']).claims
    jwt.JWTClaimsRegistry(iss={'essential': True, 'value': issuer}, aud={'essential': True, 'value': audience},
                         iat={'essential': True}, jti={'essential': True}).validate(claims)
    if ('nonce' in claims or claims.get('events') != {'http://schemas.openid.net/event/backchannel-logout': {}}
            or not isinstance(claims.get('jti'), str) or not claims['jti']
            or not any(isinstance(claims.get(k), str) and claims[k] for k in ('sid', 'sub'))
            or not time.time() - 300 <= claims['iat'] <= time.time() + 30):
        raise ValueError('Invalid logout token')
    return claims


def install_oidc(app, db, User, csrf, limiter):
    app.config.update(
        OIDC_ENABLED=os.getenv('OIDC_ENABLED', '').lower() == 'true',
        OIDC_ISSUER=os.getenv('OIDC_ISSUER', ''),
        OIDC_CLIENT_ID=os.getenv('OIDC_CLIENT_ID', ''),
        OIDC_CLIENT_SECRET=os.getenv('OIDC_CLIENT_SECRET', ''),
    )
    if app.config['OIDC_ENABLED']:
        issuer = urlsplit(app.config['OIDC_ISSUER'])
        public = urlsplit(app.config['PUBLIC_BASE_URL'])
        if (issuer.scheme != 'https' or public.scheme != 'https' or not issuer.hostname or not public.hostname
                or issuer.username or issuer.query or issuer.fragment
                or not app.config['OIDC_CLIENT_ID'] or not app.config['OIDC_CLIENT_SECRET']):
            raise RuntimeError('OIDC nécessite des URLs HTTPS et un client configuré.')

    class SsoIdentity(db.Model):
        __tablename__ = 'sso_identity'
        issuer = db.Column(db.String(512), primary_key=True)
        subject = db.Column(db.String(255), primary_key=True)
        user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
        __table_args__ = (db.UniqueConstraint('issuer', 'user_id'),)

    class SsoSession(db.Model):
        __tablename__ = 'sso_session'
        id = db.Column(db.String(64), primary_key=True)
        user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
        subject = db.Column(db.String(255), nullable=False)
        provider_sid = db.Column(db.String(255))
        id_token = db.Column(db.Text, nullable=False)
        expires = db.Column(db.Float, nullable=False)

    class SsoLogout(db.Model):
        __tablename__ = 'sso_logout'
        id = db.Column(db.String(64), primary_key=True)
        expires = db.Column(db.Float, nullable=False)

    oauth = OAuth(app)
    client = oauth.register('rauthy', client_id=app.config['OIDC_CLIENT_ID'], client_secret=app.config['OIDC_CLIENT_SECRET'],
        server_metadata_url=app.config['OIDC_ISSUER'].rstrip('/') + '/.well-known/openid-configuration',
        client_kwargs={'scope': 'openid profile email', 'code_challenge_method': 'S256', 'timeout': 15})
    bp = Blueprint('sso', __name__)
    pending = OrderedDict()
    pending_lock = threading.Lock()

    def enabled():
        if not app.config['OIDC_ENABLED']:
            abort(404)

    def valid_session(user_id):
        row = db.session.get(SsoSession, session.get('oidc_session')) if session.get('oidc_session') else None
        return row if row and row.user_id == user_id and row.expires > time.time() else None

    def finish(user, identity):
        # N'envoyer aucun jeton OIDC au navigateur, même dans un cookie signé.
        db.session.query(SsoSession).filter(SsoSession.expires < time.time()).delete()
        record = SsoSession(id=secrets.token_urlsafe(32), user_id=user.id, subject=identity['sub'],
            provider_sid=identity.get('sid'), id_token=identity['id_token'], expires=time.time() + 43200)
        db.session.add(record)
        db.session.commit()
        return_to = session.get('sso_return_to', url_for('home'))
        session.clear()
        login_user(user)
        session['oidc_session'] = record.id
        session.permanent = True
        return redirect(return_to)

    @bp.get('/auth/login')
    @limiter.limit('20 per minute')
    def login():
        enabled()
        transfer = request.args.get('transfer', '')
        return_to = '/imports/cyclerouter/' + transfer if re.fullmatch(r'[a-f0-9]{64}', transfer) else url_for('home')
        if current_user.is_authenticated:
            return redirect(return_to)
        session.clear()
        session['sso_return_to'] = return_to
        try:
            return client.authorize_redirect(app.config['PUBLIC_BASE_URL'] + '/auth/callback', nonce=secrets.token_urlsafe(32))
        except Exception:
            return render_template('sso_error.html', message='La connexion commune est momentanément indisponible.'), 503

    @bp.get('/auth/callback')
    def callback():
        enabled()
        try:
            token = client.authorize_access_token()
            info = token['userinfo']
            if (info.get('iss') != app.config['OIDC_ISSUER'] or not isinstance(info.get('sub'), str)
                    or not 0 < len(info['sub']) <= 255 or info.get('email_verified') is not True
                    or not isinstance(info.get('email'), str) or not 0 < len(info['email']) <= 100):
                raise ValueError('Unverified identity')
            identity = dict(info, id_token=token['id_token'])
            link = db.session.get(SsoIdentity, (info['iss'], info['sub']))
            if link:
                user = db.session.get(User, link.user_id)
                if not user:
                    raise ValueError('Missing local account')
                return finish(user, identity)
            user = db.session.execute(db.select(User).where(db.func.lower(User.email) == info['email'].strip().lower())).scalars().first()
            if user:
                key = secrets.token_urlsafe(32)
                with pending_lock:
                    pending[key] = (time.time() + 600, identity, user.id)
                    while len(pending) > 128:
                        pending.popitem(last=False)
                return_to = session.get('sso_return_to', url_for('home'))
                session.clear()
                session['pending_sso'] = key
                session['sso_return_to'] = return_to
                return redirect(url_for('sso.link_account'))
            user = User(name=(info.get('name') or info['email'])[:100], email=info['email'].strip().lower())
            user.set_password(secrets.token_urlsafe(64))
            db.session.add(user)
            db.session.flush()
            db.session.add(SsoIdentity(issuer=info['iss'], subject=info['sub'], user_id=user.id))
            return finish(user, identity)
        except Exception:
            db.session.rollback()
            session.clear()
            return render_template('sso_error.html', message='Connexion refusée ou expirée. Vérifiez votre adresse e-mail puis recommencez.'), 400

    @bp.route('/auth/link', methods=['GET', 'POST'])
    @limiter.limit('5 per minute', methods=['POST'])
    def link_account():
        enabled()
        key = session.get('pending_sso')
        with pending_lock:
            item = pending.get(key)
        if not item or item[0] < time.time():
            return render_template('sso_error.html', message='La demande de rattachement a expiré. Recommencez la connexion.'), 400
        _, identity, user_id = item
        user = db.session.get(User, user_id)
        if not user:
            abort(400)
        error = None
        if request.method == 'POST':
            if not user.check_password(request.form.get('password', '')):
                error = 'Le mot de passe Watermap est incorrect.'
            else:
                try:
                    db.session.add(SsoIdentity(issuer=identity['iss'], subject=identity['sub'], user_id=user.id))
                    response = finish(user, identity)
                    with pending_lock:
                        pending.pop(key, None)
                    return response
                except Exception:
                    db.session.rollback()
                    return render_template('sso_error.html', message='Le compte est déjà rattaché. Recommencez la connexion.'), 409
        return render_template('sso_link.html', email=user.email, error=error), 400 if error else 200

    def logout():
        record = valid_session(current_user.id)
        try:
            endpoint = client.load_server_metadata()['end_session_endpoint']
            if urlsplit(endpoint).netloc != urlsplit(app.config['OIDC_ISSUER']).netloc:
                raise ValueError('Invalid logout endpoint')
        except Exception:
            return render_template('sso_error.html', message='La déconnexion commune est indisponible. Réessayez.'), 503
        encoded = record.id_token if record else None
        if record:
            db.session.delete(record)
            db.session.commit()
        logout_user()
        session.clear()
        args = {'post_logout_redirect_uri': app.config['PUBLIC_BASE_URL'] + '/', 'client_id': app.config['OIDC_CLIENT_ID']}
        if encoded:
            args['id_token_hint'] = encoded
        return redirect(endpoint + '?' + urlencode(args))

    @bp.post('/auth/backchannel-logout')
    @csrf.exempt
    @limiter.limit('60 per minute')
    def backchannel_logout():
        enabled()
        try:
            if request.content_length and request.content_length > 16384:
                abort(413)
            claims = verified_logout(request.form['logout_token'], client.fetch_jwk_set(),
                                     app.config['OIDC_ISSUER'], app.config['OIDC_CLIENT_ID'])
            replay = hashlib.sha256(claims['jti'].encode()).hexdigest()
            if db.session.get(SsoLogout, replay):
                return {}, 200
            db.session.query(SsoLogout).filter(SsoLogout.expires < time.time()).delete()
            db.session.add(SsoLogout(id=replay, expires=time.time() + 600))
            query = db.session.query(SsoSession)
            if claims.get('sid'):
                query = query.filter(SsoSession.provider_sid == claims['sid'])
            if claims.get('sub'):
                query = query.filter(SsoSession.subject == claims['sub'])
            query.delete()
            db.session.commit()
            return {}, 200
        except Exception:
            db.session.rollback()
            return {'error': 'Notification de déconnexion invalide.'}, 400

    @app.before_request
    def route_common_identity():
        if not app.config['OIDC_ENABLED']:
            return None
        if request.endpoint in {'login', 'register', 'forgot_password', 'reset_password', 'change_password'}:
            if request.method != 'GET':
                return {'error': 'Utilisez le compte commun pour cette opération.'}, 403
            if request.endpoint == 'change_password':
                return redirect(app.config['OIDC_ISSUER'].rstrip('/') + '/account')
            return redirect(url_for('sso.login'))
        if request.endpoint == 'home' and not current_user.is_authenticated:
            return redirect(url_for('sso.login'))

    app.register_blueprint(bp)
    return {'client': client, 'identities': SsoIdentity, 'sessions': SsoSession,
            'valid_session': valid_session, 'logout': logout, 'pending': pending}
