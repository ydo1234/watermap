import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import account, csrf, login


@pytest.fixture
def admin(module, client, owner, monkeypatch):
    module.db.session.add(module.AdminAccess(user_id=owner.id))
    module.db.session.commit()
    monkeypatch.setitem(module.app.config, 'MAIL_SUPPRESS_SEND', True)
    monkeypatch.setitem(module.app.config, 'MAIL_DEFAULT_SENDER', 'sender@example.test')
    return owner


def admin_post(client, path, **data):
    return client.post(path, data={'csrf_token': csrf(client, '/admin/announcements/new'), **data})


def draft(module, client, **values):
    response = admin_post(client, '/admin/announcements/new', title='Information Watermap',
                          body=values.pop('body', 'Un message pour les utilisateurs.'), kind='message', **values)
    assert response.status_code == 302
    return module.db.session.execute(module.db.select(module.Announcement)).scalar_one()


def wait_for_mail(module):
    module.mail_executor.submit(lambda: None).result(timeout=5)
    module.db.session.expire_all()


def test_admin_requires_explicit_grant_and_checks_all_routes(module, client, owner):
    assert client.get('/admin').status_code == 403
    for path in ('/admin/announcements/new', '/admin/announcements/1', '/admin/announcements/1/edit'):
        assert client.get(path).status_code == 403
    token = csrf(client)
    for path in ('new', '1/edit', '1/publish', '1/hide', '1/send'):
        assert client.post('/admin/announcements/' + path, data={'csrf_token': token}).status_code == 403
    assert b'>Administration<' not in client.get('/').data
    runner = module.app.test_cli_runner()
    assert runner.invoke(args=['admin-user', owner.email]).exit_code == 0
    assert client.get('/admin').status_code == 200
    assert b'>Administration<' in client.get('/').data
    assert runner.invoke(args=['admin-user', owner.email, '--revoke']).exit_code == 0
    assert client.get('/admin').status_code == 403
    assert runner.invoke(args=['admin-user', 'missing@example.test']).exit_code != 0
    anonymous = module.app.test_client()
    assert anonymous.get('/admin').status_code == 302


def test_draft_edit_preview_publish_hide_and_escaping(module, client, admin):
    text = '<script>alert("hello")</script>\nAnnonce test'
    item = draft(module, client, body=text)
    path = f'/admin/announcements/{item.id}'
    anonymous = module.app.test_client()
    assert b'Annonce test' not in anonymous.get('/').data
    preview = client.get(path)
    assert preview.status_code == 200 and b'&lt;script&gt;' in preview.data
    assert b'<script>alert' not in preview.data
    assert client.post(path + '/publish').status_code == 400
    assert client.get(path + '/publish').status_code == 405
    assert admin_post(client, path + '/edit', title='Titre corrigé', body=text, kind='downtime').status_code == 302
    assert admin_post(client, path + '/publish').status_code == 302
    for browser in (client, anonymous):
        response = browser.get('/')
        assert b'Annonce test' in response.data and b'&lt;script&gt;' in response.data
        assert response.status_code == 200
    assert client.get(path + '/edit').status_code == 409
    assert admin_post(client, path + '/hide').status_code == 302
    assert b'Annonce test' not in anonymous.get('/login').data
    assert client.get(path + '/edit').status_code == 409


def test_templates_remain_editable_and_do_not_save_or_publish(module, client, admin):
    response = admin_post(client, '/admin/announcements/new', kind='update', action='template',
                          scheduled_for='2026-11-05T14:30', duration='30 minutes')
    assert response.status_code == 200
    assert b'05/11/2026' in response.data and b'14:30' in response.data and b'30 minutes' in response.data
    assert module.db.session.query(module.Announcement).count() == 0
    response = admin_post(client, '/admin/announcements/new', kind='downtime', action='template')
    assert 'temporairement indisponible' in response.text
    assert module.db.session.query(module.Announcement).count() == 0


@pytest.mark.parametrize('values', [
    {'kind': 'unknown'}, {'title': ''}, {'body': ''}, {'body': 'x' * 5001},
    {'title': 'x' * 161}, {'title': 'Title\r\nBcc: injected@example.test'},
    {'scheduled_for': 'bad-date'}, {'duration': 'x' * 81},
    {'action': 'template', 'kind': 'update', 'scheduled_for': 'bad-date'},
])
def test_invalid_messages_are_not_saved(module, client, admin, values):
    response = admin_post(client, '/admin/announcements/new', **{
        'title': 'Titre', 'body': 'Texte', 'kind': 'message', **values,
    })
    assert response.status_code == 200
    assert b'role="alert"' in response.data
    assert module.db.session.query(module.Announcement).count() == 0


