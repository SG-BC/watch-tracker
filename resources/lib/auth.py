"""Sign-in for every tracking service: OAuth device flow (PIN code), one module for all of them.

* The client id of each service comes ONLY from services_config.py (public by design).
* Tokens live ONLY in the add-on's profile folder (<service>_auth.json) -- never in the repo, never in
  logs/uploads. Simkl revokes any token it finds on GitHub.
* One owner refreshes: refreshing can replace the refresh token, so two processes (service + settings
  script) must not refresh independently. A lock file serialises it and the file is re-read inside
  the lock.
* Adding a service = one entry in services_config.SERVICES and one entry in ENDPOINTS below.
"""
import base64
import json
import os
import time
from contextlib import contextmanager

from . import cfg, log, net, services_config

ENDPOINTS = {
    'simkl': {
        'base': 'https://api.simkl.com', 'device': '/oauth2/device', 'token': '/oauth2/token',
        'revoke': '/oauth2/revoke', 'scope': 'media:read media:write', 'write_scope': 'media:write',
        'pin_page': 'https://simkl.com/pin', 'refresh_margin': 86400,
    },
    'mdblist': {
        'base': 'https://api.mdblist.com', 'device': '/oauth/device-authorization/', 'token': '/oauth/token/',
        'revoke': None, 'scope': 'write', 'write_scope': 'write',
        'pin_page': 'https://mdblist.com/oauth/device/', 'refresh_margin': 3 * 86400,   # tokens last 30 days
    },
    'wetrakr': {
        # JSON bodies; the app key travels in a header on every call; the access token lasts 7 days and the
        # refresh token rotates. Device flow: code -> poll (400 until approved).
        'base': 'https://api.wetrakr.com', 'device': '/oauth/device/code', 'token': '/oauth/device/token',
        'refresh': '/oauth/token/refresh', 'revoke': None, 'scope': '', 'write_scope': None,
        'pin_page': 'https://wetrakr.com/activate', 'refresh_margin': 86400,
        'json': True, 'key_header': 'wetrakr-api-key', 'headers': {'wetrakr-api-version': '1'},
    },
    'anilist': {
        # No device flow, no refresh token: the user opens the authorize page (redirect URL set to AniList's auth
        # pin page), copies the token it shows and pastes it into Kodi. The token lasts one year.
        'base': 'https://graphql.anilist.co', 'token_paste': True, 'revoke': None, 'scope': '', 'write_scope': None,
        'authorize': 'https://anilist.co/api/v2/oauth/authorize?client_id={client_id}&response_type=token',
        'refresh_margin': 0,
    },
    'plex': {
        # plex.tv PIN flow (legacy, long-lived token); the flow itself is in plexapi.py. Only storage is used here.
        'base': 'https://plex.tv', 'plex_pin': True, 'revoke': None, 'scope': '', 'write_scope': None,
        'refresh_margin': 0,
    },
}


class LoginError(Exception):
    pass


def user_agent():
    return 'service.watch.tracker/{}'.format(cfg.version() or '1.0.0')


