"""MDBList: scrobble start/stop, ratings, mark-watched fallback, cached title search, sync reads and writes.
Free tier: 1,000 requests/day. Auth: OAuth device flow (PIN code), Bearer token."""
import time

from .. import cache, cfg, log, net, parsing, status, ui
from .base import Provider

MAX_WAIT = 10
SEARCH_LIMIT = 10


def _stamp(when):
    return time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(when))


class MDBList(Provider):
    name = 'mdblist'
    label = 'MDBList'
    searches_by_title = True

    def enabled(self):
        if not cfg.get_bool('mdblist_enabled', True):
            return False
        if not self.logged_in():
            ui.notify_once('mdblist-login', 'MDBList: log in from the add-on settings')
            return False
        return True

    # ------------------------------------------------------------------ http
    def _request(self, method, endpoint, params=None, json_data=None):
        return self._call_authed(method, endpoint, params, json_data)

    def fetch_username(self):
        res = self._request('GET', '/user')
        return str(res.body.get('username') or res.body.get('name') or '') if res.ok else ''

    def check(self):
        if self.account.access_token() is None:
            status.set_status(self.name, False)
            return
        name = self.fetch_username()         # cheap; the result also sets the status
        if name:
            self.account.set_username(name)
            status.set_status(self.name, True)

    # ------------------------------------------------------------------ ids / payloads
    @staticmethod
    def _kind(ref):
        return 'movie' if ref['kind'] == 'movie' else 'show'

    def _ids(self, ref):
        return parsing.normalize_ids(ref.get('ids') or {}, self._kind(ref))

    def accepts(self, ref):
        return bool(self._ids(ref))

    def _payload(self, ref, progress):
        body = {'progress': progress, 'app_name': cfg.APP_NAME, 'app_version': cfg.version()}
        if ref['kind'] == 'movie':
            body['movie'] = {'ids': self._ids(ref)}
        else:
            body['show'] = {'ids': self._ids(ref),
                            'season': {'number': ref['season'], 'episode': {'number': ref['episode']}}}
        return body

    # ------------------------------------------------------------------ scrobbling
    def start(self, ref, progress, position=None, duration=None):
        return self._request('POST', '/scrobble/start', json_data=self._payload(ref, progress))

    def stop(self, ref, progress, position=None, duration=None, when=None):
        """Retried once on network trouble / 5xx / short 429; start is never retried."""
        res = net.Result()
        for attempt in range(2):
            res = self._request('POST', '/scrobble/stop', json_data=self._payload(ref, progress))
            if res.ok or not res.transient or attempt:
                break
            time.sleep(min(res.retry_after or 3, MAX_WAIT))
        return res

    def mark_watched(self, ref, when):
        stamp = _stamp(when)
        if ref['kind'] == 'movie':
            body = {'movies': [{'ids': self._ids(ref), 'watched_at': stamp}]}
        else:
            body = {'shows': [{'ids': self._ids(ref), 'seasons': [{'number': ref['season'], 'episodes': [
                {'number': ref['episode'], 'watched_at': stamp}]}]}]}
        return self._request('POST', '/sync/watched', json_data=body)

    # ------------------------------------------------------------------ ratings
    def rating_supported(self, kind):
        return kind in ('movie', 'episode')

    def rate(self, ref, value, when=None):
        extra = {'rated_at': _stamp(when)} if when else {}
        if ref['kind'] == 'movie':
            body = {'movies': [dict({'ids': self._ids(ref), 'rating': value}, **extra)]}
        elif ref['kind'] == 'show':
            body = {'shows': [dict({'ids': self._ids(ref), 'rating': value}, **extra)]}
        else:
            body = {'shows': [{'ids': self._ids(ref), 'seasons': [{'number': ref['season'], 'episodes': [
                dict({'number': ref['episode'], 'rating': value}, **extra)]}]}]}
        return self._request('POST', '/sync/ratings', json_data=body)

    # ------------------------------------------------------------------ title search (cached)
    def search_ids(self, kind, title, year=None, max_year=None):
        """GET /search/<movie|show>: ids of the exact-title match, {} if none. One request per
        never-seen title; hits and misses are cached on disk; network errors are not cached."""
        mediatype = 'movie' if kind == 'movie' else 'show'
        key = '{}|{}|{}|{}'.format(mediatype, parsing.normalize(title), year or '', max_year or '')
        cached = cache.get(key)
        if cached is not None:
            log.debug('search cache hit {} -> {}'.format(key, cached))
            return cached
        params = {'query': title, 'limit': SEARCH_LIMIT}
        if year:
            params['year'] = year
        res = self._request('GET', '/search/' + mediatype, params=params)
        if not res.ok:
            return {}
        results = [r for r in (res.body.get('search') or [])
                   if isinstance(r, dict) and r.get('type') in (None, '', mediatype)]
        best = parsing.pick_best(results, title, year, max_year)
        ids = parsing.normalize_ids(parsing.ids_from_search_item(best), mediatype) if best else {}
        cache.put(key, ids)
        log.debug('title search {} "{}" ({}) -> {}'.format(mediatype, title, year, ids or 'no match'))
        return ids

    # ------------------------------------------------------------------ sync writes (Simkl-shaped payloads in)
    def _convert(self, payload):
        """Neutral payload (title/year/ids per item) -> MDBList's (ids only). Items without a usable id are dropped."""
        out, dropped = {}, 0
        for key, kind in (('movies', 'movie'), ('shows', 'show')):
            rows = []
            for item in payload.get(key) or []:
                ids = parsing.normalize_ids(item.get('ids') or {}, kind)
                if not ids:
                    dropped += 1
                    continue
                rows.append(dict({k: v for k, v in item.items() if k not in ('title', 'year', 'ids', 'to', 'added_at')}, ids=ids))
            if rows:
                out[key] = rows
        if dropped:
            log.debug('mdblist: {} items without a usable id were not sent'.format(dropped))
        return out

    def add_history(self, payload):
        body = self._convert(payload)
        if not body:
            return net.Result(status=200, body={})
        return self._request('POST', '/sync/watched', json_data=body)

    def add_ratings(self, payload):
        body = self._convert(payload)
        if not body:
            return net.Result(status=200, body={})
        return self._request('POST', '/sync/ratings', json_data=body)

    # ------------------------------------------------------------------ sync reads (MDBList -> others)
    def get_watched(self, cursor=None, since=None, limit=1000, plays=False):
        params = {'limit': limit}
        if plays:
            params['plays'] = 'all'     # one row per play, with that play's own date
        if cursor:
            params['cursor'] = cursor
        if since:
            params['since'] = since
        return self._request('GET', '/sync/watched', params=params)

    def get_ratings(self, cursor=None, since=None, limit=1000):
        params = {'limit': limit}
        if cursor:
            params['cursor'] = cursor
        if since:
            params['since'] = since
        return self._request('GET', '/sync/ratings', params=params)


PROVIDER = MDBList()
