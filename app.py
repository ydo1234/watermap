import hashlib
from io import BytesIO
import hmac
import urllib.parse
import html
import json
import math
import os
import re
import secrets
import shutil
import threading
import time
import uuid
# XML generation only. All input parsing below uses defusedxml.
import xml.etree.ElementTree as ET  # nosec B405
from datetime import datetime, timedelta, timezone
from functools import wraps
import click
from concurrent.futures import ThreadPoolExecutor
from defusedxml import ElementTree as SafeET
from flask import Flask, request, redirect, url_for, render_template, send_from_directory, jsonify, session, flash, g, abort, send_file
from werkzeug.utils import secure_filename
from werkzeug.security import generate_password_hash, check_password_hash
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, UserMixin, login_user, logout_user, login_required, current_user
from itsdangerous import URLSafeTimedSerializer as Serializer, BadSignature
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from flask_mail import Mail, Message
from flask_wtf.csrf import CSRFProtect
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from remote_fetch import fetch_public_url
from gpx_utils import coordinate, parse_track, MAX_FILE_BYTES, MAX_TRACK_POINTS
from spatial import RouteIndex, PointSet, vector, EARTH_RADIUS
from gpx_export import enriched_gpx, map_points, MAX_MAP_BYTES

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
DATA_DIR = os.environ.get('WATERMAP_DATA_DIR', BASE_DIR)
DB_FOLDER = os.path.join(DATA_DIR, 'db')
UPLOAD_FOLDER = os.path.join(DATA_DIR, 'uploads')
PAGE_FOLDER = os.path.join(DATA_DIR, 'pages')
WATER_GPX = os.path.join(BASE_DIR, 'water.gpx')
TOILETS_GPX = os.path.join(BASE_DIR, 'toilets_all.osm.gpx')
ALLOWED_EXTENSIONS = {'gpx'}

os.makedirs(DB_FOLDER, exist_ok=True)
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(PAGE_FOLDER, exist_ok=True)

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = MAX_FILE_BYTES
# Werkzeug reads multipart bodies in 64 KiB blocks, including file parts.
app.config['MAX_FORM_MEMORY_SIZE'] = 128 * 1024
app.config['MAX_FORM_PARTS'] = 12
secret_key = os.environ.get('SECRET_KEY', '')
if len(secret_key) < 32 or secret_key in ('dev-secret-key-change-me', 'replace-with-a-long-random-secret'):
    raise RuntimeError('SECRET_KEY doit contenir au moins 32 caractères aléatoires.')
app.config['SECRET_KEY'] = secret_key
app.config.update(
    SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE='Lax',
    SESSION_COOKIE_SECURE=os.environ.get('WATERMAP_COOKIE_SECURE', '').lower() == 'true',
    PERMANENT_SESSION_LIFETIME=timedelta(hours=12),
    PUBLIC_BASE_URL=os.environ.get('WATERMAP_PUBLIC_URL', 'http://localhost:8081').rstrip('/'),
)
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///' + os.path.join(DB_FOLDER, 'users.db')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

# Configuration Email
app.config['MAIL_SERVER'] = os.environ.get('MAIL_SERVER', 'smtp.gmail.com')
app.config['MAIL_PORT'] = int(os.environ.get('MAIL_PORT', 587))
app.config['MAIL_USE_TLS'] = os.environ.get('MAIL_USE_TLS', 'True').lower() in ['true', 'on', '1']
app.config['MAIL_USERNAME'] = os.environ.get('MAIL_USERNAME')
app.config['MAIL_PASSWORD'] = os.environ.get('MAIL_PASSWORD')
app.config['MAIL_DEFAULT_SENDER'] = os.environ.get('MAIL_DEFAULT_SENDER', app.config['MAIL_USERNAME'])
app.config['MAIL_TIMEOUT'] = 15
app.config['MAIL_SUPPRESS_SEND'] = os.environ.get('MAIL_SUPPRESS_SEND', '').lower() == 'true'

csrf = CSRFProtect(app)
limiter = Limiter(get_remote_address, app=app, storage_uri='memory://')

db = SQLAlchemy(app)
mail = Mail(app)
login_manager = LoginManager(app)
login_manager.login_view = 'login'
login_manager.login_message = "Veuillez vous connecter pour accéder à cette page."
dummy_password_hash = generate_password_hash('watermap-invalid-login-placeholder')
mail_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='watermap-mail')
mail_slots = threading.BoundedSemaphore(10)

@app.context_processor
def inject_env():
    return {'is_dev': os.environ.get('FLASK_ENV') == 'development', 'csp_nonce': csp_nonce}


def csp_nonce():
    if 'csp_nonce' not in g:
        g.csp_nonce = secrets.token_urlsafe(24)
    return g.csp_nonce

# Modèle Utilisateur
class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100))
    email = db.Column(db.String(100), unique=True, nullable=False)
    password_hash = db.Column(db.String(200), nullable=False)

    @property
    def is_admin(self):
        return db.session.get(AdminAccess, self.id) is not None

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return isinstance(password, str) and 0 < len(password) <= 512 and check_password_hash(self.password_hash, password)

    def password_version(self):
        return hashlib.sha256(self.password_hash.encode()).hexdigest()

    def get_id(self):
        # Password changes invalidate existing sessions without a schema migration.
        return f'{self.id}:{self.password_version()}'

    def get_reset_token(self):
        s = Serializer(app.config['SECRET_KEY'], salt='watermap-password-reset-v1')
        return s.dumps({'user_id': self.id, 'version': self.password_version()})

    @staticmethod
    def verify_reset_token(token, expires_sec=1800):
        s = Serializer(app.config['SECRET_KEY'], salt='watermap-password-reset-v1')
        try:
            data = s.loads(token, max_age=expires_sec)
            user_id = data['user_id']
            if type(user_id) is not int:
                return None
            user = db.session.get(User, user_id)
            if user and hmac.compare_digest(data['version'], user.password_version()):
                return user
        except (BadSignature, KeyError, TypeError, ValueError):
            return None
        return None

class AdminAccess(db.Model):
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), primary_key=True)


class Announcement(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(160), nullable=False)
    body = db.Column(db.Text, nullable=False)
    kind = db.Column(db.String(20), nullable=False, default='message')
    scheduled_for = db.Column(db.String(16), nullable=False, default='')
    duration = db.Column(db.String(80), nullable=False, default='')
    created_at = db.Column(db.DateTime, nullable=False, default=lambda: datetime.now(timezone.utc))
    created_by = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    published = db.Column(db.Boolean, nullable=False, default=False)
    locked = db.Column(db.Boolean, nullable=False, default=False)
    mail_prepared = db.Column(db.Boolean, nullable=False, default=False)
    mail_simulated = db.Column(db.Boolean, nullable=False, default=False)


class AnnouncementDelivery(db.Model):
    announcement_id = db.Column(db.Integer, db.ForeignKey('announcement.id'), primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), primary_key=True)
    email = db.Column(db.String(100), nullable=False)
    name = db.Column(db.String(100), nullable=False)
    status = db.Column(db.String(20), nullable=False, default='pending')


