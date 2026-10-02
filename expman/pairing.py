"""Portable worker enrollment; never include administrator credentials."""
import ipaddress
import re
import socket
import json
import base64
from urllib.parse import urlsplit


def validate_origin(url):
    if not isinstance(url, str) or len(url) > 2048 or any(c.isspace() for c in url):
        raise ValueError('Invalid controller URL')
    parsed = urlsplit(url)
    if (parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username is not None
            or parsed.password is not None or parsed.path not in ('', '/') or parsed.query or parsed.fragment):
        raise ValueError('Use a controller origin such as http://computer-name:8765')
    parsed.port
    return url.rstrip('/')


def parse_connection_text(text):
    if not isinstance(text, str) or len(text) > 16384:
        raise ValueError('连接凭证应小于 16 KiB')
    text = text.strip()
    if text.startswith('exlab://connect/'):
        raw = text[len('exlab://connect/'):]
        text = base64.urlsafe_b64decode(raw + '=' * (-len(raw) % 4)).decode('utf-8')
    return json.loads(text)


def validate_pairing(value):
    if not isinstance(value, dict) or value.get('schema', 1) != 1:
        raise ValueError('Invalid pairing file / 配对文件格式不正确')
    node_id = value.get('node_id', '')
    token = value.get('token', '')
    url = value.get('hub_url', '')
    if not isinstance(node_id, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,63}', node_id):
        raise ValueError('Node name: use 1–64 letters, digits, _, . or -')
    if not isinstance(token, str) or not re.fullmatch(r'[A-Za-z0-9_-]{20,512}', token):
        raise ValueError('Missing or invalid worker credential')
    if not isinstance(url, str) or len(url) > 2048 or any(c.isspace() for c in url):
        raise ValueError('Invalid controller URL')
    parsed = urlsplit(url)
    if (parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username is not None
            or parsed.password is not None or parsed.path not in ('', '/') or parsed.query or parsed.fragment):
        raise ValueError('Use a controller origin such as http://computer-name:8765')
    parsed.port  # Reject invalid/out-of-range ports before writing credentials.
    result = {'schema': 1, 'node_id': node_id, 'hub_url': validate_origin(url), 'token': token}
    if 'hub_urls' in value:
        origins = value['hub_urls']
        if not isinstance(origins, list) or len(origins) > 8:
            raise ValueError('Invalid Center address list')
        result['hub_urls'] = list(dict.fromkeys([result['hub_url'], *map(validate_origin, origins)]))
    return result


def address_candidates(port):
    """Suggestions only: routing still has to be tested from the worker."""
    addresses = set()
    try:
        for item in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            address = ipaddress.ip_address(item[4][0])
            if not address.is_loopback and (address.is_private or address in ipaddress.ip_network('100.64.0.0/10')):
                addresses.add(str(address))
    except OSError:
        pass
    def key(address):
        return (0 if ipaddress.ip_address(address) in ipaddress.ip_network('100.64.0.0/10') else 1, address)
    return [f'http://{address}:{port}' for address in sorted(addresses, key=key)]
