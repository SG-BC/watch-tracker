"""Simkl: scrobble start/stop, movie ratings, mark-watched fallback, history/ratings writes for sync.

No search is needed: Simkl resolves ids server-side and accepts title + year. One call per user
action, never on a timer. Writes are paced to 1 per second.
"""
import threading
import time

from .. import cache, cfg, log, net, parsing, status, ui
from .base import Provider, ref_key

MIN_INTERVAL = 1.1          # Simkl allows 1 POST per second
_pace_lock = threading.Lock()
_last_call = [0.0]


def _pace():
    with _pace_lock:
        wait = _last_call[0] + MIN_INTERVAL - time.time()
        if wait > 0:
            time.sleep(wait)
        _last_call[0] = time.time()


def _stamp(when):
    return time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(when))


class Simkl(Provider):
    name = 'simkl'
    label = 'Simkl'

    title_only_fallback = True

    def __init__(self):
        self._unmatched = set()     # items Simkl could not identify; this session only

    def enabled(self):
        if not cfg.get_bool('simkl_enabled', False):
            return False
        if not self.logged_in():
            ui.notify_once('simkl-login', 'Simkl: log in from the add-on settings')
            return False
        return True

    def watched_mark(self):
        return max(50, min(100, cfg.get_int('simkl_min_progress', 80)))

    def fetch_username(self):
        res = self._call('GET', '/users/settings')
        user = res.body.get('user') if res.ok else None
        return str((user or {}).get('name') or '') if isinstance(user, dict) else ''

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
    def _before_call(self):
        _pace()

    def _call(self, method, endpoint, params=None, json_data=None):
        return self._call_authed(method, endpoint, params, json_data)

    # ------------------------------------------------------------------ item objects
    @staticmethod
    def _ids(ref, kind=None):
        keys = ('imdb', 'tmdb') if (kind or ref['kind']) == 'movie' else ('imdb', 'tmdb', 'tvdb')
        return {k: v for k, v in (ref.get('ids') or {}).items() if k in keys}

    def item(self, ref):
        obj = {}
        if ref.get('title'):
            obj['title'] = ref['title']
        if ref.get('year'):
            obj['year'] = ref['year']
        ids = self._ids(ref)
        native = (ref.get('native') or {}).get(self.name)
        if native:
            ids = dict(ids, simkl=native)
        if ids:
            obj['ids'] = ids
        return obj

    def accepts(self, ref):
        if ref_key(ref) in self._unmatched:
            return False
        obj = self.item(ref)
        return bool(obj.get('ids')) or bool(obj.get('title') and obj.get('year'))      # title + year, never title alone

    def _payload(self, ref, progress):
        if ref['kind'] == 'movie':
            return {'progress': progress, 'movie': self.item(ref)}
        return {'progress': progress, 'show': self.item(ref),
                'episode': {'season': ref['season'], 'number': ref['episode']}}

    # ------------------------------------------------------------------ scrobbling
    def _scrobble(self, action, ref, progress):
        res = self._call('POST', '/scrobble/' + action, params={'min_progress': self.watched_mark()},
                         json_data=self._payload(ref, progress))
        if res.status == 409:           # already completed within the last hour: nothing to do
            res.status = 200
            res.note = 'already completed (409)'
        elif res.unmatched:
            self._unmatched.add(ref_key(ref))
            log.warning('simkl could not identify this item; skipping it for this session')
        return res

    def start(self, ref, progress, position=None, duration=None):
        return self._scrobble('start', ref, progress)

    def stop(self, ref, progress, position=None, duration=None, when=None):
        return self._scrobble('stop', ref, progress)       # no retry loop: the saved pending stop covers it

    def mark_watched(self, ref, when):
        stamp = _stamp(when)
        item = self.item(ref)
        if ref['kind'] == 'movie':
            body = {'movies': [dict(item, watched_at=stamp)]}
        else:
            body = {'shows': [dict(item, seasons=[{'number': ref['season'], 'episodes': [
                {'number': ref['episode'], 'watched_at': stamp}]}])]}
        return self._call('POST', '/sync/history', json_data=body)

    # ------------------------------------------------------------------ ratings (movies only)
    def rating_supported(self, kind):
        return kind in ('movie', 'show')    # Simkl has no episode ratings; the live prompt only asks for movies/episodes

    def rate(self, ref, value, when=None):
        extra = {'rated_at': _stamp(when)} if when else {}
        key = 'movies' if ref['kind'] == 'movie' else 'shows'       # a show rating only comes from re-dating
        return self._call('POST', '/sync/ratings', json_data={key: [dict(self.item(ref), rating=value, **extra)]})

    # ------------------------------------------------------------------ title search (no ids, no year)
    def search_ids(self, kind, title, year=None, max_year=None):
        """GET /search/{movie|tv}: only for an item with no ids AND no usable year (Simkl matches by title +
        year on its own otherwise). `year` (the show's premiere year, +-1) or `max_year` (it cannot have
        premiered later) narrow the hits. Cached on disk; returns {'native_id': .., 'tmdb': ..} or {}."""
        mediatype = 'movie' if kind == 'movie' else 'tv'
        key = 'simkl|{}|{}|{}|{}'.format(mediatype, parsing.normalize(title), year or '', max_year or '')
        cached = cache.get(key)
        if cached is not None:
            return cached
        res = self._call('GET', '/search/' + mediatype, params={'q': title, 'limit': 10})
        if not res.ok:
            return {}
        rows = res.raw if isinstance(res.raw, list) else res.body.get('results') or []
        results = [dict(r, score=0) for r in rows if isinstance(r, dict)]
        best = parsing.pick_best(results, title, year, max_year)
        found = {}
        if best:
            ids = best.get('ids') or {}
            if ids.get('simkl_id') or ids.get('simkl'):
                found['native_id'] = int(ids.get('simkl_id') or ids.get('simkl'))
            if ids.get('tmdb'):
                found['tmdb'] = ids['tmdb']
        cache.put(key, found)
        log.debug('simkl title search {} "{}" ({}) -> {}'.format(mediatype, title, year, found or 'no match'))
        return found

    # ------------------------------------------------------------------ sync writes
    def add_history(self, payload):
        return self._call('POST', '/sync/history', json_data=payload)

    def add_ratings(self, payload):
        return self._call('POST', '/sync/ratings', json_data=payload)

    def remove_history(self, payload):
        return self._call('POST', '/sync/history/remove', json_data=payload)

    def remove_ratings(self, payload):
        return self._call('POST', '/sync/ratings/remove', json_data=payload)

    def watchlist(self):
        """Plan to watch: movies, shows and anime. Asks /sync/activities first; unchanged -> None is NOT returned,
        the caller keeps its copy through `watchlist_changed`."""
        out = []
        for type_, kind, key in (('movies', 'movie', 'movie'), ('shows', 'show', 'show'), ('anime', 'show', 'show')):
            res = self.get_all_items(type_, 'plantowatch')
            if not res.ok:
                return None
            for row in (res.body.get(type_) or []) if isinstance(res.body, dict) else []:
                obj = row.get(key) or row.get('anime') or row.get('show') or {}
                poster = obj.get('poster')
                out.append({'kind': kind, 'title': obj.get('title'), 'year': obj.get('year'),
                            'ids': {k: v for k, v in (obj.get('ids') or {}).items() if k in ('imdb', 'tmdb', 'tvdb')},
                            'poster': 'https://simkl.in/posters/{}_m.webp'.format(poster) if poster else ''})
        return out

    def watchlist_stamp(self):
        """Simkl's own 'something changed' stamp (activities.all); None when unknown."""
        res = self.get_activities()
        return res.body.get('all') if res.ok and isinstance(res.body, dict) else None

    def get_activities(self):
        return self._call('GET', '/sync/activities')

    def get_all_items(self, type_, status, params=None):
        return self._call('GET', '/sync/all-items/{}/{}'.format(type_, status), params=params)

    def add_to_list(self, payload):
        return self._call('POST', '/sync/add-to-list', json_data=payload)


PROVIDER = Simkl()