def test_individual_mail_confirm_snapshot_and_no_duplicates(module, client, admin, monkeypatch):
    monkeypatch.setitem(module.app.config, 'MAIL_SUPPRESS_SEND', False)
    account(module, 'second@example.test')
    item = draft(module, client)
    path = f'/admin/announcements/{item.id}'
    assert admin_post(client, path + '/send').status_code == 400
    assert module.db.session.query(module.AnnouncementDelivery).count() == 0
    messages = []
    monkeypatch.setattr(module.mail, 'send', messages.append)
    assert admin_post(client, path + '/send', confirm='yes').status_code == 302
    wait_for_mail(module)
    assert len(messages) == 2
    assert [m.recipients for m in messages] == [[admin.email], ['second@example.test']]
    assert all(not m.cc and not m.bcc and 'https://watermap.example' in m.body for m in messages)
    assert module.db.session.query(module.AnnouncementDelivery).filter_by(status='sent').count() == 2
    assert client.get(path + '/edit').status_code == 409
    account(module, 'later@example.test')
    assert admin_post(client, path + '/send', confirm='yes').status_code == 302
    wait_for_mail(module)
    assert len(messages) == 2
    assert b'second@example.test' in client.get(path).data


def test_uncertain_delivery_is_not_retried_and_pending_can_resume(module, client, admin, monkeypatch):
    monkeypatch.setitem(module.app.config, 'MAIL_SUPPRESS_SEND', False)
    account(module, 'second@example.test')
    item = draft(module, client)
    path = f'/admin/announcements/{item.id}/send'
    attempts = []
    def timeout(message):
        attempts.append(message.recipients)
        raise TimeoutError('SMTP acceptance unknown')
    monkeypatch.setattr(module.mail, 'send', timeout)
    admin_post(client, path, confirm='yes')
    wait_for_mail(module)
    assert attempts == [[admin.email]]
    assert module.db.session.query(module.AnnouncementDelivery).filter_by(status='uncertain').count() == 1
    assert module.db.session.query(module.AnnouncementDelivery).filter_by(status='pending').count() == 1
    monkeypatch.setattr(module.mail, 'send', lambda message: attempts.append(message.recipients))
    admin_post(client, path, confirm='yes')
    wait_for_mail(module)
    assert attempts == [[admin.email], ['second@example.test']]


def test_simulated_mail_never_becomes_a_real_resend(module, client, admin, monkeypatch):
    item = draft(module, client)
    path = f'/admin/announcements/{item.id}/send'
    monkeypatch.setattr(module.mail, 'send', lambda message: pytest.fail('Simulation must not call SMTP'))
    admin_post(client, path, confirm='yes')
    wait_for_mail(module)
    assert module.db.session.query(module.AnnouncementDelivery).one().status == 'simulated'
    monkeypatch.setitem(module.app.config, 'MAIL_SUPPRESS_SEND', False)
    assert admin_post(client, path, confirm='yes').status_code == 409


def test_claimed_delivery_survives_restart_without_resend(module, client, admin, monkeypatch):
    item = draft(module, client)
    item.mail_prepared = item.mail_simulated = True
    module.db.session.add(module.AnnouncementDelivery(announcement_id=item.id, user_id=admin.id,
                                                    email=admin.email, name=admin.name, status='sending'))
    module.db.session.commit()
    monkeypatch.setattr(module.mail, 'send', lambda message: pytest.fail('Unexpected resend'))
    admin_post(client, f'/admin/announcements/{item.id}/send', confirm='yes')
    wait_for_mail(module)
    assert module.db.session.query(module.AnnouncementDelivery).one().status == 'sending'


def test_existing_user_database_is_preserved_on_startup(tmp_path, monkeypatch):
    import os
    (tmp_path / 'db').mkdir()
    with sqlite3.connect(tmp_path / 'db/users.db') as connection:
        connection.execute('CREATE TABLE user (id INTEGER PRIMARY KEY, name VARCHAR(100), email VARCHAR(100) UNIQUE NOT NULL, password_hash VARCHAR(200) NOT NULL)')
        connection.execute("INSERT INTO user VALUES (1, 'Existing', 'existing@example.test', 'unchanged')")
    script = '''import app
with app.app.app_context():
    user = app.db.session.get(app.User, 1)
    assert user.password_hash == 'unchanged'
    assert not user.is_admin
    assert app.db.session.query(app.Announcement).count() == 0
'''
    subprocess.run([sys.executable, '-c', script], check=True, timeout=30,
                   cwd=Path(__file__).resolve().parents[1],
                   env={**os.environ, 'SECRET_KEY': 'test-key-' * 8, 'WATERMAP_DATA_DIR': str(tmp_path)})