def admin_required(view):
    @wraps(view)
    @login_required
    def wrapped(*args, **kwargs):
        if not current_user.is_admin:
            abort(403)
        return view(*args, **kwargs)
    return wrapped


@app.cli.command('admin-user')
@click.argument('email')
@click.option('--revoke', is_flag=True, help='Retirer les droits administrateur.')
def admin_user(email, revoke):
    """Accorder les droits administrateur à un compte existant."""
    user = find_user(email)
    if user is None:
        raise click.ClickException('Aucun compte ne correspond à cette adresse.')
    access = db.session.get(AdminAccess, user.id)
    if revoke and access:
        db.session.delete(access)
    elif not revoke and access is None:
        db.session.add(AdminAccess(user_id=user.id))
    db.session.commit()
    click.echo('Droits administrateur retirés.' if revoke else 'Droits administrateur accordés.')


@login_manager.user_loader
def load_user(user_id):
    try:
        identifier, version = user_id.split(':', 1)
        user = db.session.get(User, int(identifier))
        return user if user and hmac.compare_digest(version, user.password_version()) else None
    except (TypeError, ValueError, AttributeError):
        return None

with app.app_context():
    db.create_all()
    # SQLite expression indexes are not reflected by SQLAlchemy's `checkfirst`.
    with db.engine.begin() as connection:
        connection.exec_driver_sql('CREATE INDEX IF NOT EXISTS ix_user_email_lower ON user (lower(email))')

def get_user_upload_folder(user_id):
    path = os.path.join(UPLOAD_FOLDER, str(user_id))
    os.makedirs(path, exist_ok=True)
    return path

def get_user_page_folder(user_id):
    path = os.path.join(PAGE_FOLDER, str(user_id))
    os.makedirs(path, exist_ok=True)
    return path

def send_reset_email(user):
    token = user.get_reset_token()
    msg = Message('Réinitialisation de votre mot de passe - WaterMap',
                  recipients=[user.email])
    msg.body = f'''Pour réinitialiser votre mot de passe, visitez le lien suivant :
{app.config['PUBLIC_BASE_URL']}{url_for('reset_password', token=token)}

Si vous n'avez pas fait cette demande, ignorez simplement cet email.
'''
    if not mail_slots.acquire(blocking=False):
        app.logger.warning('File de courriels de réinitialisation pleine.')
        return False
    def deliver():
        try:
            with app.app_context():
                mail.send(msg)
        except Exception:
            app.logger.error('Échec de l’envoi du courriel de réinitialisation.')
        finally:
            mail_slots.release()
    try:
        mail_executor.submit(deliver)
    except RuntimeError:
        mail_slots.release()
        return False
    return True


def find_user(email):
    return db.session.execute(db.select(User).where(func.lower(User.email) == email.strip().lower())).scalars().first()


def valid_password(password):
    return isinstance(password, str) and 8 <= len(password) <= 512


@app.route('/admin')
@admin_required
def admin():
    announcements = db.session.execute(db.select(Announcement).order_by(Announcement.id.desc()).limit(100)).scalars().all()
    return render_template('admin.html', announcements=announcements)


@app.route('/admin/announcements/new', methods=['GET', 'POST'])
@app.route('/admin/announcements/<int:identifier>/edit', methods=['GET', 'POST'])
@admin_required
def announcement_new(identifier=None):
    announcement = db.get_or_404(Announcement, identifier) if identifier else None
    if announcement and announcement.locked:
        abort(409, 'Une annonce déjà diffusée ne peut plus être modifiée. Créez un nouveau message.')
    values = {'title': '', 'body': '', 'kind': 'message', 'scheduled_for': '', 'duration': ''}
    if announcement:
        values = {key: getattr(announcement, key) for key in values}
    error = None
    if request.method == 'POST':
        values = {key: request.form.get(key, '').strip() for key in values}
        if values['kind'] not in ('message', 'downtime', 'update'):
            error = 'Choisissez un type de message valide.'
        elif ('\r' in values['title'] or '\n' in values['title'] or
              len(values['title']) > 160 or len(values['body']) > 5000 or len(values['duration']) > 80):
            error = 'Limites : titre de 160 caractères, message de 5 000 caractères, durée de 80 caractères.'
        elif request.form.get('action') == 'template':
            if values['kind'] == 'update':
                try:
                    planned = datetime.strptime(values['scheduled_for'], '%Y-%m-%dT%H:%M')
                    if not values['duration']:
                        raise ValueError
                    values['title'] = values['title'] or 'Mise à jour programmée de Watermap'
                    values['body'] = (
                        f'Une mise à jour de Watermap est prévue le {planned:%d/%m/%Y} à {planned:%H:%M} '
                        f'(heure de Paris), pour une durée estimée de {values["duration"]}. '
                        'Le service pourra être temporairement indisponible pendant cette intervention. '
                        'Merci de votre compréhension.'
                    )
                except ValueError:
                    error = 'Renseignez la date, l’heure et la durée estimée de la mise à jour.'
            elif values['kind'] == 'downtime':
                values['title'] = values['title'] or 'Indisponibilité de Watermap'
                values['body'] = 'Watermap est temporairement indisponible. Nous vous informerons du rétablissement du service. Merci de votre compréhension.'
            else:
                error = 'Choisissez « Indisponibilité » ou « Mise à jour programmée » pour remplir le modèle.'
        elif not values['title'] or not values['body']:
            error = 'Renseignez le titre et le message.'
        else:
            if values['scheduled_for']:
                try:
                    datetime.strptime(values['scheduled_for'], '%Y-%m-%dT%H:%M')
                except ValueError:
                    error = 'La date et l’heure sont invalides.'
            if not error:
                if announcement:
                    updated = db.session.execute(db.update(Announcement).where(
                        Announcement.id == identifier, Announcement.locked.is_(False)
                    ).values(**values))
                    if updated.rowcount != 1:
                        db.session.rollback()
                        abort(409)
                else:
                    announcement = Announcement(**values, created_by=current_user.id)
                    db.session.add(announcement)
                db.session.commit()
                flash('Brouillon enregistré. Vérifiez le message avant sa diffusion.', 'success')
                return redirect(url_for('announcement_detail', identifier=announcement.id))
    return render_template('announcement_form.html', values=values, error=error)


@app.route('/admin/announcements/<int:identifier>')
@admin_required
def announcement_detail(identifier):
    announcement = db.get_or_404(Announcement, identifier)
    deliveries = db.session.execute(db.select(AnnouncementDelivery).where(
        AnnouncementDelivery.announcement_id == identifier
    ).order_by(AnnouncementDelivery.user_id)).scalars().all()
    return render_template('announcement_detail.html', announcement=announcement, deliveries=deliveries,
                           recipient_count=db.session.query(User).count(),
                           mail_suppressed=app.config['MAIL_SUPPRESS_SEND'])


