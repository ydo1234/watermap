from pathlib import Path

from conftest import account, csrf, post


def saved_route(module, user, name):
    page = Path(module.get_user_page_folder(user.id), name + '.html')
    upload = Path(module.get_user_upload_folder(user.id), name + '.gpx')
    page.write_text('<html><body>Carte</body></html>')
    upload.write_text('<gpx/>')
    return page, upload


def test_delete_all_removes_only_current_users_routes(module, client, owner):
    own_files = [*saved_route(module, owner, 'first'), *saved_route(module, owner, 'second')]
    other = account(module, 'other@example.test')
    other_files = saved_route(module, other, 'first')
    response = client.get('/')
    assert 'Supprimer tous mes parcours' in response.text
    assert 'name="page_count" value="2"' in response.text
    assert post(client, '/delete-all', confirm_delete_all='yes', page_count='2').status_code == 302
    assert all(not path.exists() for path in own_files)
    assert all(path.exists() for path in other_files)
    assert 'id="delete-all-form"' not in client.get('/').text


def test_delete_all_requires_confirmation_and_csrf(module, client, owner):
    files = saved_route(module, owner, 'tour')
    assert post(client, '/delete-all', page_count='1').status_code == 400
    assert client.post('/delete-all', data={'confirm_delete_all': 'yes', 'page_count': '1'}).status_code == 400
    assert all(path.exists() for path in files)


def test_delete_all_requires_login_and_post(client):
    assert client.get('/delete-all').status_code == 405
    token = csrf(client)
    assert client.post('/delete-all', data={'csrf_token': token, 'confirm_delete_all': 'yes', 'page_count': '0'}).status_code == 302


def test_delete_all_refuses_changed_list(module, client, owner):
    files = [*saved_route(module, owner, 'first'), *saved_route(module, owner, 'second')]
    response = post(client, '/delete-all', confirm_delete_all='yes', page_count='1')
    assert response.status_code == 302
    assert all(path.exists() for path in files)
    assert 'La liste des parcours a changé' in client.get('/').text


def test_delete_all_waits_for_own_processing(module, client, owner):
    files = saved_route(module, owner, 'tour')
    module.active_names.add((owner.id, 'new'))
    try:
        assert post(client, '/delete-all', confirm_delete_all='yes', page_count='1').status_code == 302
        assert all(path.exists() for path in files)
        assert 'Un parcours est en cours de création' in client.get('/').text
    finally:
        module.active_names.discard((owner.id, 'new'))


def test_delete_all_allows_other_users_processing(module, client, owner):
    files = saved_route(module, owner, 'tour')
    other = account(module, 'other@example.test')
    module.active_names.add((other.id, 'new'))
    try:
        assert post(client, '/delete-all', confirm_delete_all='yes', page_count='1').status_code == 302
        assert all(not path.exists() for path in files)
    finally:
        module.active_names.discard((other.id, 'new'))
