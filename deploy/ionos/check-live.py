"""Check authenticated access over the real HTTPS endpoint without revealing a session."""
from pathlib import Path
import sys

sys.path.insert(0, '/app')
import requests
from wsgi import app
from app import db, User, PAGE_FOLDER, UPLOAD_FOLDER

base = 'https://watermap.dolisi.fr'
with app.app_context():
    owner = db.session.execute(db.select(User).order_by(User.id)).scalars().first()
    assert owner is not None
    user_id = owner.id
    cookie = app.session_interface.get_signing_serializer(app).dumps({'_user_id': str(user_id), '_fresh': True})
pages = sorted((Path(PAGE_FOLDER) / str(user_id)).glob('*.html'))
uploads = sorted((Path(UPLOAD_FOLDER) / str(user_id)).glob('*.gpx'))
assert len(pages) >= 9 and len(uploads) >= 10
anonymous = requests.get(base + '/generated/' + pages[0].stem, allow_redirects=False, timeout=10)
assert anonymous.status_code == 302 and '/login' in anonymous.headers['Location']
session = requests.Session()
session.cookies.set(app.config['SESSION_COOKIE_NAME'], cookie, domain='watermap.dolisi.fr', secure=True)
for path in ('/', '/generated/' + pages[0].stem, f'/uploads/{user_id}/' + uploads[0].name):
    response = session.get(base + path, allow_redirects=False, timeout=10)
    assert response.status_code == 200, response.status_code
assert session.get(base + '/_health', timeout=10).status_code == 404
print('HTTPS réel : accès authentifié, carte et GPX migrés OK ; accès anonyme bloqué.')