@app.route('/admin/announcements/<int:identifier>/publish', methods=['POST'])
@admin_required
def announcement_publish(identifier):
    db.get_or_404(Announcement, identifier)
    db.session.execute(db.update(Announcement).where(Announcement.id == identifier).values(published=True, locked=True))
    db.session.commit()
    flash('Annonce publiée dans l’application.', 'success')
    return redirect(url_for('announcement_detail', identifier=identifier))


@app.route('/admin/announcements/<int:identifier>/hide', methods=['POST'])
@admin_required
def announcement_hide(identifier):
    announcement = db.get_or_404(Announcement, identifier)
    announcement.published = False
    db.session.commit()
    flash('Annonce retirée de l’application.', 'success')
    return redirect(url_for('announcement_detail', identifier=identifier))


def deliver_announcement(identifier):
    """Claim each recipient before SMTP; never retry an uncertain delivery."""
    with app.app_context():
        try:
            announcement = db.session.get(Announcement, identifier)
            if announcement.mail_simulated != app.config['MAIL_SUPPRESS_SEND']:
                app.logger.error('Le mode de courriel de l’annonce %s a changé ; envoi suspendu.', identifier)
                return
            recipients = db.session.execute(db.select(AnnouncementDelivery.user_id).where(
                AnnouncementDelivery.announcement_id == identifier,
                AnnouncementDelivery.status == 'pending'
            ).order_by(AnnouncementDelivery.user_id)).scalars().all()
            for user_id in recipients:
                claimed = db.session.execute(db.update(AnnouncementDelivery).where(
                    AnnouncementDelivery.announcement_id == identifier,
                    AnnouncementDelivery.user_id == user_id,
                    AnnouncementDelivery.status == 'pending'
                ).values(status='sending'))
                db.session.commit()
                if claimed.rowcount != 1:
                    continue
                delivery = db.session.get(AnnouncementDelivery, (identifier, user_id))
                try:
                    message = Message('Watermap — ' + announcement.title, recipients=[delivery.email],
                                      sender=app.config.get('MAIL_DEFAULT_SENDER') or app.config.get('MAIL_USERNAME')
                                      or ('watermap@example.invalid' if announcement.mail_simulated else None))
                    message.body = (f'Bonjour {delivery.name},\n\n{announcement.body}\n\n'
                                    f'{app.config["PUBLIC_BASE_URL"]}\n')
                    message.msgId = f'<watermap-announcement-{identifier}-{user_id}@watermap.dolisi.fr>'
                    if not announcement.mail_simulated:
                        mail.send(message)
                except Exception:
                    delivery.status = 'uncertain'
                    db.session.commit()
                    app.logger.error('Envoi d’annonce %s interrompu ; résultat SMTP à vérifier.', identifier)
                    break
                delivery.status = 'simulated' if app.config['MAIL_SUPPRESS_SEND'] else 'sent'
                db.session.commit()
        except Exception:
            db.session.rollback()
            app.logger.exception('Traitement du courriel d’annonce %s interrompu.', identifier)
        finally:
            mail_slots.release()


@app.route('/admin/announcements/<int:identifier>/send', methods=['POST'])
@admin_required
@limiter.limit('5 per minute')
def announcement_send(identifier):
    db.get_or_404(Announcement, identifier)
    if request.form.get('confirm') != 'yes':
        abort(400, 'Confirmez l’envoi aux utilisateurs de cette instance.')
    if not app.config['MAIL_SUPPRESS_SEND'] and not (app.config.get('MAIL_DEFAULT_SENDER') or app.config.get('MAIL_USERNAME')):
        flash('Configurez l’expéditeur SMTP avant l’envoi.', 'danger')
        return redirect(url_for('announcement_detail', identifier=identifier))
    if not mail_slots.acquire(blocking=False):
        abort(503, 'La file de courriels est pleine. Réessayez plus tard.')
    try:
        # Lock content before snapshotting the local instance's recipients.
        db.session.execute(db.update(Announcement).where(Announcement.id == identifier).values(locked=True))
        announcement = db.session.get(Announcement, identifier, populate_existing=True)
        if announcement.mail_prepared and announcement.mail_simulated != app.config['MAIL_SUPPRESS_SEND']:
            abort(409, 'Le mode de courriel a changé. Créez une nouvelle annonce pour ce mode.')
        if not announcement.mail_prepared:
            for user in db.session.execute(db.select(User).order_by(User.id)).scalars():
                db.session.add(AnnouncementDelivery(announcement_id=identifier, user_id=user.id,
                                                    email=user.email, name=user.name or ''))
            announcement.mail_prepared = True
            announcement.mail_simulated = app.config['MAIL_SUPPRESS_SEND']
        db.session.commit()
        mail_executor.submit(deliver_announcement, identifier)
    except Exception:
        db.session.rollback()
        mail_slots.release()
        raise
    flash('Envoi demandé. Consultez le suivi ci-dessous ; les envois déjà tentés ne sont pas répétés.', 'success')
    return redirect(url_for('announcement_detail', identifier=identifier))


@app.context_processor
def inject_announcements():
    return {'announcements': db.session.execute(
        db.select(Announcement).where(Announcement.published.is_(True)).order_by(Announcement.id.desc())
    ).scalars().all()}


@app.after_request
def security_headers(response):
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['X-Frame-Options'] = 'SAMEORIGIN'
    # HTTPS CSRF checks require a same-origin Referer. Send only the origin
    # on forms so recovery tokens and private paths remain undisclosed.
    response.headers['Referrer-Policy'] = (
        'strict-origin-when-cross-origin'
        if request.endpoint == 'page' and response.status_code == 200 else 'strict-origin'
    )
    response.headers['Cache-Control'] = 'private, no-store'
    response.headers['Content-Security-Policy'] = (
        "default-src 'self'; base-uri 'none'; object-src 'none'; frame-ancestors 'self'; "
        f"form-action 'self'; script-src 'self' 'nonce-{csp_nonce()}' https://unpkg.com https://cdnjs.cloudflare.com; "
        "style-src 'self' 'unsafe-inline' https://unpkg.com; "
        "img-src 'self' data: https://tile.openstreetmap.org https://*.tile.openstreetmap.org https://cdnjs.cloudflare.com https://unpkg.com; "
        "connect-src 'self'"
    )
    return response


@app.errorhandler(413)
def oversized_request(error):
    return jsonify(error='Le fichier ou le formulaire dépasse la taille maximale autorisée (20 Mo).'), 413


@app.errorhandler(429)
def rate_limited(error):
    return jsonify(error='Trop de demandes. Réessayez dans quelques instants.'), 429

# --- Routes d'Authentification ---

@app.route('/register', methods=['GET', 'POST'])
@limiter.limit('10 per minute', methods=['POST'])
def register():
    if current_user.is_authenticated:
        return redirect(url_for('home'))
    if request.method == 'POST':
        name = request.form.get('name', '').strip()
        email = request.form.get('email', '').strip().lower()
        password = request.form.get('password', '')

        if not name or len(name) > 100 or len(email) > 100 or not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+', email) or not valid_password(password):
            flash('Renseignez un nom, un email valide et un mot de passe d’au moins 8 caractères.', 'danger')
            return redirect(url_for('register'))
        
        if find_user(email):
            flash('Cet email est déjà utilisé.', 'danger')
            return redirect(url_for('register'))
        
        user = User(name=name, email=email)
        user.set_password(password)
        db.session.add(user)
        try:
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
            flash('Cet email est déjà utilisé.', 'danger')
            return redirect(url_for('register'))
        flash('Inscription réussie ! Vous pouvez vous connecter.', 'success')
        return redirect(url_for('login'))
    return render_template('register.html')

