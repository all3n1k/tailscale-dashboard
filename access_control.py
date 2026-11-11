"""Network boundary for local dashboards and explicitly configured remote access."""
import base64
import ipaddress
import secrets
from urllib.parse import urlsplit

class AccessPolicy:
    def __init__(self, environ, default_host='127.0.0.1'):
        self.host = environ.get('DASHBOARD_HOST', default_host)
        self.token = environ.get('DASHBOARD_TOKEN', '')
        self.allowed_hosts = {h.strip().lower().rstrip('.') for h in environ.get('DASHBOARD_ALLOWED_HOSTS', 'localhost,127.0.0.1,::1').split(',') if h.strip()}
        if self.host not in ('0.0.0.0', '::'):
            self.allowed_hosts.add(self.host.lower().rstrip('.'))
        try:
            self.local = ipaddress.ip_address(self.host).is_loopback
        except ValueError:
            self.local = self.host.lower() == 'localhost'

    def validate_configuration(self):
        if not self.local and len(self.token) < 24:
            raise ValueError('Remote dashboard access requires DASHBOARD_TOKEN with at least 24 characters and explicit DASHBOARD_ALLOWED_HOSTS')

    def check(self, headers):
        host = headers.get('host', '')
        try:
            parsed = urlsplit('//'+host)
            hostname = (parsed.hostname or '').lower().rstrip('.')
            _ = parsed.port
            if not hostname or parsed.username or parsed.password or parsed.path or parsed.query or parsed.fragment or hostname not in self.allowed_hosts:
                return 403, 'Host is not allowed'
        except ValueError:
            return 403, 'Invalid Host'
        if headers.get('sec-fetch-site') in ('cross-site', 'same-site'):
            return 403, 'Cross-site requests are not allowed'
        origin = headers.get('origin')
        if origin:
            try:
                parsed_origin = urlsplit(origin)
            except ValueError:
                return 403, 'Invalid Origin'
            if parsed_origin.scheme not in ('http', 'https') or parsed_origin.netloc.lower() != host.lower() or parsed_origin.path or parsed_origin.query or parsed_origin.fragment:
                return 403, 'Cross-origin requests are not allowed'
        try:
            self.validate_configuration()
        except ValueError:
            return 503, 'Remote dashboard access is not configured'
        if self.token:
            auth = headers.get('authorization', '')
            provided = ''
            if auth.startswith('Bearer '):
                provided = auth[7:]
            elif auth.startswith('Basic '):
                try:
                    provided = base64.b64decode(auth[6:], validate=True).decode('utf-8').split(':', 1)[1]
                except (ValueError, UnicodeError, IndexError):
                    pass
            if not secrets.compare_digest(provided.encode('utf-8'), self.token.encode('utf-8')):
                return 401, 'Dashboard authentication required'
        return None
