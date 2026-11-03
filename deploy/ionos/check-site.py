#!/usr/bin/env python3
import urllib.request

with urllib.request.urlopen('https://watermap.dolisi.fr/login', timeout=20) as response:
    assert response.status == 200
    assert 'max-age=' in response.headers.get('Strict-Transport-Security', '')
    assert b'name="csrf_token"' in response.read(100000)
print('Watermap HTTPS et page de connexion : OK')