@app.route('/login', methods=['GET', 'POST'])
@limiter.limit('10 per minute', methods=['POST'])
def login():
    if current_user.is_authenticated:
        return redirect(url_for('home'))
    if request.method == 'POST':
        email = request.form.get('email', '')[:101]
        password = request.form.get('password', '')
        user = find_user(email)
        authenticated = user.check_password(password) if user else (
            check_password_hash(dummy_password_hash, password) if 0 < len(password) <= 512 else False
        )
        if user and authenticated:
            session.clear()
            login_user(user)
            session.permanent = True
            return redirect(url_for('home'))
        else:
            flash('Email ou mot de passe incorrect.', 'danger')
    return render_template('login.html')

@app.route('/forgot-password', methods=['GET', 'POST'])
@limiter.limit('5 per minute', methods=['POST'])
def forgot_password():
    if request.method == 'POST':
        email = request.form.get('email', '')[:101]
        user = find_user(email)
        if user:
            send_reset_email(user)
        flash('Si un compte correspond à cette adresse, un lien de réinitialisation vous sera envoyé.', 'info')
    return render_template('forgot_password.html')

@app.route('/reset-password/<token>', methods=['GET', 'POST'])
@limiter.limit('10 per minute')
def reset_password(token):
    user = User.verify_reset_token(token)
    if not user:
        flash('Lien invalide ou expiré.', 'danger')
        return redirect(url_for('forgot_password'))
    
    if request.method == 'POST':
        password = request.form.get('password')
        if not valid_password(password):
            flash('Choisissez un mot de passe d’au moins 8 caractères.', 'danger')
            return render_template('reset_password.html')
        old_hash = user.password_hash
        new_hash = generate_password_hash(password)
        updated = db.session.execute(db.update(User).where(User.id == user.id, User.password_hash == old_hash).values(password_hash=new_hash).execution_options(synchronize_session=False))
        if updated.rowcount != 1:
            db.session.rollback()
            flash('Lien invalide ou déjà utilisé.', 'danger')
            return redirect(url_for('forgot_password'))
        db.session.commit()
        db.session.expire_all()
        flash('Mot de passe mis à jour !', 'success')
        return redirect(url_for('login'))
    return render_template('reset_password.html')

@app.route('/change-password', methods=['GET', 'POST'])
@login_required
@limiter.limit('5 per minute', methods=['POST'])
def change_password():
    if request.method == 'POST':
        old_password = request.form.get('old_password')
        new_password = request.form.get('new_password')
        if not valid_password(new_password):
            flash('Choisissez un mot de passe de 8 à 512 caractères.', 'danger')
        elif current_user.check_password(old_password):
            current_user.set_password(new_password)
            db.session.commit()
            login_user(current_user._get_current_object())
            flash('Mot de passe modifié.', 'success')
            return redirect(url_for('home'))
        else:
            flash('Ancien mot de passe incorrect.', 'danger')
    return render_template('change_password.html')

# Reference files are trusted, but are parsed without XML entities as well.
def load_reference(path, label):
    points = []
    if not os.path.exists(path):
        raise RuntimeError(f'Fichier de référence manquant : {path}')
    class ReferenceReader:
        # Historical OSM exports contain bare ampersands. Repair trusted reference
        # lines while streaming; uploaded GPX is always parsed strictly.
        def __init__(self, source):
            self.source = source

        def read(self, size):
            chunks, length = [], 0
            while length < size:
                line = self.source.readline()
                if not line:
                    break
                line = re.sub(r'&(?!([A-Za-z]+|#\d+|#x[0-9A-Fa-f]+);)', '&amp;', line)
                chunks.append(line)
                length += len(line)
            return ''.join(chunks)

    with open(path, encoding='utf-8', errors='replace') as source:
        for _, element in SafeET.iterparse(ReferenceReader(source), events=('end',), forbid_dtd=True):
            if element.tag.rsplit('}', 1)[-1] != 'wpt':
                continue
            try:
                lat, lon = coordinate(element.attrib['lat'], element.attrib['lon'])
            except (KeyError, ValueError, TypeError):
                element.clear()
                continue
            name = next((child.text for child in element if child.tag.rsplit('}', 1)[-1] in ('name', 'meta_name_com') and child.text), None)
            points.append({'lat': lat, 'lon': lon, 'name': name or f'{label} {lat:.5f},{lon:.5f}'})
            element.clear()
    return PointSet(points)


_water_points_cache = load_reference(WATER_GPX, "Point d'eau")
_toilets_cache = load_reference(TOILETS_GPX, 'Toilettes')
processing_status = {}
task_owners = {}
status_lock = threading.RLock()
file_lock = threading.Lock()
TASK_LIMIT = 2
TASK_HISTORY_LIMIT = 200
TASK_TTL_SECONDS = 3600
executor = ThreadPoolExecutor(max_workers=TASK_LIMIT, thread_name_prefix='watermap-gpx')
processing_slots = threading.BoundedSemaphore(TASK_LIMIT)
active_names = set()


def set_status(task_id, progress, message, done=False, page_url=None, error=None):
    with status_lock:
        processing_status[task_id] = {
            'progress': progress,
            'message': message,
            'done': done,
            'page_url': page_url,
            'error': error,
            '_updated': time.monotonic(),
        }


def get_status(task_id):
    with status_lock:
        result = processing_status.get(task_id)
        return {key: value for key, value in result.items() if not key.startswith('_')} if result else None


def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS


def parse_gpx_track_points(gpx_path):
    return parse_track(gpx_path)


def parse_water_points():
    return _water_points_cache


def parse_toilets():
    return _toilets_cache


def nearby_points(points, index, max_distance_m, task_id=None):
    nearby = []
    if index.root is None:
        return nearby
    chord = 2.0 * math.sin(max_distance_m / (2.0 * EARTH_RADIUS)) + 1e-14
    bounds = index.root[0]
    (xlo, xhi), (ylo, yhi), (zlo, zhi) = [(low-chord, high+chord) for low, high in bounds]
    positions = points.vectors if isinstance(points, PointSet) else (vector(p['lat'], p['lon']) for p in points)
    for point, position in zip(points, positions):
        x, y, z = position
        if not (xlo <= x <= xhi and ylo <= y <= yhi and zlo <= z <= zhi):
            continue
        distance = index.distance_vector(position, max_distance_m)
        if distance <= max_distance_m:
            nearby.append({**point, 'distance_m': int(distance)})
    return nearby


def nearby_water_points(track_points, max_distance_m=500, task_id=None, index=None):
    return nearby_points(parse_water_points(), index or RouteIndex(track_points), max_distance_m, task_id)


