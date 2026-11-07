"""plex.tv sign-in, the account's server list, and calls to Plex Media Servers.

* Sign-in: plex.tv's PIN flow (legacy tokens): POST /api/v2/pins -> the user enters the short code at plex.tv/link ->
  poll GET /api/v2/pins/{id} until `authToken` is set. The token is kept in the profile folder (plex_auth.json, through
  auth.Account like every other service), never in settings or code.
* X-Plex-Client-Identifier is "an opaque identifier unique to the client": a UUID made once per install
  (plex_client.json). Nothing is registered with Plex.
* Each server has its own token and connections (GET clients.plex.tv/api/v2/resources). Local connections are tried
  first, relay last ("bandwidth on relay connections is limited").
* Every server keeps its own watch state, ratings and history, so each chosen server is read and written by itself.
* Reads are paged (X-Plex-Container-Start / -Size), paced, and back off; a server that keeps failing is skipped.
Never logs addresses or tokens: log lines name the server and the path only (and go through redact anyway)."""
import json
import os
import time
import uuid

from . import auth, cfg, log, net, services_config

PLEX_TV = 'https://plex.tv'
CLIENTS = 'https://clients.plex.tv'
LINK_PAGE = 'https://plex.tv/link'
LIBRARY = 'com.plexapp.plugins.library'
PAGE = 200                 # rows per page; halved (down to MIN_PAGE) when a page times out
MIN_PAGE = 25
PACE = 0.5                 # seconds between two requests to the same server
TIMEOUT = 20
PROBE_TIMEOUT = 5
KINDS = ('movie', 'show')  # library types we sync

WARNING = ('Plex servers can be remote, relayed or shared, and each library is read separately. Syncing several '
           'servers or libraries can take a long time, and a server may slow down or refuse requests if asked too '
           'much. Choose only the servers and libraries you need.')
NONE_CHOSEN = 'Choose Plex servers and libraries in the settings first (Accounts > Plex).'


class ServerDown(Exception):
    pass


# ------------------------------------------------------------------------------ identity + headers
def _profile(name):
    return os.path.join(cfg.profile_dir(), name)


def _load(name):
    try:
        with open(_profile(name), 'r', encoding='utf-8') as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _store(name, data):
    try:
        with open(_profile(name), 'w', encoding='utf-8') as f:
            json.dump(data, f)
    except OSError as e:
        log.warning('could not save {}: {}'.format(name, e))


def client_id():
    data = _load('plex_client.json')
    if not data.get('id'):
        data['id'] = str(uuid.uuid4())
        _store('plex_client.json', data)
    return data['id']


def headers(token=None):
    h = {'Accept': 'application/json', 'User-Agent': auth.user_agent(),
         'X-Plex-Client-Identifier': client_id(), 'X-Plex-Product': services_config.SERVICES['plex']['product'],
         'X-Plex-Version': cfg.version() or '1.0.0', 'X-Plex-Platform': 'Kodi', 'X-Plex-Device-Name': 'Kodi'}
    if token:
        h['X-Plex-Token'] = token
    return h


# ------------------------------------------------------------------------------ plex.tv account
def account():
    return auth.get('plex')


def logged_in():
    return account().is_logged_in()


def _token():
    return account().access_token()


def start_pin():
    """-> {'id', 'code', 'expires_in'}. Raises auth.LoginError."""
    res = net.request('POST', PLEX_TV + '/api/v2/pins', headers=headers())
    if res.ok and res.body.get('id') and res.body.get('code'):
        return {'id': res.body['id'], 'code': res.body['code'], 'expires_in': int(res.body.get('expiresIn') or 900)}
    raise auth.LoginError('Plex did not return a login code ({}).'.format(net.summarize(res)))


def poll_pin(pin):
    """One poll: 'ok' | 'pending' | 'expired' | 'error'."""
    res = net.request('GET', '{}/api/v2/pins/{}'.format(PLEX_TV, pin['id']), headers=headers())
    token = res.body.get('authToken') if res.ok else None
    if token:
        account()._save({'access_token': token, 'refresh_token': None, 'scope': '', 'expires_at': None, 'username': ''})
        return 'ok'
    if res.ok or res.network_error:
        return 'pending'
    if res.status in (404, 410):
        return 'expired'
    log.warning('plex login poll: {}'.format(net.summarize(res)))
    return 'error'


