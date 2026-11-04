import socket
from unittest.mock import Mock, patch

import pytest

from remote_fetch import fetch_public_url


def dns(address='1.1.1.1'):
    return [(socket.AF_INET6 if ':' in address else socket.AF_INET, socket.SOCK_STREAM, 6, '', (address,443))]


def response(content=b'gpx', status=200, headers=None):
    result = Mock(status=status, headers=headers or {})
    result.read1.side_effect = [content, b'']
    return result


@pytest.mark.parametrize('address', ['127.0.0.1','169.254.169.254','10.0.0.1','172.17.0.1','192.168.1.1','::1','::ffff:127.0.0.1','224.0.0.1','ff02::1','0.0.0.0'])
def test_private_and_multicast_blocked(address):
    with patch('remote_fetch.socket.getaddrinfo', return_value=dns(address)), patch('remote_fetch.urllib3.HTTPSConnectionPool') as pool:
        with pytest.raises(ValueError):
            fetch_public_url('https://example.test/trace')
        pool.assert_not_called()


@pytest.mark.parametrize('url', ['file:///etc/passwd','https://user:password@example.test/','http://example.test:8080/','http://example.test:abc/'])
def test_disallowed_urls(url):
    with pytest.raises(ValueError):
        fetch_public_url(url)


def test_dns_pinning_and_tls_verification():
    with patch('remote_fetch.socket.getaddrinfo', return_value=dns()) as lookup, patch('remote_fetch.urllib3.HTTPSConnectionPool') as pool:
        pool.return_value.request.return_value = response()
        assert fetch_public_url('https://example.test/trace').content == b'gpx'
        assert pool.call_args.args == ('1.1.1.1',443)
        assert pool.call_args.kwargs['server_hostname'] == 'example.test'
        assert pool.call_args.kwargs['assert_hostname'] == 'example.test'
        assert lookup.call_count == 1
        pool.return_value.request.return_value.close.assert_called_once()
        pool.return_value.close.assert_called_once()


def test_private_redirect_blocked():
    with patch('remote_fetch.socket.getaddrinfo', side_effect=[dns(),dns('127.0.0.1')]), patch('remote_fetch.urllib3.HTTPSConnectionPool') as pool:
        pool.return_value.request.return_value = response(status=302,headers={'Location':'http://localhost/secret'})
        with pytest.raises(ValueError, match='privées'):
            fetch_public_url('https://example.test/trace')


@pytest.mark.parametrize('headers,content', [({'Content-Length':'101'},b'a'),({},b'a'*101),({'Content-Encoding':'gzip'},b'a')])
def test_size_and_compression_limits(headers,content):
    with patch('remote_fetch.socket.getaddrinfo', return_value=dns()), patch('remote_fetch.urllib3.HTTPSConnectionPool') as pool:
        pool.return_value.request.return_value = response(content,headers=headers)
        with pytest.raises(ValueError):
            fetch_public_url('https://example.test/trace',max_bytes=100)


def test_download_deadline():
    with patch('remote_fetch.socket.getaddrinfo', return_value=dns()), patch('remote_fetch.urllib3.HTTPSConnectionPool') as pool, patch('remote_fetch.time.monotonic',side_effect=[0,0,0,46]):
        pool.return_value.request.return_value = response()
        with pytest.raises(ValueError, match='expiré'):
            fetch_public_url('https://example.test/trace')


def test_redirect_limit():
    with patch('remote_fetch.socket.getaddrinfo', return_value=dns()), patch('remote_fetch.urllib3.HTTPSConnectionPool') as pool:
        pool.return_value.request.return_value = response(status=302,headers={'Location':'/loop'})
        with pytest.raises(ValueError, match='redirections'):
            fetch_public_url('https://example.test/trace')