def nearby_toilets(track_points, max_distance_m=500, task_id=None, index=None):
    return nearby_points(parse_toilets(), index or RouteIndex(track_points), max_distance_m, task_id)


def kml_to_gpx(kml_content):
    raw_xml = kml_content.decode('utf-8', errors='strict')
    fixed_xml = re.sub(r'&(?!([A-Za-z]+|#\d+|#x[0-9A-Fa-f]+);)', '&amp;', raw_xml)
    root = SafeET.fromstring(fixed_xml, forbid_dtd=True)
    gpx_root = ET.Element('gpx', version='1.1', creator='WaterMap', xmlns='http://www.topografix.com/GPX/1/1')
    trk = ET.SubElement(gpx_root, 'trk')
    total = 0
    for element in root.iter():
        if element.tag.rsplit('}', 1)[-1] != 'coordinates' or not element.text:
            continue
        trkseg = ET.SubElement(trk, 'trkseg')
        for chunk in element.text.split():
            parts = chunk.split(',')
            if len(parts) < 2:
                raise ValueError('Coordonnées KML invalides.')
            lat, lon = coordinate(parts[1], parts[0])
            total += 1
            if total > MAX_TRACK_POINTS:
                raise ValueError('Le KML dépasse la limite de 100 000 points.')
            ET.SubElement(trkseg, 'trkpt', lat=f'{lat:.7f}', lon=f'{lon:.7f}')
    if not total:
        raise ValueError('Aucun point trouvé dans le KML.')
    return ET.tostring(gpx_root, encoding='utf-8')


