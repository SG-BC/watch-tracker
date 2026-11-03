"""AniList (anime only): progress and scores through the GraphQL API, sync reads and writes, manual edits.

AniList has no scrobbling. A watch is "progress = episode number" on the list entry (COMPLETED once it reaches the
episode count), a rating is a score. It cannot look anything up by imdb / tvdb / tmdb id, and one AniList entry is
one season (or movie), so only the FIRST season of a show is written, and only when the item is matched by
an AniList / MAL id or by an exact title AND year. Everything unsure is skipped; nothing is ever lowered or
overwritten except by the manual editor. Login: the user pastes the token AniList shows (auth pin page).
Rate limit: 90 requests a minute (30 while the API is degraded), so calls are paced."""
import calendar
import threading
import time

from .. import cache, cfg, log, net, parsing, status, syncmap, ui
from .base import Provider

MIN_INTERVAL = 1.1
_pace_lock = threading.Lock()
_last_call = [0.0]

SHOW_FORMATS = ('TV', 'TV_SHORT', 'ONA')

Q_ENTRY = 'query($id:Int){Media(id:$id,type:ANIME){id episodes format mediaListEntry{id status progress score repeat}}}'
Q_MAL = 'query($mal:Int){Media(idMal:$mal,type:ANIME){id}}'
Q_SEARCH = ('query($s:String){Page(perPage:10){media(search:$s,type:ANIME,isAdult:false){id idMal format seasonYear '
            'startDate{year} episodes title{romaji english native} synonyms}}}')
Q_VIEWER = 'query{Viewer{id name}}'
Q_LIST = ('query($userId:Int!){MediaListCollection(type:ANIME,userId:$userId){lists{entries{status progress '
          'score(format:POINT_100) completedAt{year month day} media{id idMal format startDate{year} seasonYear '
          'title{romaji english}}}}}}')
M_SAVE = ('mutation($mediaId:Int,$status:MediaListStatus,$progress:Int,$scoreRaw:Int,$startedAt:FuzzyDateInput,'
          '$completedAt:FuzzyDateInput){SaveMediaListEntry(mediaId:$mediaId,status:$status,progress:$progress,'
          'scoreRaw:$scoreRaw,startedAt:$startedAt,completedAt:$completedAt){id status progress}}')
M_DELETE = 'mutation($id:Int){DeleteMediaListEntry(id:$id){deleted}}'


def _pace():
    with _pace_lock:
        wait = _last_call[0] + MIN_INTERVAL - time.time()
        if wait > 0:
            time.sleep(wait)
        _last_call[0] = time.time()


def _epoch(when):
    """Epoch seconds or an ISO string -> epoch seconds (None when unusable)."""
    if isinstance(when, (int, float)):
        return float(when)
    ts = syncmap.normalize_ts(when)
    return calendar.timegm(time.strptime(ts, '%Y-%m-%dT%H:%M:%SZ')) if ts else None


def _fuzzy(when):
    epoch = _epoch(when)
    if epoch is None:
        return None
    t = time.localtime(epoch)
    return {'year': t.tm_year, 'month': t.tm_mon, 'day': t.tm_mday}


def _stamp_from_fuzzy(date):
    if date and date.get('year') and date.get('month') and date.get('day'):
        return '{:04d}-{:02d}-{:02d}T12:00:00Z'.format(date['year'], date['month'], date['day'])
    return None


