import importlib.util
from pathlib import Path

import pytest

spec=importlib.util.spec_from_file_location('release_mailing',Path(__file__).resolve().parents[1]/'deploy/ionos/notify-release.py')
module=importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def test_production_guard():
    production={'PUBLIC_BASE_URL':module.PRODUCTION_URL,'SESSION_COOKIE_SECURE':True,'TESTING':False,'MAIL_SUPPRESS_SEND':False}
    module.require_production(production,{})
    for override in ({'PUBLIC_BASE_URL':'http://rpi4ng:8081'},{'SESSION_COOKIE_SECURE':False},{'TESTING':True},{'MAIL_SUPPRESS_SEND':True}):
        with pytest.raises(RuntimeError):
            module.require_production({**production,**override},{})
    with pytest.raises(RuntimeError):
        module.require_production(production,{'FLASK_ENV':'development'})


def test_individual_sends_and_idempotency(tmp_path):
    ledger=module.init_ledger(tmp_path/'ledger.db')
    calls=[]
    recipients=[(1,'One','one@example.invalid'),(2,'Two','two@example.invalid')]
    result=module.deliver_release(ledger,'a'*40,'notes',recipients,lambda *args:calls.append(args))
    assert result['sent']==2
    assert [call[1] for call in calls]==['one@example.invalid','two@example.invalid']
    again=module.deliver_release(ledger,'a'*40,'notes',recipients,lambda *args:calls.append(args))
    assert again['already_sent']==2 and len(calls)==2
    ledger.close()


def test_uncertain_delivery_never_retried_automatically(tmp_path):
    ledger=module.init_ledger(tmp_path/'ledger.db')
    calls=[]
    def timeout(*args):
        calls.append(args)
        raise TimeoutError('SMTP acceptance unknown')
    recipients=[(1,'One','one@example.invalid')]
    assert module.deliver_release(ledger,'b'*40,'notes',recipients,timeout)['needs_review']==1
    assert module.deliver_release(ledger,'b'*40,'notes',recipients,timeout)['needs_review']==1
    assert len(calls)==1
    ledger.close()


def test_modified_notes_do_not_cause_resend(tmp_path):
    ledger=module.init_ledger(tmp_path/'ledger.db')
    recipients=[(1,'One','one@example.invalid')]
    assert module.deliver_release(ledger,'c'*40,'notes',recipients,lambda *args:None)['sent']==1
    assert module.deliver_release(ledger,'c'*40,'changed',recipients,lambda *args:pytest.fail('resend'))['needs_review']==1
    ledger.close()


def test_mime_message_has_one_recipient_and_one_message_id():
    from email import message_from_string
    from flask import Flask
    from flask_mail import Mail
    app=Flask(__name__)
    app.config.update(MAIL_USERNAME='sender@example.invalid',MAIL_DEFAULT_SENDER='')
    Mail(app)
    with app.app_context():
        message=module.make_message('User','recipient@example.invalid','Production https://watermap.dolisi.fr','<release@example.invalid>')
        mime=message_from_string(message.as_string())
    assert mime.get_all('Message-ID')==['<release@example.invalid>']
    assert mime['To']=='recipient@example.invalid'
    assert mime['From']=='sender@example.invalid'
    assert mime['Cc'] is None and mime['Bcc'] is None
