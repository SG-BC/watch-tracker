"""Redaction of anything sensitive from log text. Pure Python (no Kodi imports) so it is unit-tested.

Applied to EVERY log line (memory buffer, kodi.log) and again to the whole text before an export
or upload. Goal: nothing that identifies a server, an account or a credential survives, while
titles, folder/file names and ids (needed to debug matching) stay.
"""
import re
from urllib.parse import parse_qsl, unquote, urlsplit

_SENSITIVE = ('token', 'key', 'secret', 'auth', 'pass', 'sig', 'identifier', 'session', 'code',
              'cookie', 'bearer', 'credential', 'user', 'email', 'account')

URL_RE = re.compile(r'(?P<url>[A-Za-z][A-Za-z0-9+.\-]*://[^\s\'"<>)\]]+)')
KV_RE = re.compile(
    r'(?i)\b(x-api-key|x-plex-token|x-plex-client-identifier|access_token|refresh_token|device_code|client_secret|'
    r'api[_-]?key|apikey|password|passwd|authorization|token|code_verifier)\b(["\']?\s*[:=]\s*["\']?)'
    r'[^\s&,"\'}\]]+')
BEARER_RE = re.compile(r'(?i)\bBearer\s+[A-Za-z0-9._\-~+/=]+')
SIMKL_TOKEN_RE = re.compile(r'simkl_(?:at|rt)_[A-Za-z0-9]+')
UUID_RE = re.compile(r'\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b')
IPV4_RE = re.compile(r'\b(?:\d{1,3}\.){3}\d{1,3}\b(?::\d{2,5})?')
IPV6_RE = re.compile(r'\b(?:[0-9A-Fa-f]{1,4}:){3,7}[0-9A-Fa-f]{1,4}\b')
PLEX_HOST_RE = re.compile(r'(?i)\b[\w\-]+(?:\.[\w\-]+)*\.plex\.(?:direct|tv)\b(?::\d{2,5})?')
HOSTPORT_RE = re.compile(r'(?i)\b[a-z0-9\-]+(?:\.[a-z0-9\-]+)+:\d{2,5}\b')
EMAIL_RE = re.compile(r'[\w.+\-]+@[\w\-]+(?:\.[\w\-]+)+')
USERPATH_RE = re.compile(r'(?i)(/home/|/Users/|[\\/]Users[\\/])[^/\\\s\'"]+')
HEX_RE = re.compile(r'(?<![A-Za-z0-9])[0-9a-fA-F]{20,}(?![A-Za-z0-9])')
MIXED_TOKEN_RE = re.compile(
    r'(?<![A-Za-z0-9])(?=[A-Za-z0-9]*[a-z])(?=[A-Za-z0-9]*[A-Z])(?=[A-Za-z0-9]*\d)[A-Za-z0-9]{20,}(?![A-Za-z0-9])')


def _is_sensitive(name):
    name = name.lower()
    return any(s in name for s in _SENSITIVE)


def mask_url(url):
    """scheme://<host>/…/last/three/parts?name=<hidden>  (plugin:// keeps its addon id and safe values)."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return '<url>'
    segs = [unquote(s) for s in parts.path.split('/') if s]
    tail = segs[-3:]
    path = ('…/' if len(segs) > 3 else '/') + '/'.join(tail) if tail else ''
    if tail and parts.path.endswith('/'):
        path += '/'
    query = ''
    if parts.query:
        pairs = parse_qsl(parts.query, keep_blank_values=True)
        if parts.scheme.lower() == 'plugin':
            shown = ['{}={}'.format(k, '<hidden>' if _is_sensitive(k) or len(v) > 40 else v) for k, v in pairs[:8]]
        else:
            shown = ['{}=<hidden>'.format(k) for k, _ in pairs[:6]]
        query = '?' + '&'.join(shown)
    host = parts.netloc.split('@')[-1] if parts.scheme.lower() == 'plugin' else '<host>'
    return '{}://{}{}{}'.format(parts.scheme, host, path, query)


def text(value):
    s = str(value)
    s = URL_RE.sub(lambda m: mask_url(m.group('url')), s)
    s = KV_RE.sub(lambda m: '{}{}<hidden>'.format(m.group(1), m.group(2)), s)
    s = BEARER_RE.sub('Bearer <hidden>', s)
    s = SIMKL_TOKEN_RE.sub('<simkl-token>', s)
    s = UUID_RE.sub('<id>', s)
    s = EMAIL_RE.sub('<email>', s)
    s = PLEX_HOST_RE.sub('<host>', s)
    s = IPV4_RE.sub('<ip>', s)
    s = IPV6_RE.sub('<ip>', s)
    s = HOSTPORT_RE.sub('<host>', s)
    s = USERPATH_RE.sub(lambda m: m.group(1) + '<user>', s)
    s = HEX_RE.sub('<id>', s)
    s = MIXED_TOKEN_RE.sub('<token>', s)
    return s


def path(value):
    """A file path or URL for display: only the last three parts (show folder / season / file)."""
    p = str(value or '')
    if '://' in p:
        return text(p)
    segs = [s for s in re.split(r'[\\/]+', p) if s]
    tail = segs[-3:]
    shown = ('…/' if len(segs) > 3 else ('/' if p[:1] in '/\\' else '')) + '/'.join(tail)
    return text(shown)