def fetch_username():
    token = _token()
    if not token:
        return ''
    res = net.request('GET', PLEX_TV + '/api/v2/user', headers=headers(token))
    log.debug('plex GET /api/v2/user -> {}'.format(net.summarize(res)))
    name = (res.body.get('username') or res.body.get('title') or '') if res.ok else ''
    if name:
        account().set_username(name)
    return name


def check():
    """Status row: Connected as @username / Disconnected."""
    from . import status
    if not logged_in():
        status.set_status('plex', False)
        return
    res = net.request('GET', PLEX_TV + '/api/v2/user', headers=headers(_token()))
    if res.ok:
        name = res.body.get('username') or res.body.get('title')
        if name:
            account().set_username(name)
        status.set_status('plex', True)
    elif res.auth_error or res.network_error:
        status.set_status('plex', False)


def logout():
    account().logout()


def servers():
    """Every Plex Media Server the account can reach -> [{'id','name','owned','token','connections'}], or None."""
    token = _token()
    if not token:
        return None
    res = net.request('GET', CLIENTS + '/api/v2/resources', params={'includeHttps': 1, 'includeRelay': 1},
                      headers=headers(token))
    log.debug('plex GET /api/v2/resources -> {}'.format(net.summarize(res)))
    if not res.ok or not isinstance(res.raw, list):
        return None
    out = []
    for r in res.raw:
        if not isinstance(r, dict) or 'server' not in str(r.get('provides') or ''):
            continue
        out.append({'id': r.get('clientIdentifier'), 'name': r.get('name') or 'Plex server',
                    'owned': bool(r.get('owned')), 'token': r.get('accessToken') or token,
                    'connections': [c for c in (r.get('connections') or []) if isinstance(c, dict) and c.get('uri')]})
    return out


# ------------------------------------------------------------------------------ the user's choice
def chosen():
    """-> {'servers': [{'id','name','libraries': [{'key','title','type'}]}], 'warned': bool}"""
    data = _load('plex_libraries.json')
    data['servers'] = [s for s in (data.get('servers') or []) if isinstance(s, dict) and s.get('libraries')]
    return data


def save_choice(server_list):
    data = chosen()
    data['servers'] = server_list
    _store('plex_libraries.json', data)


def warned():
    return bool(chosen().get('warned'))


def set_warned():
    data = _load('plex_libraries.json')
    data['warned'] = True
    _store('plex_libraries.json', data)


def ready():
    """None when Plex can be used, else the reason."""
    if not logged_in():
        return 'Log in to Plex in the settings first.'
    if not chosen()['servers']:
        return NONE_CHOSEN
    return None


# ------------------------------------------------------------------------------ one server
def _int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


