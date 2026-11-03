"""Check SMTP authentication without sending a message or printing credentials."""
import os
import smtplib
import ssl

server = os.environ.get('MAIL_SERVER', 'smtp.gmail.com')
port = int(os.environ.get('MAIL_PORT', '587'))
try:
    with smtplib.SMTP(server, port, timeout=15) as client:
        client.ehlo()
        if os.environ.get('MAIL_USE_TLS', 'True').lower() in ('true', 'on', '1'):
            client.starttls(context=ssl.create_default_context())
            client.ehlo()
        client.login(os.environ['MAIL_USERNAME'], os.environ['MAIL_PASSWORD'])
        assert client.noop()[0] == 250
    print('SMTP : authentification et TLS OK ; aucun message envoyé')
except Exception as exc:
    print('SMTP : échec', type(exc).__name__, getattr(exc, 'smtp_code', ''))
    raise SystemExit(1)