def process_gpx_task(task_id, upload_path, page_name, original_name, user_id, gpx_url=None, max_distance_m=500):
    try:
        user_page_folder = get_user_page_folder(user_id)
        if gpx_url:
            set_status(task_id, 5, 'Récupération du tracé distant...')
            target_url = gpx_url
            
            # 1. Résolution des liens raccourcis
            if urllib.parse.urlsplit(target_url).hostname in ('maps.app.goo.gl', 'goo.gl'):
                resp = fetch_public_url(target_url, method='HEAD', timeout=10)
                target_url = resp.url
            
            # 2. Gestion de la page de consentement Google
            if urllib.parse.urlsplit(target_url).hostname == 'consent.google.com':
                continue_match = re.search(r'continue=([^&]+)', target_url)
                if continue_match:
                    target_url = urllib.parse.unquote(continue_match.group(1))
            
            # 3. Cas spécial : Google Maps Directions (Itinéraires)
            target = urllib.parse.urlsplit(target_url)
            google_host = target.hostname in ('google.com', 'www.google.com', 'maps.google.com', 'google.fr', 'www.google.fr')
            if google_host and target.path.startswith('/maps/dir/'):
                set_status(task_id, 8, 'Extraction des points de l\'itinéraire...')
                # Détection du mode de transport
                mode = 'bicycle'
                if '!3e0' in target_url: mode = 'car'
                elif '!3e1' in target_url: mode = 'bicycle'
                elif '!3e2' in target_url: mode = 'foot'
                
                # Extraction multi-format des coordonnées
                # Google utilise !1d (Lon) et !2d (Lat) pour les points de l'itinéraire
                raw = re.findall(r'!1d(-?\d+\.\d+)!2d(-?\d+\.\d+)', target_url)
                # Parfois !2d (Lon) et !3d (Lat)
                raw += re.findall(r'!2d(-?\d+\.\d+)!3d(-?\d+\.\d+)', target_url)
                
                all_coords = []
                for lon, lat in raw:
                    lat, lon = coordinate(lat, lon)
                    all_coords.append((lon, lat))

                if not all_coords:
                    simple = re.findall(r'/(-?\d+\.\d+),(-?\d+\.\d+)', target_url)
                    for lat, lon in simple:
                        lat, lon = coordinate(lat, lon)
                        all_coords.append((lon, lat))

                # Déduplication et nettoyage
                coords = []
                seen = set()
                for pt in all_coords:
                    # Arrondi pour éviter les doublons quasi-identiques
                    pt_round = (round(pt[0], 6), round(pt[1], 6))
                    if pt_round not in seen:
                        coords.append(pt)
                        seen.add(pt_round)

                if len(coords) >= 2:
                    set_status(task_id, 10, f'Calcul du tracé routier ({mode})...')
                    osrm_coords = ';'.join([f"{lon},{lat}" for lon, lat in coords])
                    
                    # Correction des noms d'instances pour openstreetmap.de
                    if mode == 'bicycle':
                        osrm_base = "https://routing.openstreetmap.de/routed-bike"
                    elif mode == 'foot':
                        osrm_base = "https://routing.openstreetmap.de/routed-foot"
                    else:
                        osrm_base = "https://routing.openstreetmap.de/routed-car"
                    
                    osrm_url = f"{osrm_base}/route/v1/driving/{osrm_coords}?overview=full&geometries=geojson"
                    
                    try:
                        route_data = json.loads(fetch_public_url(osrm_url, timeout=15).content)
                        
                        if route_data.get('routes'):
                            geometry = route_data['routes'][0]['geometry']['coordinates']
                            gpx_root = ET.Element('gpx', version='1.1', creator='WaterMap', xmlns='http://www.topografix.com/GPX/1/1')
                            trk = ET.SubElement(gpx_root, 'trk')
                            trkseg = ET.SubElement(trk, 'trkseg')
                            if len(geometry) > MAX_TRACK_POINTS:
                                raise ValueError('Le tracé routier dépasse 100 000 points.')
                            for lon, lat in geometry:
                                lat, lon = coordinate(lat, lon)
                                ET.SubElement(trkseg, 'trkpt', lat=str(lat), lon=str(lon))
                            
                            content = ET.tostring(gpx_root, encoding='utf-8')
                            with open(upload_path, 'wb') as f:
                                f.write(content)
                            set_status(task_id, 12, f'Tracé routier récupéré ({len(geometry)} points)')
                        else:
                            raise ValueError("OSRM n'a pas pu calculer d'itinéraire.")
                    except Exception as error:
                        raise ValueError('Le service de calcul d’itinéraire est indisponible ou a renvoyé un tracé invalide. Réessayez avec un fichier GPX.') from error
                else:
                    raise ValueError('Ce lien Google Maps ne contient pas au moins deux coordonnées exploitables. Exportez un fichier GPX ou KML.')

            # 4. Traitement normal si on n'a pas encore créé le fichier
            if not os.path.exists(upload_path):
                # Google My Maps
                if google_host and target.path.startswith('/maps/d/'):
                    if 'mid=' in target_url:
                        mid_match = re.search(r'mid=([^&]+)', target_url)
                        if mid_match:
                            mid = urllib.parse.quote(urllib.parse.unquote(mid_match.group(1)), safe='')
                            target_url = f"https://www.google.com/maps/d/u/0/kml?mid={mid}&forcekml=1"
                
                resp = fetch_public_url(target_url, timeout=20)
                content = resp.content
                
                # Si c'est du KML, on convertit
                if b'<kml' in content or b'</kml>' in content:
                    set_status(task_id, 10, 'Conversion du KML en GPX...')
                    content = kml_to_gpx(content)
                
                with open(upload_path, 'wb') as f:
                    f.write(content)
                set_status(task_id, 12, 'Tracé récupéré avec succès')

        set_status(task_id, 15, 'Analyse du tracé...')
        track_points = parse_gpx_track_points(upload_path)
        if not track_points:
             raise ValueError("Le fichier ne contient aucun point de tracé valide.")

        set_status(task_id, 20, 'Utilisation des points d\u2019eau préchargés...')
        water_points = parse_water_points()
        set_status(task_id, 25, f'{len(water_points)} points d\u2019eau disponibles')
        index = RouteIndex(track_points)
        nearby_water = nearby_water_points(track_points, max_distance_m=max_distance_m, task_id=task_id, index=index)
        set_status(task_id, 80, 'Recherche des toilettes...')
        nearby_toilets_list = nearby_toilets(track_points, max_distance_m=max_distance_m, task_id=task_id, index=index)
        
        # Déduplication : si des toilettes sont au même endroit qu'un point d'eau, supprimer le point d'eau
        # Seuil de proximité : 10 mètres (coordonnées très proches)
        toilet_locations = set()
        for toilet in nearby_toilets_list:
            toilet_locations.add((round(toilet['lat'], 5), round(toilet['lon'], 5)))
        
        nearby_water_filtered = [w for w in nearby_water 
                                 if (round(w['lat'], 5), round(w['lon'], 5)) not in toilet_locations]
        
        set_status(task_id, 85, f'{len(nearby_water_filtered)} points d\u2019eau et {len(nearby_toilets_list)} toilettes trouvés')
        set_status(task_id, 90, 'Génération de la page...')

        page_path = os.path.join(user_page_folder, f"{page_name}.html")
        page_name_html = html.escape(page_name)
        original_name_html = html.escape(original_name)
        gpx_url_link = f"/uploads/{user_id}/{page_name}.gpx"
        def script_json(value):
            return json.dumps(value, ensure_ascii=True, allow_nan=False).replace('<', '\\u003c').replace('>', '\\u003e').replace('&', '\\u0026')

        water_points_js = script_json(nearby_water_filtered)
        toilets_js = script_json(nearby_toilets_list)
        starts = sorted(track_points.segment_starts | {len(track_points)})
        route_segments_js = script_json([track_points[a:b] for a, b in zip(starts, starts[1:])])
        if nearby_water_filtered:
            water_list_html = '<ul>' + ''.join([
                f"<li>{html.escape(pt['name'])} — {pt['distance_m']} m</li>"
                for pt in nearby_water_filtered
            ]) + '</ul>'
        else:
            water_list_html = f'<div class="empty-message">✨ Aucun point d\u2019eau trouvé sous {max_distance_m} m du parcours.</div>'
        if nearby_toilets_list:
            toilets_list_html = '<ul>' + ''.join([
                f"<li>{html.escape(pt['name'])} — {pt['distance_m']} m</li>"
                for pt in nearby_toilets_list
            ]) + '</ul>'
        else:
            toilets_list_html = f'<div class="empty-message">✨ Aucune toilette trouvée sous {max_distance_m} m du parcours.</div>'

        temporary_page = page_path + '.tmp'
        with open(temporary_page, 'w', encoding='utf-8') as f:
            f.write(f"""<!doctype html>
<html lang=\"fr\">
<head>
  <meta charset=\"utf-8\">
  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">
  <title>{page_name_html}</title>
  <link rel=\"stylesheet\" href=\"https://unpkg.com/leaflet@1.9.4/dist/leaflet.css\" integrity=\"sha256-p4NxAoJBhIIN+hmNHrzRCf9tD/miZyoHS5obTRR9BMY=\" crossorigin=\"anonymous\" />
  <style>
    * {{ margin: 0; padding: 0; box-sizing: border-box; }}
    body {{
      font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'Helvetica Neue', Arial, sans-serif;
      background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
      min-height: 100vh;
      padding: 20px;
    }}
    .container {{
      max-width: 1200px;
      margin: 0 auto;
    }}
    nav {{
      background: rgba(255, 255, 255, 0.95);
      padding: 15px 20px;
      border-radius: 8px;
      margin-bottom: 20px;
      box-shadow: 0 5px 20px rgba(0, 0, 0, 0.1);
    }}
    nav a {{
      color: #667eea;
      text-decoration: none;
      font-weight: 600;
      transition: color 0.2s;
    }}
    nav a:hover {{
      color: #764ba2;
    }}
    h1 {{
      color: white;
      margin-bottom: 10px;
      font-size: 2rem;
    }}
    .header-info {{
      color: rgba(255, 255, 255, 0.9);
      font-size: 0.95rem;
      margin-bottom: 20px;
    }}
    .card {{
      background: white;
      border-radius: 12px;
      padding: 30px;
      box-shadow: 0 10px 40px rgba(0, 0, 0, 0.2);
      margin-bottom: 30px;
    }}
    #map {{
      width: 100%;
      height: 500px;
      border-radius: 8px;
      margin-bottom: 20px;
    }}
    h2 {{
      color: #333;
      margin-bottom: 15px;
      font-size: 1.3rem;
    }}
    section ul {{
      list-style: none;
      padding: 0;
    }}
    section li {{
      padding: 12px;
      margin-bottom: 8px;
      background: #f8f9fa;
      border-left: 4px solid #667eea;
      border-radius: 4px;
      color: #333;
    }}
    .empty-message {{
      padding: 20px;
      background: #f0f4ff;
      border-radius: 8px;
      color: #667eea;
      border-left: 4px solid #667eea;
    }}
    @media (max-width: 768px) {{
      .container {{ padding: 0; }}
      h1 {{ font-size: 1.5rem; }}
      .card {{ padding: 20px; }}
      #map {{ height: 300px; }}
    }}
  </style>
</head>
<body>
  <nav><a href=\"/\">← Retour à la liste</a> · <a href=\"{gpx_url_link}\" download>Télécharger le GPX</a></nav>
  <div class=\"container\">
    <h1>🗺️ {page_name_html}</h1>
    <div class=\"header-info\">Fichier : {original_name_html}</div>
    <div class=\"card\">
      <div id=\"map\"></div>
    </div>
    <div class=\"card\">
      <h2>💧 Points d'eau à moins de {max_distance_m} m</h2>
      <section>
        {water_list_html}
      </section>
    </div>
    <div class=\"card\">
      <h2>🚻 Toilettes à moins de {max_distance_m} m</h2>
      <section>
        {toilets_list_html}
      </section>
    </div>
  </div>
  <script src=\"https://unpkg.com/leaflet@1.9.4/dist/leaflet.js\" integrity=\"sha256-20nQCchB9co0qIjJZRGuk2/Z9VM+kNiyxNV1lvTlZBo=\" crossorigin=\"anonymous\"></script>
  <script nonce=\"__WATERMAP_CSP_NONCE__\">
    const map = L.map('map').setView([0, 0], 2);
    L.tileLayer('https://tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png', {{
      referrerPolicy: 'strict-origin-when-cross-origin',
      attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors'
    }}).addTo(map);

    const waterPoints = {water_points_js};
    const waterLayer = L.featureGroup().addTo(map);
    const waterIcon = L.icon({{
      iconUrl: 'https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/images/marker-icon.png',
      iconSize: [25, 41],
      iconAnchor: [12, 41],
      popupAnchor: [1, -34],
      shadowUrl: 'https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/images/marker-shadow.png',
      shadowSize: [41, 41]
    }});

    const toiletPoints = {toilets_js};
    const toiletLayer = L.featureGroup().addTo(map);
    const toiletIcon = L.icon({{
      iconUrl: 'data:image/svg+xml;base64,PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIHdpZHRoPSIyNSIgaGVpZ2h0PSI0MSIgdmlld0JveD0iMCAwIDI1IDQxIj48cGF0aCBmaWxsPSIjMmVjYzcxIiBzdHJva2U9IiMyN2FlNjAiIHN0cm9rZS13aWR0aD0iMSIgZD0iTTEyLjUgMEM1LjU5NiAwIDAgNS41OTYgMCAxMi41YzAgNy43NDMgOS45OCAyOC41IDEyLjUgMjguNXMxMi41LTIwLjc1NyAxMi41LTI4LjVDMjUgNS41OTYgMTkuNDA0IDAgMTIuNSAweiIvPjxjaXJjbGUgY3g9IjEyLjUiIGN5PSIxMi41IiByPSI0IiBmaWxsPSJ3aGl0ZSIvPjwvc3ZnPg==',
      iconSize: [25, 41],
      iconAnchor: [12, 41],
      popupAnchor: [1, -34],
      shadowUrl: 'https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/images/marker-shadow.png',
      shadowSize: [41, 41]
    }});

    function popup(point) {{
      const content = document.createElement('div');
      const name = document.createElement('strong');
      name.textContent = point.name;
      const distance = document.createElement('p');
      distance.textContent = 'Distance : ' + point.distance_m + ' m';
      const link = document.createElement('a');
      link.href = 'https://maps.google.com/maps?q=&layer=c&cbll=' + point.lat + ',' + point.lon;
      link.textContent = 'Street View';
      link.target = '_blank';
      link.rel = 'noopener noreferrer';
      content.append(name, distance, link);
      return content;
    }}
    waterPoints.forEach(point => L.marker([point.lat, point.lon], {{icon: waterIcon}}).bindPopup(popup(point)).addTo(waterLayer));
    toiletPoints.forEach(point => L.marker([point.lat, point.lon], {{icon: toiletIcon}}).bindPopup(popup(point)).addTo(toiletLayer));
    const route = L.polyline({route_segments_js}).addTo(map);
    const bounds = route.getBounds();
    if (waterLayer.getLayers().length) bounds.extend(waterLayer.getBounds());
    if (toiletLayer.getLayers().length) bounds.extend(toiletLayer.getBounds());
    if (bounds.isValid()) map.fitBounds(bounds.pad(0.05));
  </script>
</body>
</html>""")

        with file_lock:
            if os.path.getsize(temporary_page) > MAX_FILE_BYTES:
                raise ValueError('La carte générée dépasse 20 Mo. Découpez le tracé en plusieurs itinéraires.')
            actual_size = sum(entry.stat().st_size for folder in (user_page_folder, get_user_upload_folder(user_id)) for entry in os.scandir(folder) if entry.is_file())
            if actual_size > 200 * 1024 * 1024:
                raise ValueError('La limite de stockage de 200 Mo est atteinte. Supprimez d’anciens itinéraires.')
            os.replace(temporary_page, page_path)
        set_status(task_id, 100, 'Terminé', done=True, page_url=f'/generated/{page_name}')
    except Exception as error:
        with file_lock:
            for candidate in (upload_path, os.path.join(get_user_page_folder(user_id), page_name + '.html.tmp')):
                if os.path.isfile(candidate):
                    os.remove(candidate)
        message = str(error) if isinstance(error, ValueError) else 'Le traitement a échoué. Vérifiez le tracé ou réessayez plus tard.'
        app.logger.warning('Échec du traitement GPX (%s).', type(error).__name__)
        set_status(task_id, 100, message, done=True, error=message)
    finally:
        with file_lock:
            active_names.discard((user_id, page_name))
        processing_slots.release()