class Server:
    def __init__(self, info):
        self.id, self.name, self.owned = info.get('id'), info.get('name') or 'Plex server', info.get('owned', False)
        self.token, self.connections = info.get('token'), info.get('connections') or []
        self.uri, self.kind, self.ms = None, None, None
        self._last = 0.0

    @staticmethod
    def _rank(c):
        return 0 if c.get('local') and not c.get('relay') else (2 if c.get('relay') else 1)

    def connect(self):
        """Try the connections (local, then remote, then relay). -> True when one answers."""
        for c in sorted(self.connections, key=self._rank):
            t = time.time()
            res = net.request('GET', c['uri'].rstrip('/') + '/identity', headers=headers(self.token), timeout=PROBE_TIMEOUT)
            if res.ok:
                self.uri = c['uri'].rstrip('/')
                self.kind = ('local', 'remote', 'relay')[self._rank(c)]
                self.ms = int((time.time() - t) * 1000)
                return True
        log.debug('plex {}: no connection answered'.format(self.name))
        return False

    def describe(self):
        if not self.uri:
            return '{} - {} - no answer'.format(self.name, 'owned' if self.owned else 'shared')
        warn = ' (slow)' if self.kind == 'relay' or (self.ms or 0) > 1500 else ''
        return '{} - {} - {} - {} ms{}'.format(self.name, 'owned' if self.owned else 'shared', self.kind, self.ms, warn)

    def _pace(self):
        wait = PACE - (time.time() - self._last)
        if wait > 0:
            time.sleep(wait)
        self._last = time.time()

    def call(self, method, path, params=None, start=None, size=None):
        if not self.uri:
            raise ServerDown('{}: not connected'.format(self.name))
        h = headers(self.token)
        if start is not None:
            h['X-Plex-Container-Start'], h['X-Plex-Container-Size'] = str(start), str(size)
        self._pace()
        res = net.request(method, self.uri + path, params=params, headers=h, timeout=TIMEOUT)
        if res.status == 429:                                   # asked too much: wait once, then try again
            time.sleep(min(30, res.retry_after or 5))
            res = net.request(method, self.uri + path, params=params, headers=h, timeout=TIMEOUT)
        log.debug('plex {} {} {} -> {}'.format(self.name, method, path, 'network error' if res.network_error else res.status))
        return res

    def pages(self, path, params=None):
        """Yield every row of a paged list (Metadata or Directory). Raises ServerDown."""
        start, size = 0, PAGE
        while True:
            res = self.call('GET', path, params, start, size)
            if res.network_error or (res.status or 0) >= 500:  # slow or struggling: a smaller page, once
                size = max(MIN_PAGE, size // 2)
                res = self.call('GET', path, params, start, size)
            if not res.ok:
                raise ServerDown('{}: {}'.format(self.name, 'no answer' if res.network_error else 'HTTP {}'.format(res.status)))
            mc = res.body.get('MediaContainer') or {}
            rows = mc.get('Metadata') or mc.get('Directory') or []
            for row in rows:
                yield row
            start += len(rows)
            total = _int(res.headers.get('x-plex-container-total-size'))
            if total is None:
                total = _int(mc.get('totalSize'))
            if not rows or (total is not None and start >= total) or (total is None and len(rows) < size):
                return

    def sections(self):
        """Movie and show libraries -> [{'key','title','type'}]. Raises ServerDown."""
        res = self.call('GET', '/library/sections')
        if not res.ok:
            raise ServerDown('{}: {}'.format(self.name, 'no answer' if res.network_error else 'HTTP {}'.format(res.status)))
        rows = (res.body.get('MediaContainer') or {}).get('Directory') or []
        return [{'key': str(d.get('key')), 'title': d.get('title') or '?', 'type': d.get('type')}
                for d in rows if isinstance(d, dict) and d.get('type') in KINDS]

    # ---- writes
    def scrobble(self, key):
        return self.call('PUT', '/:/scrobble', {'identifier': LIBRARY, 'key': key})

    def unscrobble(self, key):
        return self.call('PUT', '/:/unscrobble', {'identifier': LIBRARY, 'key': key})

    def rate(self, key, value, rated_at=None):
        params = {'identifier': LIBRARY, 'key': key, 'rating': value}
        if rated_at and rated_at < time.time():                # a future ratedAt is refused (400)
            params['ratedAt'] = int(rated_at)
        return self.call('PUT', '/:/rate', params)


def connect_chosen(ui=None):
    """-> ([(Server, [library, ...])], [(name, reason)]): the chosen servers that answer, and the skipped ones."""
    choice = chosen()['servers']
    known = {s['id']: s for s in (servers() or [])}
    if not known and choice:
        return [], [(c.get('name') or 'Plex', 'plex.tv did not answer') for c in choice]
    ready_, skipped = [], []
    for n, c in enumerate(choice):
        if ui is not None:
            ui.update(2 + int(8 * n / max(1, len(choice))), 'Connecting to {}...'.format(c.get('name')))
        info = known.get(c.get('id'))
        if info is None:
            skipped.append((c.get('name') or 'Plex', 'no longer in your Plex account'))
            continue
        server = Server(info)
        if not server.connect():
            skipped.append((server.name, 'no answer'))
            continue
        ready_.append((server, c['libraries']))
    return ready_, skipped
