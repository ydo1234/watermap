"""Exercise the production image against a disposable copy of the migrated data."""
import io
import os
from pathlib import Path
import re
import secrets
import shutil
import socket
import sys
import tempfile
import time
from unittest.mock import patch

sys.path.insert(0, "/app")
temporary = tempfile.TemporaryDirectory(prefix="watermap-smoke-")
data = Path(temporary.name)
for name in ("db", "pages", "uploads"):
    shutil.copytree(Path("/app") / name, data / name)
os.environ["WATERMAP_DATA_DIR"] = str(data)

from wsgi import app
import app as module
from remote_fetch import fetch_public_url

BASE = "https://watermap.dolisi.fr"
app.config.update(TESTING=True)
client = app.test_client()
passed = []


def get(path, browser=client):
    return browser.get(path, base_url=BASE)


def token(path="/", browser=client):
    response = get(path, browser)
    assert response.status_code == 200, (path, response.status_code)
    return re.search(rb'name="csrf_token" value="([^"]+)"', response.data).group(1).decode()


def post(path, payload, browser=client):
    return browser.post(path, data=payload, base_url=BASE, headers={"Referer": BASE + "/"})


assert get("/").status_code == 200
assert get("/generated/example").status_code == 302
assert post("/login", {"email": "test@example.invalid", "password": "invalid"}).status_code == 400
passed.append("anonymous access and CSRF protection")

password = secrets.token_urlsafe(20)
email = "deployment-test@example.invalid"
assert post("/register", {"csrf_token": token("/register"), "name": "Deployment test", "email": email, "password": password}).status_code == 302
login = post("/login", {"csrf_token": token("/login"), "email": email, "password": password})
assert login.status_code == 302
cookie = login.headers.get("Set-Cookie", "")
assert "Secure" in cookie and "HttpOnly" in cookie and "SameSite=Lax" in cookie
assert get("/").status_code == 200
with app.app_context():
    owner = module.db.session.execute(module.db.select(module.User).where(module.User.email != email)).scalar_one()
    owner_id = owner.id
    test_id = module.db.session.execute(module.db.select(module.User).where(module.User.email == email)).scalar_one().id
passed.append("registration, password hashing, login, secure cookies")

owner_pages = sorted((data / "pages" / str(owner_id)).glob("*.html"))
owner_uploads = sorted((data / "uploads" / str(owner_id)).glob("*.gpx"))
assert len(owner_pages) == 9 and len(owner_uploads) == 10
assert get("/generated/" + owner_pages[0].stem).status_code == 403
assert get(f"/uploads/{owner_id}/" + owner_uploads[0].name).status_code == 403
owner_client = app.test_client()
with owner_client.session_transaction(base_url=BASE) as session:
    session["_user_id"] = str(owner_id)
    session["_fresh"] = True
assert get("/", owner_client).status_code == 200
assert get("/generated/" + owner_pages[0].stem, owner_client).status_code == 200
assert get(f"/uploads/{owner_id}/" + owner_uploads[0].name, owner_client).status_code == 200
assert "/uploads/" + str(owner_id) + "/" in owner_pages[0].read_text()
passed.append("all migrated files, legacy links, account isolation")

assert len(module.parse_water_points()) > 0 and len(module.parse_toilets()) > 0
point = module.parse_water_points()[0]
lat, lon = point["lat"], point["lon"]
gpx = f'''<?xml version="1.0"?><gpx version="1.1" xmlns="http://www.topografix.com/GPX/1/1"><trk><trkseg><trkpt lat="{lat}" lon="{lon}"/><trkpt lat="{lat + 0.0001}" lon="{lon + 0.0001}"/></trkseg></trk></gpx>'''.encode()
upload = post("/upload", {"csrf_token": token(), "page_name": "deployment-smoke", "gpx_file": (io.BytesIO(gpx), "trace.gpx")})
assert upload.status_code == 202, upload.data
task_id = upload.json["task_id"]
for _ in range(300):
    status = get("/status/" + task_id)
    assert status.status_code == 200
    if status.json["done"]:
        break
    time.sleep(0.1)
assert status.json["done"] and not status.json["error"], status.json
assert get("/status/" + task_id, owner_client).status_code == 404
page = get("/generated/deployment-smoke")
assert page.status_code == 200 and f"/uploads/{test_id}/deployment-smoke.gpx".encode() in page.data
assert get(f"/uploads/{test_id}/deployment-smoke.gpx").data == gpx
assert post("/delete/deployment-smoke", {"csrf_token": token()}).status_code == 302
assert not (data / "pages" / str(test_id) / "deployment-smoke.html").exists()
passed.append("reference datasets, GPX processing, map, download, delete, task ownership")

with patch.object(module.mail, "send") as send:
    assert post("/forgot-password", {"csrf_token": token("/forgot-password"), "email": email}).status_code == 200
    send.assert_called_once()
    assert BASE + "/reset-password/" in send.call_args.args[0].body
with app.app_context():
    user = module.db.session.execute(module.db.select(module.User).where(module.User.email == email)).scalar_one()
    reset_token = user.get_reset_token()
reset_path = "/reset-password/" + reset_token
new_password = secrets.token_urlsafe(20)
assert post(reset_path, {"csrf_token": token(reset_path), "password": new_password}).status_code == 302
get("/logout")
assert post("/login", {"csrf_token": token("/login"), "email": email, "password": new_password}).status_code == 302
assert get("/reset-password/invalid").status_code == 302
passed.append("HTTPS password reset links and password change; no email sent")

for address in ("127.0.0.1", "169.254.169.254", "10.0.0.1", "172.17.0.1", "192.168.1.1", "::1"):
    result = [(socket.AF_INET6 if ":" in address else socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 80))]
    with patch("remote_fetch.socket.getaddrinfo", return_value=result), patch("remote_fetch.urllib3.HTTPConnectionPool") as pool:
        try:
            fetch_public_url("http://example.invalid/")
            raise AssertionError("Private address accepted")
        except ValueError:
            pass
        pool.assert_not_called()
public_dns = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("1.1.1.1", 443))]
with patch("remote_fetch.socket.getaddrinfo", return_value=public_dns), patch("remote_fetch.urllib3.HTTPSConnectionPool") as pool:
    response = pool.return_value.request.return_value
    response.status = 200
    response.read.return_value = b"gpx"
    assert fetch_public_url("https://example.invalid/trace").content == b"gpx"
    assert pool.call_args.args == ("1.1.1.1", 443)
    assert pool.call_args.kwargs["server_hostname"] == "example.invalid"
    assert pool.call_args.kwargs["assert_hostname"] == "example.invalid"
with patch("remote_fetch.socket.getaddrinfo", side_effect=[public_dns, [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 80))]]), patch("remote_fetch.urllib3.HTTPSConnectionPool") as pool:
    response = pool.return_value.request.return_value
    response.status = 302
    response.headers = {"Location": "http://localhost/secret"}
    try:
        fetch_public_url("https://example.invalid/")
        raise AssertionError("Private redirect accepted")
    except ValueError:
        pass
passed.append("private network, metadata, redirect and DNS-rebinding protection")

for _ in range(11):
    limited = post("/login", {"csrf_token": token(), "email": "missing@example.invalid", "password": "invalid"})
assert limited.status_code == 429
passed.append("login rate limiting")

for item in passed:
    print("PASS:", item)
temporary.cleanup()