@app.route('/logout', methods=['POST'])
@login_required
def logout():
    logout_user()
    session.clear()
    return redirect(url_for('home'))


@app.route('/')
def home():
    if not current_user.is_authenticated:
        return render_template('login.html')
    
    user_page_folder = get_user_page_folder(current_user.id)
    pages = []
    for filename in sorted(os.listdir(user_page_folder)):
        if filename.endswith('.html'):
            pages.append({'name': filename[:-5]})
    return render_template('index.html', pages=pages, user=current_user)


@app.route('/upload', methods=['POST'])
@login_required
@limiter.limit('5 per minute', methods=['POST'])
def upload():
    gpx_url = request.form.get('gpx_url', '').strip()
    file = request.files.get('gpx_file')

    if not file and not gpx_url:
        return jsonify({'error': 'Aucun fichier ou URL envoyé'}), 400

    if file and file.filename == '':
        return jsonify({'error': 'Fichier non valide'}), 400

    page_name_input = request.form.get('page_name', '').strip()
    if not page_name_input:
        return jsonify({'error': 'Le nom de la page est requis'}), 400

    page_name = secure_filename(page_name_input)
    if not page_name:
        return jsonify({'error': 'Le nom contient des caractères invalides'}), 400

    if len(page_name) > 100 or len(gpx_url) > 4096:
        return jsonify(error='Le nom de page ou le lien est trop long.'), 400
    if bool(file) == bool(gpx_url):
        return jsonify(error='Choisissez un fichier GPX ou un lien, pas les deux.'), 400
    if file and not allowed_file(file.filename):
        return jsonify(error='Seuls les fichiers .gpx sont acceptés.'), 400
    try:
        max_distance = int(request.form.get('max_distance', '500'))
        if not 0 <= max_distance <= 5000:
            raise ValueError
    except ValueError:
        return jsonify(error='Choisissez une distance entière entre 0 et 5 000 mètres.'), 400
    if not processing_slots.acquire(blocking=False):
        return jsonify(error='Deux tracés sont déjà en cours de traitement. Réessayez dans un instant.'), 503
    user_id = current_user.id
    task_id = str(uuid.uuid4())
    upload_path = None
    try:
        with file_lock:
            user_page_folder = get_user_page_folder(user_id)
            user_upload_folder = get_user_upload_folder(user_id)
            files = [entry for folder in (user_page_folder, user_upload_folder) for entry in os.scandir(folder) if entry.is_file()]
            reserved = sum(owner == user_id for owner, _ in active_names)
            if (len(files) + reserved * 2 >= 200
                    or sum(entry.stat().st_size for entry in files) + (reserved + 1) * MAX_FILE_BYTES > 200 * 1024 * 1024):
                processing_slots.release()
                return jsonify(error='Limite de 100 itinéraires ou 200 Mo atteinte. Supprimez d’anciens itinéraires.'), 413
            if shutil.disk_usage(DATA_DIR).free < 512 * 1024 * 1024:
                processing_slots.release()
                return jsonify(error='Espace de stockage insuffisant. Réessayez plus tard.'), 503
            base_name = page_name
            count = 0
            while ((user_id, page_name) in active_names
                   or os.path.exists(os.path.join(user_page_folder, page_name + '.html'))
                   or os.path.exists(os.path.join(user_upload_folder, page_name + '.gpx'))):
                count += 1
                page_name = f'{base_name}-{count}'
            active_names.add((user_id, page_name))
        upload_path = os.path.join(user_upload_folder, page_name + '.gpx')
        if file:
            file.save(upload_path)
        original_name = file.filename if file else 'Lien distant'
        with status_lock:
            completed = [previous for previous, value in processing_status.items() if value['done']]
            for previous in completed:
                value = processing_status[previous]
                if time.monotonic() - value['_updated'] > TASK_TTL_SECONDS or len(processing_status) >= TASK_HISTORY_LIMIT:
                    task_owners.pop(previous, None)
                    processing_status.pop(previous, None)
            task_owners[task_id] = user_id
            set_status(task_id, 0, 'Préparation du traitement...')
        executor.submit(process_gpx_task, task_id, upload_path, page_name, original_name, user_id, gpx_url, max_distance)
    except Exception:
        if upload_path and os.path.isfile(upload_path):
            os.remove(upload_path)
        with file_lock:
            active_names.discard((user_id, page_name))
        with status_lock:
            processing_status.pop(task_id, None)
            task_owners.pop(task_id, None)
        processing_slots.release()
        raise

    return jsonify({'task_id': task_id}), 202


