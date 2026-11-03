"""Fetch public GPX/Google URLs without allowing access to private networks."""
import ipaddress
import socket
from typing import NamedTuple
from urllib.parse import urljoin, urlsplit, urlunsplit

import urllib3


class RemoteResponse(NamedTuple):
    url: str
    content: bytes


def fetch_public_url(url, method='GET', timeout=20, max_bytes=100 * 1024 * 1024):
    for _ in range(6):
        parsed = urlsplit(url)
        if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError('Le lien doit être une adresse HTTP ou HTTPS publique.')
        port = parsed.port or (443 if parsed.scheme == 'https' else 80)
        if port not in (80, 443):
            raise ValueError('Le lien doit utiliser un port HTTP ou HTTPS standard.')
        hostname = parsed.hostname.encode('idna').decode('ascii')
        addresses = socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM)
        if not addresses or any(not ipaddress.ip_address(item[4][0]).is_global for item in addresses):
            raise ValueError('Les adresses locales et privées ne sont pas autorisées.')
        # Connect directly to the checked IP so a second DNS lookup cannot change the destination.
        addresses.sort(key=lambda item: item[0] != socket.AF_INET)
        address = addresses[0][4][0]
        options = {'timeout': urllib3.Timeout(connect=5, read=timeout), 'maxsize': 1}
        if parsed.scheme == 'https':
            pool = urllib3.HTTPSConnectionPool(address, port, server_hostname=hostname, assert_hostname=hostname, **options)
        else:
            pool = urllib3.HTTPConnectionPool(address, port, **options)
        host_header = f'[{hostname}]' if ':' in hostname else hostname
        if port != (443 if parsed.scheme == 'https' else 80):
            host_header += f':{port}'
        path = urlunsplit(('', '', parsed.path or '/', parsed.query, ''))
        response = None
        try:
            response = pool.request(method, path, headers={'Host': host_header, 'User-Agent': 'WaterMap/1.0'},
                                    redirect=False, retries=False, preload_content=False)
            if response.status in (301, 302, 303, 307, 308):
                location = response.headers.get('Location')
                if not location:
                    raise ValueError('La redirection distante ne contient pas de destination.')
                url = urljoin(url, location)
                continue
            if response.status >= 400:
                raise ValueError(f'Le serveur distant a répondu HTTP {response.status}.')
            content = b'' if method == 'HEAD' else response.read(max_bytes + 1, decode_content=True)
            if len(content) > max_bytes:
                raise ValueError('Le fichier distant dépasse la taille maximale autorisée.')
            return RemoteResponse(url, content)
        finally:
            if response is not None:
                response.close()
            pool.close()
    raise ValueError('Le lien comporte trop de redirections.')
