import importlib
import secrets
import shutil
from pathlib import Path

import pytest


@pytest.fixture(scope='session')
def watermap(tmp_path_factory):
    import os
    directory = tmp_path_factory.mktemp('watermap-test')
    old = {name: os.environ.get(name) for name in ('SECRET_KEY', 'WATERMAP_DATA_DIR')}
    os.environ['SECRET_KEY'] = secrets.token_urlsafe(48)
    os.environ['WATERMAP_DATA_DIR'] = str(directory)
    module = importlib.import_module('app')
    module.app.config.update(TESTING=True, PUBLIC_BASE_URL='https://watermap.example')
    @module.app.teardown_request
    def clear_request_caches(error):
        # An outer DB app context keeps `g` alive in tests. Production creates a
        # fresh app context per request, including Flask-Login and CSRF caches.
        from flask import g
        g.pop('_login_user', None)
        g.pop('csrf_token', None)
        g.pop('csp_nonce', None)
    yield module
    module.executor.shutdown(wait=True)
    module.mail_executor.shutdown(wait=True)
    for name, value in old.items():
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value


@pytest.fixture
def module(watermap):
    with watermap.app.app_context():
        watermap.db.drop_all()
        watermap.db.create_all()
        watermap.limiter.reset()
        for folder in (watermap.UPLOAD_FOLDER, watermap.PAGE_FOLDER):
            for path in Path(folder).iterdir():
                if path.is_dir():
                    shutil.rmtree(path)
        with watermap.status_lock:
            watermap.processing_status.clear()
            watermap.task_owners.clear()
        yield watermap
        watermap.db.session.remove()


@pytest.fixture
def client(module):
    return module.app.test_client()


def csrf(client, path='/'):
    import re
    # Each browser request needs its own `g`; tests keep an outer DB context.
    with client.application.app_context():
        response = client.get(path)
    assert response.status_code == 200
    return re.search(rb'name="csrf_token" value="([^"]+)"', response.data).group(1).decode()


def post(client, path, **data):
    token = csrf(client, '/register' if path == '/register' else '/')
    return client.post(path, data={'csrf_token': token, **data})


def account(module, email='owner@example.test'):
    user = module.User(name='Test', email=email)
    user.set_password('valid-password-123')
    module.db.session.add(user)
    module.db.session.commit()
    return user


def login(client, email='owner@example.test', password='valid-password-123'):
    return post(client, '/login', email=email, password=password)


@pytest.fixture
def owner(module, client):
    user = account(module)
    assert login(client).status_code == 302
    return user
