"""WeTrakr: scrobble start/stop, ratings (movies, shows, episodes), the mark-watched fallback, sync writes and reads,
manual edits (remove plays / ratings) and the planning list for the watchlist widget.

WeTrakr resolves ids server-side and accepts title + year, so no search is needed. Auth: OAuth device flow
(PIN code), the app key in the `wetrakr-api-key` header (see auth.ENDPOINTS)."""
import json
import time

from .. import cfg, log, net, status, syncmap, ui
from .base import Provider, ref_key

PAGE = 100
MAX_PAGES = 500
COMPACT_PAGE = 5000                 # compact rows are ~200 bytes; a whole library is a handful of calls
MAX_COMPACT_PAGES = 40
DATE_KEYS = ('watched_at', 'tracked_at', 'date', 'created_at')


def _stamp(when):
    return time.strftime('%Y-%m-%dT%H:%M:%S.000Z', time.gmtime(when))


def _iso(value):
    """Any ISO date from the neutral payloads -> WeTrakr's millisecond form (None when unusable)."""
    ts = syncmap.normalize_ts(value)
    return ts.replace('Z', '.000Z') if ts else None


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
    def _convert_ids(ids, kind):
        keys = ('imdb', 'tmdb', 'anilist') if kind == 'movie' else ('imdb', 'tmdb', 'tvdb', 'anilist')
        out = {}
        for k, v in (ids or {}).items():
            name = 'myanimelist' if k == 'mal' else k
            if k in keys and v not in (None, ''):
                out[k] = int(v) if k != 'imdb' and str(v).isdigit() else v
            elif name == 'myanimelist' and v:
                out[name] = int(v) if str(v).isdigit() else v
        return out

    @classmethod
    def _obj(cls, title, year, ids, kind, native=None):
        obj = {}
        if native:
            obj['id'] = native
        if title:
            obj['title'] = title
        if year:
            obj['year'] = year
        ids = cls._convert_ids(ids, kind)
        if ids:
            obj['ids'] = ids
        return obj

    def item(self, ref):
        return self._obj(ref.get('title'), ref.get('year'), ref.get('ids'), 'movie' if ref['kind'] == 'movie' else 'show',
                         (ref.get('native') or {}).get(self.name))

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

    # ------------------------------------------------------------------ sync / manual writes (neutral payloads in)
    @staticmethod
    def _answer(res):
        """WeTrakr says notFound; the sync runner reads not_found."""
        if res.ok and isinstance(res.body, dict) and res.body.get('notFound') is not None:
            res.body['not_found'] = res.body['notFound']
        return res

    def _history_body(self, payload, remove=False):
        def mark(stamp):
            row = {'status': 'watched'}
            if not remove:
                row['allow_rewatch'] = False
                if stamp:
                    row['tracked_at'] = stamp
            return row

        body = {}
        for it in payload.get('movies') or []:
            obj = self._obj(it.get('title'), it.get('year'), it.get('ids'), 'movie')
            if obj.get('id') or obj.get('ids') or (obj.get('title') and obj.get('year')):
                body.setdefault('movies', []).append(dict(obj, **mark(_iso(it.get('watched_at')))))
        for it in payload.get('shows') or []:
            obj = self._obj(it.get('title'), it.get('year'), it.get('ids'), 'show')
            if not (obj.get('id') or obj.get('ids') or (obj.get('title') and obj.get('year'))):
                continue
            seasons = []
            for season in it.get('seasons') or []:
                row = {'number': season.get('number')}
                eps = [dict({'number': e.get('number')}, **mark(_iso(e.get('watched_at'))))
                       for e in season.get('episodes') or []]
                if eps:
                    row['episodes'] = eps
                else:
                    row.update(mark(_iso(season.get('watched_at'))))
                seasons.append(row)
            if seasons:
                body.setdefault('shows', []).append(dict(obj, status='watched', seasons=seasons))
            else:
                body.setdefault('shows', []).append(dict(obj, **mark(_iso(it.get('watched_at')))))
        return body

    def add_history(self, payload):
        body = self._history_body(payload)
        if not body:
            return net.Result(status=200, body={})
        return self._answer(self._call('POST', '/sync/tracking', json_data=body))

    def remove_history(self, payload):
        body = self._history_body(payload, remove=True)
        if not body:
            return net.Result(status=200, body={})
        return self._answer(self._call('POST', '/sync/tracking/remove/all', json_data=body))

    def _ratings_body(self, payload, with_value=True):
        body = {}
        for key, kind in (('movies', 'movie'), ('shows', 'show')):
            for it in payload.get(key) or []:
                obj = self._obj(it.get('title'), it.get('year'), it.get('ids'), kind)
                if not (obj.get('id') or obj.get('ids') or (obj.get('title') and obj.get('year'))):
                    continue
                if with_value and it.get('rating'):
                    obj['rating'] = it['rating']
                seasons = []
                for season in it.get('seasons') or []:
                    eps = [dict({'number': e.get('number')}, **({'rating': e['rating']} if with_value and e.get('rating') else {}))
                           for e in season.get('episodes') or []]
                    if eps:
                        seasons.append({'number': season.get('number'), 'episodes': eps})
                if seasons:
                    obj['seasons'] = seasons
                body.setdefault(key, []).append(obj)
        return body

    def add_ratings(self, payload):
        body = self._ratings_body(payload)
        if not body:
            return net.Result(status=200, body={})
        return self._answer(self._call('POST', '/sync/ratings', json_data=body))

    def remove_ratings(self, payload):
        body = self._ratings_body(payload, with_value=False)
        if not body:
            return net.Result(status=200, body={})
        return self._answer(self._call('POST', '/sync/ratings/remove', json_data=body))

    # ------------------------------------------------------------------ sync reads (WeTrakr -> others)
    def activity_stamp(self):
        """/sync/last_activities `all`; None when unknown. Unchanged since the last run = nothing to read."""
        res = self._call('GET', '/sync/last_activities')
        if res.ok and isinstance(res.body, dict):
            body = res.body.get('data') if isinstance(res.body.get('data'), dict) else res.body
            return body.get('all')
        return None

    def _pages(self, endpoint, params=None):
        """Yield the rows of a paged list. Raises RuntimeError on a refused page."""
        for page in range(1, MAX_PAGES + 1):
            res = self._call('GET', endpoint, params=dict(params or {}, page=page, limit=PAGE))
            if not res.ok:
                raise RuntimeError('WeTrakr answered {} for {}'.format(res.status, endpoint))
            rows = res.raw if isinstance(res.raw, list) else (res.body.get('data') or res.body.get('results') or [])
            rows = [r for r in rows if isinstance(r, dict)]
            for row in rows:
                yield row
            if len(rows) < PAGE:
                return

    def _compact(self, endpoint, params=None):
        """Yield the compact rows of a list (small rows, up to 5,000 a page, walked with the `after` cursor
        WeTrakr hands back in X-Pagination-Next). Raises RuntimeError on a refused page."""
        after, shown = None, 0
        for _ in range(MAX_COMPACT_PAGES):
            query = dict(params or {}, compact='true', limit=COMPACT_PAGE)
            if after:
                query['after'] = after
            res = self._call('GET', endpoint, params=query)
            if not res.ok:
                raise RuntimeError('WeTrakr answered {} for {}'.format(res.status, endpoint))
            rows = res.raw if isinstance(res.raw, list) else (res.body.get('data') or res.body.get('results') or [])
            for row in rows:
                if not isinstance(row, dict):
                    continue
                if shown < 2:                       # real shapes for the next debug log
                    shown += 1
                    log.debug('sample wetrakr {}: {}'.format(endpoint, json.dumps(row)[:300]))
                yield row
            after = res.headers.get('x-pagination-next')
            if not after or not rows:
                return

    @staticmethod
    def _date(row):
        for key in DATE_KEYS:
            when = syncmap.normalize_ts(row.get(key))
            if when:
                return when
        return None

    @staticmethod
    def _score(value):
        """A rating as the number 1-10, whether the row holds 8, "8", 8.5 or {"value": 8}; None when absent."""
        if isinstance(value, dict):
            value = next((value[k] for k in ('value', 'rating', 'score') if value.get(k) is not None), None)
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        return syncmap.round_half_up(number) if number > 0 else None

    @staticmethod
    def _neutral(obj, kind):
        ids = {k: v for k, v in (obj.get('ids') or {}).items() if k in ('imdb', 'tmdb', 'tvdb', 'anilist') and v}
        if (obj.get('ids') or {}).get('myanimelist'):
            ids['mal'] = obj['ids']['myanimelist']
        return syncmap.media({'title': obj.get('title'), 'year': obj.get('year'), 'ids': ids}, kind) or {}

    def read_history(self, since=None):
        """Every play, newest first -> a Simkl-shaped history payload (the latest play of each item)."""
        params = {'from_date': since} if since else {}
        movies, shows, seen = [], {}, set()
        for row in self._compact('/sync/tracking/watched/history/movies', params):
            item = self._neutral(row, 'movie')
            key = str(sorted((item.get('ids') or {}).items()))
            if not item or key in seen:
                continue
            seen.add(key)
            when = self._date(row)
            movies.append(dict(item, **({'watched_at': when} if when else {})))
        for row in self._compact('/sync/tracking/watched/history/episodes', params):
            item = self._neutral({'ids': row.get('show_ids')}, 'show')
            season_no, number = row.get('season_number'), row.get('number')
            if not item or season_no is None or number is None:
                continue
            group = shows.setdefault(syncmap.show_key(item), {'item': item, 'seasons': {}})
            season = group['seasons'].setdefault(season_no, {})
            season.setdefault(number, self._date(row))    # newest first: the first row is the latest play
        out = {}
        if movies:
            out['movies'] = movies
        rows = []
        for g in shows.values():
            seasons = [{'number': s, 'episodes': [dict({'number': n}, **({'watched_at': w} if w else {}))
                                                  for n, w in sorted(eps.items())]}
                       for s, eps in sorted(g['seasons'].items())]
            rows.append(dict(g['item'], seasons=seasons))
        if rows:
            out['shows'] = rows
        return out

    def read_ratings(self, since=None):
        params = {'from_date': since} if since else {}
        out = {}
        for key, kind in (('movies', 'movie'), ('shows', 'show')):
            rows = []
            for row in self._compact('/sync/ratings/' + key, params):
                score = self._score(row.get('rating'))
                item = self._neutral(row, kind)
                if not score or not item:
                    continue
                stamp = syncmap.normalize_ts(row.get('rated_at'))
                rows.append(dict(item, rating=score, **({'rated_at': stamp} if stamp else {})))
            if rows:
                out[key] = rows
        return out

    # ------------------------------------------------------------------ episode lists (manual "up to" marks)
    @staticmethod
    def _data(res):
        return res.body.get('data') if isinstance(res.body.get('data'), dict) else res.body

    def _lookup_show(self, ref):
        """WeTrakr id of the ref's show: GET /media/external/{source}/{id}?type=show, tmdb then imdb then tvdb."""
        ids = ref.get('ids') or {}
        for source in ('tmdb', 'imdb', 'tvdb'):
            if not ids.get(source):
                continue
            res = self._call('GET', '/media/external/{}/{}'.format(source, ids[source]), params={'type': 'show'})
            found = self._data(res).get('id') if res.ok else None
            if str(found or '').isdigit():
                return int(found)
        return None

    def episode_list(self, ref):
        """GET /shows/{id}?append=seasons,episodes: the whole tree of the show in one call (docs: seasons carry
        number, episodes number / air_date / air_datetime; season 0 = specials). Only episodes whose air date has
        passed count: one with no date is not announced yet. {id} is the WeTrakr id (/media/external lookup)."""
        wid = (ref.get('native') or {}).get(self.name) or self._lookup_show(ref)
        if not wid:
            return None
        ref.setdefault('native', {})[self.name] = wid
        res = self._call('GET', '/shows/{}'.format(wid), params={'append': 'seasons,episodes'})
        if not res.ok:
            return None
        now, out, shown = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()), set(), False
        for season in self._data(res).get('seasons') or []:
            if not isinstance(season, dict):
                continue
            for ep in season.get('episodes') or []:
                if not isinstance(ep, dict):
                    continue
                if not shown:                       # the real shape, for the next debug log
                    shown = True
                    log.debug('sample wetrakr episode: {}'.format(json.dumps(ep)[:300]))
                aired = syncmap.normalize_ts(ep.get('air_datetime')) or str(ep.get('air_date') or '')[:10]
                if not aired or aired > now:            # no date yet = not announced; a date ahead = not aired
                    continue
                try:
                    pair = (int(season.get('number', season.get('season_number'))),
                            int(ep.get('number', ep.get('episode_number'))))
                except (TypeError, ValueError):
                    continue
                if pair[0] > 0 and pair[1] > 0:
                    out.add(pair)
        return sorted(out) or None

    # ------------------------------------------------------------------ watchlist (the "planning" list)
    def watchlist(self):
        out = []
        try:
            for key, kind in (('movies', 'movie'), ('shows', 'show')):
                for row in self._pages('/sync/tracking/planning/' + key):
                    media = row.get(kind) or row
                    item = self._neutral(media, kind)
                    poster = media.get('poster') or media.get('poster_path') or ''
                    if isinstance(poster, str) and poster.startswith('/'):
                        poster = 'https://image.tmdb.org/t/p/w342' + poster
                    out.append({'kind': kind, 'title': media.get('title'), 'year': media.get('year'),
                                'ids': item.get('ids') or {}, 'poster': poster if isinstance(poster, str) else ''})
        except RuntimeError as e:
            log.warning(str(e))
            return None
        return out


PROVIDER = WeTrakr()
