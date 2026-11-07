"""Ensure staging mail can only reach the owner's approved mailbox."""
import os
from flask import current_app
from flask_mail import Mail, Connection

STAGING_RECIPIENT = 'ydolisi@gmail.com'
STAGING_URL = 'https://stag.watermap.dolisi.fr'


def staging(app):
    return (os.environ.get('WATERMAP_INSTANCE') == 'stag'
            or app.config.get('WATERMAP_INSTANCE') == 'stag'
            or app.config.get('PUBLIC_BASE_URL') == STAGING_URL)


def require_redirect(app):
    if app.config.get('MAIL_REDIRECT_TO') != STAGING_RECIPIENT:
        raise RuntimeError('Staging mail must redirect exclusively to the approved owner mailbox')


class StagingConnection(Connection):
    def send(self, message, envelope_from=None):
        require_redirect(current_app)
        message.recipients = [STAGING_RECIPIENT]
        message.cc = []
        message.bcc = []
        message.reply_to = STAGING_RECIPIENT
        if message.extra_headers:
            blocked = {'to', 'cc', 'bcc', 'resent-to', 'resent-cc', 'resent-bcc', 'reply-to'}
            message.extra_headers = {key: value for key, value in message.extra_headers.items() if key.lower() not in blocked}
        if not (message.subject or '').startswith('[STAG] '):
            message.subject = '[STAG] ' + (message.subject or '')
        return super().send(message, envelope_from)


class SafeMail(Mail):
    def init_app(self, app):
        if staging(app):
            require_redirect(app)
        return super().init_app(app)

    def connect(self):
        app = self.app or current_app
        if staging(app):
            require_redirect(app)
            return StagingConnection(app.extensions['mail'])
        return super().connect()
