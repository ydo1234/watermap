"""Transfert privé de traces CycleRouter, confirmé par leur propriétaire SSO."""
import hashlib
import hmac
from io import BytesIO
import os
from pathlib import Path
import re
import tempfile
import threading
import time

from flask import Blueprint, abort, jsonify, redirect, render_template, request, url_for
from flask_login import current_user
from werkzeug.datastructures import FileStorage
from werkzeug.utils import secure_filename

from gpx_utils import MAX_FILE_BYTES, parse_track

TOKEN = re.compile(r'[a-f0-9]{64}')


def install_cyclerouter(app, db, csrf, limiter, oidc, start_upload, get_status, page_folder):
    app.config['CYCLEROUTER_TRANSFER_SECRET'] = os.getenv('CYCLEROUTER_TRANSFER_SECRET', '')
    secret = app.config['CYCLEROUTER_TRANSFER_SECRET']
    if secret and (len(secret) < 32 or not app.config['OIDC_ENABLED']):
        raise RuntimeError('Le transfert CycleRouter nécessite le SSO et un secret dédié d’au moins 32 caractères.')

    class CycleTransfer(db.Model):
        __tablename__ = 'cyclerouter_transfer'
        id = db.Column(db.String(64), primary_key=True)
        issuer = db.Column(db.String(512), nullable=False)
        subject = db.Column(db.String(255), nullable=False)
        name = db.Column(db.String(200), nullable=False)
        gpx = db.Column(db.LargeBinary, nullable=False)
        expires = db.Column(db.Float, nullable=False, index=True)
        task_id = db.Column(db.String(64))
        page_name = db.Column(db.String(110))

    bp = Blueprint('cyclerouter', __name__)
    lock = threading.Lock()

    def enabled():
        if not app.config.get('CYCLEROUTER_TRANSFER_SECRET') or not app.config['OIDC_ENABLED']:
            abort(404)

    @bp.post('/api/imports/cyclerouter')
    @csrf.exempt
    @limiter.limit('12 per minute')
    def receive():
        enabled()
        timestamp = request.headers.get('X-CycleRouter-Timestamp', '')
        signature = request.headers.get('X-CycleRouter-Signature', '')
        if not re.fullmatch(r'\d{10}', timestamp) or abs(time.time() - int(timestamp)) > 60:
            abort(401)
        body = request.get_data()
        expected = hmac.new(app.config['CYCLEROUTER_TRANSFER_SECRET'].encode(),
            b'POST\n/api/imports/cyclerouter\n' + timestamp.encode() + b'\n' + body, hashlib.sha256).hexdigest()
        if not TOKEN.fullmatch(signature) or not hmac.compare_digest(signature, expected):
            abort(401)
        data = request.get_json(silent=True)
        if (not isinstance(data, dict) or data.get('issuer') != app.config['OIDC_ISSUER']
                or not isinstance(data.get('subject'), str) or not 0 < len(data['subject']) <= 255
                or not isinstance(data.get('name'), str) or not 0 < len(data['name'].strip()) <= 200
                or not isinstance(data.get('token'), str) or not TOKEN.fullmatch(data['token'])
                or not isinstance(data.get('gpx'), str)):
            return jsonify(error='Transfert CycleRouter invalide.'), 400
        gpx = data['gpx'].encode('utf-8')
        if len(gpx) > MAX_FILE_BYTES:
            return jsonify(error='La trace dépasse la limite de 20 Mo.'), 413
        identifier = hashlib.sha256(data['token'].encode()).hexdigest()
        with lock:
            db.session.query(CycleTransfer).filter(CycleTransfer.expires < time.time()).delete()
            existing = db.session.get(CycleTransfer, identifier)
            if existing:
                if (existing.issuer, existing.subject) != (data['issuer'], data['subject']):
                    abort(409)
                db.session.commit()
                return jsonify(ready=True), 200
            count, size = db.session.query(db.func.count(CycleTransfer.id),
                db.func.coalesce(db.func.sum(db.func.length(CycleTransfer.gpx)), 0)).one()
            own = db.session.query(CycleTransfer).filter_by(subject=data['subject'], task_id=None).count()
            if count >= 64 or size + len(gpx) > 64 * 1024 * 1024 or own >= 4:
                db.session.commit()
                return jsonify(error='Trop de traces en attente. Importez-les ou réessayez dans 15 minutes.'), 429
            try:
                with tempfile.NamedTemporaryFile(suffix='.gpx') as source:
                    source.write(gpx)
                    source.flush()
                    parse_track(source.name)
            except ValueError as error:
                return jsonify(error=str(error)), 422
            db.session.add(CycleTransfer(id=identifier, issuer=data['issuer'], subject=data['subject'],
                name=data['name'].strip(), gpx=gpx, expires=time.time() + 900))
            db.session.commit()
        return jsonify(ready=True), 201

    def owned_transfer(token):
        enabled()
        if not TOKEN.fullmatch(token):
            abort(404)
        row = db.session.get(CycleTransfer, hashlib.sha256(token.encode()).hexdigest())
        if not row or row.expires < time.time():
            return None
        identity = oidc['valid_session'](current_user.id)
        if not identity or row.issuer != app.config['OIDC_ISSUER'] or row.subject != identity.subject:
            abort(403, description='Connectez-vous avec le même compte Dolisi que dans CycleRouter.')
        return row

    @bp.route('/imports/cyclerouter/<token>', methods=['GET', 'POST'])
    @limiter.limit('12 per minute', methods=['POST'])
    def confirm(token):
        enabled()
        if not TOKEN.fullmatch(token):
            abort(404)
        if not current_user.is_authenticated:
            return redirect(url_for('sso.login', transfer=token))
        with lock:
            row = owned_transfer(token)
            if row is None:
                return render_template('sso_error.html', message='Ce transfert a expiré. Renvoyez la trace depuis CycleRouter.'), 410
            target = url_for('page', page_name=row.page_name) if row.page_name else None
            exists = row.page_name and Path(page_folder(current_user.id), row.page_name + '.html').is_file()
            status = get_status(row.task_id) if row.task_id else None
            running = status and not status.get('done') and not status.get('error')
            if request.method == 'GET':
                if exists:
                    return redirect(target)
                return render_template('cyclerouter_import.html', name=row.name[:100],
                    task_id=row.task_id if running else None)
            if exists:
                return jsonify(page_url=target)
            if running:
                return jsonify(task_id=row.task_id), 202
            source = FileStorage(stream=BytesIO(row.gpx), filename=(secure_filename(row.name)[:100] or 'parcours') + '.gpx',
                                 content_type='application/gpx+xml')
            response = app.make_response(start_upload(source, '', request.form.get('page_name', row.name),
                                                     request.form.get('max_distance', '500')))
            if response.status_code == 202:
                result = response.get_json()
                row.task_id, row.page_name = result['task_id'], result['page_name']
                db.session.commit()
            return response

    app.register_blueprint(bp)
    return CycleTransfer