@app.route('/status/<task_id>')
@login_required
def status(task_id):
    with status_lock:
        if task_owners.get(task_id) != current_user.id:
            return jsonify({'error': 'Tâche introuvable'}), 404
    status_data = get_status(task_id)
    if status_data is None:
        return jsonify({'error': 'Tâche introuvable'}), 404
    return jsonify(status_data)


@app.route('/generated/<page_name>')
@login_required
def page(page_name):
    user_page_folder = get_user_page_folder(current_user.id)
    filename = secure_filename(f"{page_name}.html")
    if not os.path.exists(os.path.join(user_page_folder, filename)):
        return "Page introuvable ou accès refusé", 403
    with open(os.path.join(user_page_folder, filename), encoding='utf-8') as source:
        content = source.read()
    content = content.replace('nonce="__WATERMAP_CSP_NONCE__"', f'nonce="{csp_nonce()}"')
    # Preserve historical maps while refusing arbitrary inline script elements.
    content = content.replace("<script>\n    const map = L.map('map')", f"<script nonce=\"{csp_nonce()}\">\n    const map = L.map('map')")
    # Existing maps also need the current OSM endpoint and attribution.
    content = content.replace('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', 'https://tile.openstreetmap.org/{z}/{x}/{y}.png')
    content = content.replace("attribution: '&copy; OpenStreetMap contributors'", "attribution: '&copy; <a href=\"https://www.openstreetmap.org/copyright\">OpenStreetMap</a> contributors'")
    # Add the export action to existing maps without rewriting their saved files.
    if 'data-watermap-enriched' not in content:
        export_url = html.escape(url_for('download_enriched', page_name=page_name), quote=True)
        content = content.replace('</nav>', f' · <a data-watermap-enriched href="{export_url}" download style="display:inline-block; padding:8px 12px; margin:4px 0; background:#edf2ff; border:1px solid #c7d2fe; border-radius:6px;">GPX enrichi · eau et WC</a></nav>', 1)
    share_url = html.escape(url_for('download_enriched', page_name=page_name), quote=True)
    share_name = html.escape(secure_filename(page_name) + '-watermap.gpx', quote=True)
    share_button = f' · <button type="button" data-garmin-share data-gpx-url="{share_url}" data-gpx-name="{share_name}" style="padding:8px 12px;margin:4px 0;border:1px solid #5464ce;border-radius:6px;background:#5464ce;color:white;cursor:pointer;">Envoyer à Garmin Connect</button>'
    content = content.replace('</nav>', share_button + '</nav>', 1)
    content = content.replace('</body>', render_template('gpx_share.html') + '</body>', 1)
    return app.response_class(content, mimetype='text/html')


@app.route('/itineraries/<page_name>/enriched.gpx')
@login_required
@limiter.limit('10 per minute')
def download_enriched(page_name):
    safe_name = secure_filename(page_name)
    if not safe_name or safe_name != page_name:
        abort(404)
    # Both inputs belong to the authenticated account; no other user's ID is accepted.
    with file_lock:
        if (current_user.id, safe_name) in active_names:
            return app.response_class('Cet itinéraire est en cours de création.', status=409, mimetype='text/plain')
        try:
            with open(os.path.join(get_user_page_folder(current_user.id), safe_name + '.html'), 'rb') as source:
                saved_map = source.read(MAX_MAP_BYTES + 1)
            with open(os.path.join(get_user_upload_folder(current_user.id), safe_name + '.gpx'), 'rb') as source:
                original = source.read(MAX_FILE_BYTES + 1)
        except FileNotFoundError:
            abort(404)
    try:
        if len(saved_map) > MAX_MAP_BYTES:
            raise ValueError('Cette carte est trop volumineuse pour être exportée.')
        water, toilets = map_points(saved_map.decode('utf-8'))
        result = enriched_gpx(original, water, toilets)
    except (ValueError, UnicodeError) as error:
        return app.response_class(str(error), status=422, mimetype='text/plain')
    return send_file(BytesIO(result), mimetype='application/gpx+xml', as_attachment=True,
                     download_name=safe_name + '-watermap.gpx', max_age=0)


@app.route('/uploads/<int:user_id>/<path:filename>')
@login_required
def uploaded_file(user_id, filename):
    if user_id != current_user.id:
        return "Accès refusé", 403
    user_upload_folder = get_user_upload_folder(user_id)
    safe_filename = secure_filename(filename)
    return send_from_directory(user_upload_folder, safe_filename, mimetype='application/gpx+xml', as_attachment=True)


@app.route('/delete/<page_name>', methods=['POST'])
@login_required
def delete(page_name):
    safe_name = secure_filename(page_name)
    user_page_folder = get_user_page_folder(current_user.id)
    user_upload_folder = get_user_upload_folder(current_user.id)
    
    page_path = os.path.join(user_page_folder, f"{safe_name}.html")
    upload_path = os.path.join(user_upload_folder, f"{safe_name}.gpx")

    with file_lock:
        if (current_user.id, safe_name) in active_names:
            return jsonify(error='Cette page est en cours de création.'), 409
        for path in (page_path, upload_path):
            if os.path.exists(path):
                os.remove(path)

    return redirect(url_for('home'))


if __name__ == '__main__':
    app.run(host='127.0.0.1', port=8081)