class AniList(Provider):
    name = 'anilist'
    label = 'AniList'
    batch_size = 25             # one item = two paced requests; keep a sync batch short and cancellable

    def enabled(self):
        if not cfg.get_bool('anilist_enabled', False):
            return False
        if not self.logged_in():
            ui.notify_once('anilist-login', 'AniList: log in from the add-on settings')
            return False
        return True

    # ------------------------------------------------------------------ http
    def _before_call(self):
        _pace()

    def _gql(self, query, variables=None):
        res = self._call_authed('POST', '', json_data={'query': query, 'variables': variables or {}})
        errors = res.body.get('errors') if isinstance(res.body, dict) else None
        if res.ok and errors:                       # HTTP 200 can still carry errors
            res.status = int((errors[0] or {}).get('status') or 400)
            log.debug('anilist error: {}'.format(str((errors[0] or {}).get('message'))[:120]))
        return res

    @staticmethod
    def _data(res):
        return (res.body.get('data') or {}) if res.ok and isinstance(res.body, dict) else {}

    # ------------------------------------------------------------------ account
    def fetch_username(self):
        viewer = self._data(self._gql(Q_VIEWER)).get('Viewer') or {}
        return str(viewer.get('name') or '')

    def check(self):
        if self.account.access_token() is None:
            status.set_status(self.name, False)
            return
        if not self.account.username():
            name = self.fetch_username()
            if name:
                self.account.set_username(name)
        status.set_status(self.name, True)

    # ------------------------------------------------------------------ matching
    @staticmethod
    def _usable(ref_or_item):
        ids = ref_or_item.get('ids') or {}
        return bool(ids.get('anilist') or ids.get('mal') or (ref_or_item.get('title') and ref_or_item.get('year')))

    def accepts(self, ref):
        if ref['kind'] != 'movie' and ref.get('season') not in (None, 1):
            return False                    # an AniList entry is one season; only the first can be matched safely
        return self._usable(ref)

    def media_id(self, kind, title, year, ids):
        """AniList media id, or None when the item cannot be matched safely."""
        ids = ids or {}
        if ids.get('anilist'):
            return int(ids['anilist'])
        if ids.get('mal'):
            key = 'anilist|mal|{}'.format(ids['mal'])
            hit = cache.get(key)
            if hit is None:
                res = self._gql(Q_MAL, {'mal': int(ids['mal'])})
                if res.status == 404:
                    hit = {}
                elif not res.ok:
                    return None
                else:
                    found = (self._data(res).get('Media') or {}).get('id')
                    hit = {'id': found} if found else {}
                cache.put(key, hit)
            if hit.get('id'):
                return int(hit['id'])
        if not (title and year):
            return None
        key = 'anilist|{}|{}|{}'.format(kind, parsing.normalize(title), year)
        hit = cache.get(key)
        if hit is None:
            res = self._gql(Q_SEARCH, {'s': title})
            if not res.ok:
                return None
            hit = {}
            wanted = parsing.normalize(title)
            for m in (self._data(res).get('Page') or {}).get('media') or []:
                if (m.get('format') == 'MOVIE') != (kind == 'movie') or (kind != 'movie' and m.get('format') not in SHOW_FORMATS):
                    continue
                t = m.get('title') or {}
                names = {parsing.normalize(n) for n in [t.get('romaji'), t.get('english'), t.get('native')] + (m.get('synonyms') or []) if n}
                begun = (m.get('startDate') or {}).get('year') or m.get('seasonYear')
                if wanted in names and begun == year:
                    hit = {'id': m['id']}
                    break
            cache.put(key, hit)
            log.debug('anilist title search {} "{}" ({}) -> {}'.format(kind, title, year, hit or 'no match'))
        return int(hit['id']) if hit.get('id') else None

    # ------------------------------------------------------------------ writing one entry
    def _apply(self, media_id, progress=None, when=None, score=None):
        """Raise progress (never lower it) / set a score. progress 'all' = the whole season. -> net.Result."""
        res = self._gql(Q_ENTRY, {'id': media_id})
        media = self._data(res).get('Media')
        if not media:
            return res if not res.ok else net.Result(status=404, body={})
        cur = media.get('mediaListEntry') or {}
        total = media.get('episodes') or (1 if media.get('format') == 'MOVIE' else None)
        variables = {'mediaId': media_id}
        changed = False
        if progress is not None:
            target = (total or 1) if progress == 'all' else int(progress)
            new = max(cur.get('progress') or 0, target)
            if total:
                new = min(new, total)
            status_now = cur.get('status')
            if total and new >= total:
                want = 'COMPLETED'
            elif status_now in (None, 'PLANNING', 'PAUSED', 'DROPPED'):
                want = 'CURRENT'
            else:
                want = status_now
            if new != (cur.get('progress') or 0) or want != status_now:
                variables.update(progress=new, status=want)
                changed = True
                if want == 'COMPLETED' and status_now != 'COMPLETED' and _fuzzy(when):
                    variables['completedAt'] = _fuzzy(when)
                if not cur and _fuzzy(when):
                    variables['startedAt'] = _fuzzy(when)
        if score is not None and score != cur.get('score'):
            variables['scoreRaw'] = score
            changed = True
        if not changed:
            return net.Result(status=200, body={})
        return self._gql(M_SAVE, variables)

    # ------------------------------------------------------------------ live tracking (no scrobbling)
    def start(self, ref, progress, position=None, duration=None):
        return net.Result(status=200, body={})

    def _watched(self, ref, when):
        kind = 'movie' if ref['kind'] == 'movie' else 'show'
        media_id = self.media_id(kind, ref.get('title'), ref.get('year'), ref.get('ids'))
        if media_id is None:
            log.debug('anilist: no safe match for this item -> not written')
            return self._skipped()
        return self._apply(media_id, 1 if kind == 'movie' else ref['episode'], when)

    @staticmethod
    def _skipped():
        res = net.Result(status=200, body={})
        res.note = 'no match'
        return res

    def stop(self, ref, progress, position=None, duration=None, when=None):
        if progress < 80:
            return net.Result(status=200, body={})
        return self._watched(ref, when or time.time())

    def mark_watched(self, ref, when):
        return self._watched(ref, when)

    # ------------------------------------------------------------------ ratings (movies and shows; AniList converts to the user's scale)
    def rating_supported(self, kind):
        return kind in ('movie', 'show')

    def rate(self, ref, value, when=None):
        kind = 'movie' if ref['kind'] == 'movie' else 'show'
        media_id = self.media_id(kind, ref.get('title'), ref.get('year'), ref.get('ids'))
        if media_id is None:
            return self._skipped()
        return self._apply(media_id, score=int(value) * 10)

    # ------------------------------------------------------------------ sync / manual writes (neutral payloads in)
    def _each(self, payload, fn):
        """Run fn(kind, item, media_id) for every matched item; -> a Result with Simkl-style counts."""
        added, not_found = {'movies': 0, 'shows': 0}, {'movies': [], 'shows': []}
        for key, kind in (('movies', 'movie'), ('shows', 'show')):
            for it in payload.get(key) or []:
                media_id = self.media_id(kind, it.get('title'), it.get('year'), it.get('ids'))
                if media_id is None:
                    not_found[key].append(it)
                    continue
                res = fn(kind, it, media_id)
                if res is not None and not res.ok:
                    return res
                added[key] += 1
        return net.Result(status=200, body={'added': added, 'not_found': {k: v for k, v in not_found.items() if v}})

    @staticmethod
    def _first_season(item):
        """-> (highest episode watched in season 1, its date) or ('all', date) for a whole show / season 1 marker."""
        seasons = item.get('seasons') or []
        if not seasons:
            return 'all', item.get('watched_at')
        for season in seasons:
            if season.get('number') != 1:
                continue
            eps = season.get('episodes') or []
            if not eps:
                return 'all', season.get('watched_at')
            top = max(eps, key=lambda e: e.get('number') or 0)
            return top.get('number'), top.get('watched_at')
        return None, None

    def add_history(self, payload):
        def one(kind, it, media_id):
            if kind == 'movie':
                return self._apply(media_id, 1, it.get('watched_at'))
            progress, when = self._first_season(it)
            return self._apply(media_id, progress, when) if progress else None

        return self._each(payload, one)

    def add_ratings(self, payload):
        def one(kind, it, media_id):
            return self._apply(media_id, score=int(it['rating']) * 10) if it.get('rating') else None

        return self._each(payload, one)

    def _entry_id(self, media_id):
        media = self._data(self._gql(Q_ENTRY, {'id': media_id})).get('Media') or {}
        entry = media.get('mediaListEntry') or {}
        return entry.get('id'), entry.get('progress') or 0

    def remove_history(self, payload):
        """Un-watch: a movie / whole season 1 deletes the list entry (that also drops its score, as the editor
        wants); single episodes only lower the progress (to the episode before the lowest one removed)."""
        def one(kind, it, media_id):
            entry_id, progress = self._entry_id(media_id)
            if not entry_id:
                return None
            lowest = None
            if kind == 'show':
                for season in it.get('seasons') or []:
                    if season.get('number') == 1 and season.get('episodes'):
                        lowest = min(e.get('number') or 0 for e in season['episodes'])
            if lowest is None or lowest <= 1:
                return self._gql(M_DELETE, {'id': entry_id})
            if progress >= lowest:
                return self._gql(M_SAVE, {'mediaId': media_id, 'progress': lowest - 1, 'status': 'CURRENT'})
            return None

        return self._each(payload, one)

    def remove_ratings(self, payload):
        def one(kind, it, media_id):
            return self._gql(M_SAVE, {'mediaId': media_id, 'scoreRaw': 0})

        return self._each(payload, one)

    # ------------------------------------------------------------------ sync reads (AniList -> others)
    def read_all(self):
        """Your anime list -> (history, ratings) neutral payloads. First season only; titles + year + anilist/mal ids.
        Raises RuntimeError when AniList cannot be read."""
        viewer = self._data(self._gql(Q_VIEWER)).get('Viewer') or {}
        if not viewer.get('id'):
            raise RuntimeError('AniList did not answer')
        res = self._gql(Q_LIST, {'userId': viewer['id']})
        if not res.ok:
            raise RuntimeError('AniList answered {}'.format(res.status))
        movies_h, shows_h, movies_r, shows_r, seen = [], [], [], [], set()
        for group in (self._data(res).get('MediaListCollection') or {}).get('lists') or []:
            for entry in group.get('entries') or []:
                media = entry.get('media') or {}
                if not media.get('id') or media['id'] in seen:
                    continue
                seen.add(media['id'])
                title = (media.get('title') or {}).get('english') or (media.get('title') or {}).get('romaji')
                ids = {'anilist': media['id']}
                if media.get('idMal'):
                    ids['mal'] = media['idMal']
                is_movie = media.get('format') == 'MOVIE'
                item = syncmap.media({'title': title, 'year': (media.get('startDate') or {}).get('year') or media.get('seasonYear'),
                                      'ids': ids}, 'movie' if is_movie else 'show')
                if not item or media.get('format') not in (('MOVIE',) + SHOW_FORMATS):
                    continue
                done = _stamp_from_fuzzy(entry.get('completedAt'))
                progress = entry.get('progress') or 0
                if is_movie and (entry.get('status') in ('COMPLETED', 'REPEATING') or progress >= 1):
                    movies_h.append(dict(item, **({'watched_at': done} if done else {})))
                elif not is_movie and progress > 0:
                    shows_h.append(dict(item, seasons=[{'number': 1, 'episodes': [
                        dict({'number': n}, **({'watched_at': done} if done and entry.get('status') == 'COMPLETED' else {}))
                        for n in range(1, progress + 1)]}]))
                if entry.get('score'):
                    rating = max(1, min(10, syncmap.round_half_up(entry['score'] / 10.0)))
                    (movies_r if is_movie else shows_r).append(dict(item, rating=rating))
        history = {k: v for k, v in (('movies', movies_h), ('shows', shows_h)) if v}
        ratings = {k: v for k, v in (('movies', movies_r), ('shows', shows_r)) if v}
        return history, ratings


PROVIDER = AniList()