@contextmanager
def _lock(path, timeout=15):
    path += '.lock'
    deadline = time.time() + timeout
    acquired = False
    while True:
        try:
            os.close(os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
            acquired = True
            break
        except FileExistsError:
            try:
                if time.time() - os.path.getmtime(path) > 60:
                    os.remove(path)
                    continue
            except OSError:
                pass
            if time.time() > deadline:
                break
            time.sleep(0.3)
        except OSError:
            break
    try:
        yield
    finally:
        if acquired:
            try:
                os.remove(path)
            except OSError:
                pass


class Account:
    """Tokens and login of one service."""

    def __init__(self, name):
        self.name = name
        self.ep = ENDPOINTS[name]
        self.BASE = self.ep['base']

    @property
    def CLIENT_ID(self):
        return services_config.SERVICES[self.name]['client_id']

    # ------------------------------------------------------------------ storage
    def _file(self):
        return os.path.join(cfg.profile_dir(), self.name + '_auth.json')

    def load(self):
        try:
            with open(self._file(), 'r', encoding='utf-8') as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

    def _save(self, data):
        tmp = self._file() + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(data, f)
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        os.replace(tmp, self._file())

    def is_logged_in(self):
        return bool(self.load().get('access_token'))

    def username(self):
        return self.load().get('username') or ''

    def set_username(self, username):
        data = self.load()
        if data.get('access_token') and username and data.get('username') != username:
            data['username'] = username
            self._save(data)

    def _store_tokens(self, body, previous=None):
        previous = previous or {}
        expires_in = body.get('expires_in')
        data = {
            'access_token': body['access_token'],
            'refresh_token': body.get('refresh_token') or previous.get('refresh_token'),
            'scope': body.get('scope') or previous.get('scope', ''),
            'expires_at': (time.time() + int(expires_in)) if expires_in else None,
            'username': previous.get('username', ''),
        }
        self._save(data)
        return data

    # ------------------------------------------------------------------ http
    def extra_headers(self):
        """Headers every call to this service carries besides the token (WeTrakr: the app key)."""
        key = self.ep.get('key_header')
        return {key: self.CLIENT_ID} if key else {}

    def _headers(self):
        headers = {'User-Agent': user_agent(),
                   'Content-Type': 'application/json' if self.ep.get('json') else 'application/x-www-form-urlencoded'}
        headers.update(self.ep.get('headers') or {})
        headers.update(self.extra_headers())
        return headers

    def _post(self, path, fields):
        """POST to the service's own auth endpoints: JSON or form, as that service wants it."""
        if self.ep.get('json'):
            return net.request('POST', self.BASE + path, headers=self._headers(), json_data=fields)
        return net.request('POST', self.BASE + path, headers=self._headers(), data=fields)

    def common_params(self):
        if self.name == 'simkl':
            return {'client_id': self.CLIENT_ID, 'app-name': cfg.SIMKL_APP_NAME, 'app-version': cfg.version() or '1.0.0'}
        return {}

    # ------------------------------------------------------------------ refresh
    def refresh(self, old_access_token):
        """Refresh under the lock. Returns the current data dict, or None if it could not be refreshed."""
        with _lock(self._file()):
            cur = self.load()
            if cur.get('access_token') and cur.get('access_token') != old_access_token:
                return cur                       # another process already refreshed
            if not cur.get('refresh_token'):
                return None
            if self.ep.get('json'):
                fields = {'refresh_token': cur['refresh_token']}
            else:
                fields = {'grant_type': 'refresh_token', 'client_id': self.CLIENT_ID, 'refresh_token': cur['refresh_token']}
            res = self._post(self.ep.get('refresh') or self.ep['token'], fields)
            if res.ok and res.body.get('access_token'):
                log.debug('{} token refreshed'.format(self.name))
                return self._store_tokens(res.body, cur)
            log.warning('{} token refresh failed: {}'.format(self.name, net.summarize(res)))
            return None

    def access_token(self):
        """A usable access token (refreshing it early if needed), or None when not logged in."""
        d = self.load()
        token = d.get('access_token')
        if not token:
            return None
        exp = d.get('expires_at')
        if exp and time.time() > exp - self.ep['refresh_margin']:
            fresh = self.refresh(token)
            if fresh:
                return fresh.get('access_token')
            if time.time() >= exp:
                return None
        return token

    def on_unauthorized(self, used_token):
        """A call returned 401. Returns a different token to retry with, or None."""
        cur = self.load()
        if cur.get('access_token') and cur['access_token'] != used_token:
            return cur['access_token']           # someone refreshed meanwhile: do NOT refresh again
        fresh = self.refresh(used_token)
        if fresh and fresh.get('access_token') != used_token:
            return fresh['access_token']
        return None

    # ------------------------------------------------------------------ login with a pasted token (AniList)
    def authorize_url(self):
        return self.ep['authorize'].format(client_id=self.CLIENT_ID)

    def store_pasted_token(self, token):
        """Keep a token the user pasted; its expiry is read from the JWT (default: about a year)."""
        token = (token or '').strip()
        expires = time.time() + 360 * 86400
        try:
            part = token.split('.')[1]
            payload = json.loads(base64.urlsafe_b64decode(part + '=' * (-len(part) % 4)))
            expires = float(payload.get('exp') or expires)
        except (IndexError, ValueError, TypeError):
            pass
        self._save({'access_token': token, 'refresh_token': None, 'scope': '', 'expires_at': expires, 'username': ''})

    # ------------------------------------------------------------------ login (device / PIN flow)
    def start_login(self):
        """-> dict(user_code, verification_uri, verification_uri_complete, interval, expires_in, device_code)"""
        fields = {'client_id': self.CLIENT_ID}
        if not self.ep.get('json'):
            fields['scope'] = self.ep['scope']
        res = self._post(self.ep['device'], fields)
        if res.ok and res.body.get('user_code'):
            b = res.body
            return {'user_code': b['user_code'], 'device_code': b.get('device_code', ''),
                    'verification_uri': b.get('verification_uri') or b.get('verification_url') or self.ep['pin_page'],
                    'verification_uri_complete': b.get('verification_uri_complete', ''),
                    'interval': int(b.get('interval', 5)), 'expires_in': int(b.get('expires_in', 900))}
        raise LoginError('{} did not return a login code ({}).'.format(self.name, net.summarize(res)))

    def poll_login(self, session):
        """One poll. Returns 'ok' | 'ok_readonly' | 'pending' | 'slow_down' | 'expired' | 'denied' | 'error'."""
        if self.ep.get('json'):
            fields = {'code': session['device_code'], 'client_id': self.CLIENT_ID}
        else:
            fields = {'grant_type': 'urn:ietf:params:oauth:grant-type:device_code',
                      'client_id': self.CLIENT_ID, 'device_code': session['device_code']}
        res = self._post(self.ep['token'], fields)
        if res.ok and res.body.get('access_token'):
            data = self._store_tokens(res.body)
            write_scope = self.ep.get('write_scope')
            return 'ok' if (not write_scope or write_scope in (data.get('scope') or '')) else 'ok_readonly'
        err = res.body.get('error')
        if err == 'authorization_pending':
            return 'pending'
        if err == 'slow_down':
            return 'slow_down'
        if err == 'expired_token':
            return 'expired'
        if err == 'access_denied':
            return 'denied'
        if res.network_error:
            return 'pending'
        if self.ep.get('json'):                  # WeTrakr: 400 until approved; anything else ends the flow
            if res.status == 400:
                return 'pending'
            if res.status == 429:
                return 'slow_down'
            if res.status in (404, 410):
                return 'expired'
            if res.status in (401, 403):
                return 'denied'
        log.warning('{} login poll: {}'.format(self.name, net.summarize(res)))
        return 'error'

    def logout(self):
        d = self.load()
        token = d.get('refresh_token') or d.get('access_token')
        if token and self.ep.get('revoke'):
            self._post(self.ep['revoke'], {'client_id': self.CLIENT_ID, 'token': token})   # nothing to check on the answer
        try:
            os.remove(self._file())
        except OSError:
            pass


_accounts = {}


def get(name):
    if name not in _accounts:
        _accounts[name] = Account(name)
    return _accounts[name]
