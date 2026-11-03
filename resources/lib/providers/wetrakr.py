"""WeTrakr: scrobble start/stop, ratings (movies, shows, episodes) and the mark-watched fallback.

WeTrakr resolves ids server-side and accepts title + year, so no search is needed. Auth: OAuth device flow
(PIN code), the app key in the `wetrakr-api-key` header (see auth.ENDPOINTS). Sync pairs are not wired yet."""
import time

from .. import cfg, log, net, status, ui
from .base import Provider, ref_key


def _stamp(when):
    return time.strftime('%Y-%m-%dT%H:%M:%S.000Z', time.gmtime(when))


class WeTrakr(Provider):
    name = 'wetrakr'
    label = 'WeTrakr'

    def __init__(self):
        self._unmatched = set()     # items WeTrakr could not identify; this session only

    def enabled(self):
        if not cfg.get_bool('wetrakr_enabled', False):
            return False
        if not self.logged_in():
            ui.notify_once('wetrakr-login', 'WeTrakr: log in from the add-on settings')
            return False
        return True

    def watched_mark(self):
        return max(50, min(100, cfg.get_int('wetrakr_min_progress', 80)))

    # ------------------------------------------------------------------ account
    def fetch_username(self):
        res = self._call('GET', '/account/settings')
        if not res.ok:
            return ''
        info = res.body.get('info') or (res.body.get('data') or {}).get('info') or {}
        return str(info.get('username') or info.get('display_name') or '') if isinstance(info, dict) else ''

    def check(self):
        if self.account.access_token() is None:
            status.set_status(self.name, False)
            return
        if not self.account.username():
            name = self.fetch_username()
            if name:
                self.account.set_username(name)
        status.set_status(self.name, True)

    # ------------------------------------------------------------------ http
    def _call(self, method, endpoint, params=None, json_data=None):
        res = self._call_authed(method, endpoint, params, json_data)
        if res.status in (420, 426):        # free-plan quota reached / VIP only
            ui.notify_once('wetrakr-plan', 'WeTrakr: {}'.format(res.body.get('message') or 'plan limit reached'))
        return res

    # ------------------------------------------------------------------ item objects
    @staticmethod
    def _ids(ref):
        keys = ('imdb', 'tmdb') if ref['kind'] == 'movie' else ('imdb', 'tmdb', 'tvdb')
        out = {}
        for k, v in (ref.get('ids') or {}).items():
            if k in keys and v not in (None, ''):
                out[k] = int(v) if k != 'imdb' and str(v).isdigit() else v
        return out

    def item(self, ref):
        obj = {}
        native = (ref.get('native') or {}).get(self.name)
        if native:
            obj['id'] = native
        if ref.get('title'):
            obj['title'] = ref['title']
        if ref.get('year'):
            obj['year'] = ref['year']
        ids = self._ids(ref)
        if ids:
            obj['ids'] = ids
        return obj

    def accepts(self, ref):
        if ref_key(ref) in self._unmatched:
            return False
        obj = self.item(ref)
        return bool(obj.get('id') or obj.get('ids')) or bool(obj.get('title') and obj.get('year'))   # never a bare title

    def _payload(self, ref, progress):
        body = {'progress': progress, 'app_version': cfg.version()}
        if ref['kind'] == 'movie':
            body['movie'] = self.item(ref)
        else:
            body['show'] = self.item(ref)
            body['episode'] = {'season': ref['season'], 'number': ref['episode']}
        return body

    # ------------------------------------------------------------------ scrobbling
    def _scrobble(self, action, ref, progress):
        res = self._call('POST', '/scrobble/' + action, params={'min_progress': self.watched_mark()},
                         json_data=self._payload(ref, progress))
        if res.unmatched:
            self._unmatched.add(ref_key(ref))
            log.warning('wetrakr could not identify this item; skipping it for this session')
        return res

    def start(self, ref, progress, position=None, duration=None):
        return self._scrobble('start', ref, progress)

    def stop(self, ref, progress, position=None, duration=None, when=None):
        """Below the watched mark WeTrakr keeps a paused session to resume; a stop is never retried here:
        the saved pending stop covers it."""
        return self._scrobble('stop', ref, progress)

    def mark_watched(self, ref, when):
        stamp = _stamp(when)
        item = self.item(ref)
        if ref['kind'] == 'movie':
            body = {'movies': [dict(item, status='watched', tracked_at=stamp, allow_rewatch=False)]}
        else:
            body = {'shows': [dict(item, seasons=[{'number': ref['season'], 'episodes': [
                {'number': ref['episode'], 'status': 'watched', 'tracked_at': stamp, 'allow_rewatch': False}]}])]}
        return self._call('POST', '/sync/tracking', json_data=body)

    # ------------------------------------------------------------------ ratings (0-10, movies, shows, episodes)
    def rating_supported(self, kind):
        return kind in ('movie', 'show', 'episode')

    def rate(self, ref, value, when=None):
        """WeTrakr's rating call takes no date, so `when` is not sent."""
        item = self.item(ref)
        if ref['kind'] == 'movie':
            body = {'movies': [dict(item, rating=value)]}
        elif ref['kind'] == 'show':
            body = {'shows': [dict(item, rating=value)]}
        else:
            body = {'shows': [dict(item, seasons=[{'number': ref['season'], 'episodes': [
                {'number': ref['episode'], 'rating': value}]}])]}
        return self._call('POST', '/sync/ratings', json_data=body)


PROVIDER = WeTrakr()
