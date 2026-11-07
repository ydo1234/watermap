from email import message_from_bytes
from flask import Flask
from flask_mail import Message
import pytest
from mail_safety import SafeMail, STAGING_RECIPIENT


def application(stag=True):
    app=Flask(__name__)
    app.config.update(MAIL_SUPPRESS_SEND=False, MAIL_SERVER='smtp.invalid', MAIL_PORT=25,
                      MAIL_DEFAULT_SENDER='watermap@example.test',
                      WATERMAP_INSTANCE='stag' if stag else 'production',
                      MAIL_REDIRECT_TO=STAGING_RECIPIENT if stag else '')
    return app


@pytest.mark.parametrize('direct', [True, False])
def test_all_recipient_paths_are_redirected(monkeypatch, direct):
    deliveries=[]
    class SMTP:
        def __init__(self,*args,**kwargs): pass
        def set_debuglevel(self,*args): pass
        def sendmail(self,sender,recipients,body,*args): deliveries.append((recipients,message_from_bytes(body)))
        def quit(self): pass
    monkeypatch.setattr('flask_mail.smtplib.SMTP',SMTP)
    app=application()
    mail=SafeMail(app)
    with app.app_context():
        message=Message('Annonce',recipients=['other@example.test'],cc=['cc@example.test'],bcc=['bcc@example.test'],
                        extra_headers={'Resent-To':'extra@example.test','Cc':'extra@example.test'})
        message.body='Test de recette'
        if direct:
            mail.send(message)
        else:
            with mail.connect() as connection:
                connection.send(message)
    assert len(deliveries)==1
    recipients,raw=deliveries[0]
    assert recipients==[STAGING_RECIPIENT]
    assert raw['To']==STAGING_RECIPIENT
    assert raw['Cc'] is None and raw['Bcc'] is None and raw['Resent-To'] is None
    assert raw['Reply-To']==STAGING_RECIPIENT
    assert raw['Subject']=='[STAG] Annonce'


def test_staging_refuses_missing_or_modified_redirect():
    app=application()
    app.config['MAIL_REDIRECT_TO']='other@example.test'
    with pytest.raises(RuntimeError,match='exclusively'):
        SafeMail(app)
    app.config['MAIL_REDIRECT_TO']=STAGING_RECIPIENT
    mail=SafeMail(app)
    app.config['MAIL_REDIRECT_TO']='other@example.test'
    with app.app_context(),pytest.raises(RuntimeError,match='exclusively'):
        mail.connect()


def test_production_recipients_are_unchanged():
    app=application(stag=False)
    app.config['MAIL_SUPPRESS_SEND']=True
    mail=SafeMail(app)
    with app.app_context(),mail.record_messages() as outbox:
        mail.send(Message('Normal',recipients=['other@example.test'],body='Production'))
    assert outbox[0].recipients==['other@example.test']
    assert outbox[0].subject=='Normal'
