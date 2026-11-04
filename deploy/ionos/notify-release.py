#!/usr/bin/env python3
"""Notify production users once per release, after deployment checks succeed."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3

PRODUCTION_URL = 'https://watermap.dolisi.fr'


def require_production(config, environment):
    if (config.get('PUBLIC_BASE_URL') != PRODUCTION_URL
            or not config.get('SESSION_COOKIE_SECURE')
            or config.get('TESTING') or config.get('MAIL_SUPPRESS_SEND')
            or environment.get('FLASK_ENV') == 'development'):
        raise RuntimeError('Release notifications are restricted to the production instance')


def init_ledger(path):
    connection = sqlite3.connect(path, timeout=15)
    connection.execute('''CREATE TABLE IF NOT EXISTS delivery (
        release TEXT NOT NULL, user_id INTEGER NOT NULL, recipient_hash TEXT NOT NULL,
        notes_hash TEXT NOT NULL, status TEXT NOT NULL, updated_at TEXT NOT NULL,
        message_id TEXT NOT NULL, error_class TEXT, PRIMARY KEY(release,user_id))''')
    connection.commit()
    os.chmod(path, 0o600)
    return connection


def deliver_release(connection, release, notes, recipients, send):
    """Never automatically resend sent or ambiguous SMTP deliveries."""
    notes_hash = hashlib.sha256(notes.encode()).hexdigest()
    result = {'recipients': len(recipients), 'sent': 0, 'already_sent': 0, 'needs_review': 0}
    for identifier, name, email in recipients:
        recipient_hash = hashlib.sha256(email.encode()).hexdigest()
        message_id = f'<watermap-{release}-{identifier}@watermap.dolisi.fr>'
        connection.execute('BEGIN IMMEDIATE')
        previous = connection.execute('SELECT status,recipient_hash,notes_hash FROM delivery WHERE release=? AND user_id=?', (release,identifier)).fetchone()
        if previous:
            connection.rollback()
            if previous == ('sent',recipient_hash,notes_hash):
                result['already_sent'] += 1
            else:
                result['needs_review'] += 1
            continue
        timestamp = datetime.now(timezone.utc).isoformat()
        connection.execute('INSERT INTO delivery VALUES (?,?,?,?,?,?,?,?)', (release,identifier,recipient_hash,notes_hash,'sending',timestamp,message_id,None))
        connection.commit()
        try:
            send(name,email,notes,message_id)
        except Exception as error:
            connection.execute('UPDATE delivery SET status=?,error_class=?,updated_at=? WHERE release=? AND user_id=?', ('uncertain',type(error).__name__,datetime.now(timezone.utc).isoformat(),release,identifier))
            connection.commit()
            result['needs_review'] += 1
            # A broken SMTP connection must not mark other recipients uncertain.
            break
        connection.execute('UPDATE delivery SET status=?,updated_at=? WHERE release=? AND user_id=?', ('sent',datetime.now(timezone.utc).isoformat(),release,identifier))
        connection.commit()
        result['sent'] += 1
    result['pending'] = result['recipients'] - result['sent'] - result['already_sent'] - result['needs_review']
    return result


def make_message(name, email, body, message_id):
    from flask import current_app
    from flask_mail import Message
    greeting = f'Bonjour {name},' if name else 'Bonjour,'
    sender = current_app.config.get('MAIL_DEFAULT_SENDER') or current_app.config.get('MAIL_USERNAME')
    message = Message('Watermap — nouvelle version disponible', recipients=[email],
                      sender=sender, body=greeting+'\n\n'+body+'\n')
    message.msgId = message_id
    return message


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', type=Path, required=True)
    parser.add_argument('--notes', type=Path, required=True)
    parser.add_argument('--send', action='store_true')
    args = parser.parse_args()
    manifest = json.loads(args.manifest.read_text())
    release = manifest['source_commit']
    if not re.fullmatch('[0-9a-f]{40}',release) or manifest.get('working_tree_dirty'):
        raise RuntimeError('A clean, identified release is required')
    from wsgi import app
    from app import db, User, mail, DB_FOLDER
    with app.app_context():
        require_production(app.config,os.environ)
        recipients = [(user.id,user.name,user.email) for user in db.session.execute(db.select(User).order_by(User.id)).scalars()]
        notes = args.notes.read_text(encoding='utf-8').strip()
        if not notes or PRODUCTION_URL not in notes:
            raise RuntimeError('Release notes must include the production link')
        if not args.send:
            print(json.dumps({'release':release,'production_recipients':len(recipients),'send':False}))
            return
        os.umask(0o077)
        ledger = init_ledger(Path(DB_FOLDER)/'release-mailings.sqlite3')
        try:
            with mail.connect() as sender:
                def send(name,email,body,message_id):
                    sender.send(make_message(name,email,body,message_id))
                result = deliver_release(ledger,release,notes,recipients,send)
            print(json.dumps({'release':release,**result}))
            if result['needs_review'] or result['pending']:
                raise SystemExit(2)
        finally:
            ledger.close()


if __name__ == '__main__':
    main()
