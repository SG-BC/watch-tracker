"""Plex library rows <-> neutral (Simkl-shaped) payloads. Pure Python (no Kodi imports) so it is unit-tested.

Plex items are turned into Kodi-shaped rows (movieid / tvshowid / episodeid = the Plex ratingKey, playcount =
viewCount, userrating = userRating, uniqueid = the imdb/tmdb/tvdb Guids). That lets the Kodi code match and
decide on them unchanged: kodimap.KodiIndex finds items, kodimap.simkl_to_kodi decides what to add (watched
only where unwatched, a rating only where there is none). `actions` turns its updates into Plex calls.

Plex -> services: one watch per item across all servers (the same item on two servers counts once). Its date
is the newest real play from the servers' history; an item watched without any history entry (marked by hand,
or copied by Plex's own sync) gets the EARLIEST lastViewedAt, so a copy cannot make an old watch look new.
A rating: the most recently rated value wins."""
import calendar
import time

try:
    from . import kodimap, syncmap
except ImportError:          # imported as a top-level module by the unit tests
    import kodimap, syncmap

GUIDS = ('imdb', 'tmdb', 'tvdb')


def guid_ids(meta):
    """Plex `Guid` list ([{'id': 'imdb://tt0088763'}, ...]) -> {'imdb': 'tt0088763', 'tmdb': '105', ...}."""
    out = {}
    for g in (meta or {}).get('Guid') or []:
        value = str((g or {}).get('id') or '')
        if '://' in value:
            prov, rest = value.split('://', 1)
            if prov in GUIDS and rest:
                out[prov] = rest
    return out


def _num(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _epoch(value):
    n = _num(value)
    return int(n) if n > 0 else None


def iso(epoch):
    return time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(epoch))


def to_epoch(value):
    """UTC ISO date -> epoch seconds, None when unusable."""
    ts = syncmap.normalize_ts(value)
    return calendar.timegm(time.strptime(ts, '%Y-%m-%dT%H:%M:%SZ')) if ts else None


# ------------------------------------------------------------------------------ Plex rows -> Kodi-shaped rows
def movie_row(m):
    return {'movieid': str(m.get('ratingKey')), 'title': m.get('title'), 'year': m.get('year'),
            'uniqueid': guid_ids(m), 'playcount': int(_num(m.get('viewCount'))), 'userrating': _num(m.get('userRating')),
            'lastviewed': _epoch(m.get('lastViewedAt')), 'lastrated': _epoch(m.get('lastRatedAt'))}


def show_row(s):
    return {'tvshowid': str(s.get('ratingKey')), 'title': s.get('title'), 'year': s.get('year'),
            'uniqueid': guid_ids(s), 'userrating': _num(s.get('userRating')), 'lastrated': _epoch(s.get('lastRatedAt'))}


def episode_row(e):
    return {'episodeid': str(e.get('ratingKey')), 'tvshowid': str(e.get('grandparentRatingKey')),
            'season': _int(e.get('parentIndex')), 'episode': _int(e.get('index')),
            'playcount': int(_num(e.get('viewCount'))), 'userrating': _num(e.get('userRating')),
            'lastviewed': _epoch(e.get('lastViewedAt'))}


def _int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


class Library:
    """What was read from the chosen libraries of ONE server."""

    def __init__(self, name=''):
        self.name = name
        self.movies, self.shows, self.episodes = [], [], []
        self.plays = {}                         # ratingKey -> newest viewedAt (epoch) from the history

    def add_movies(self, rows):
        self.movies += [movie_row(m) for m in rows if isinstance(m, dict) and m.get('ratingKey')]

    def add_shows(self, rows):
        self.shows += [show_row(s) for s in rows if isinstance(s, dict) and s.get('ratingKey')]

    def add_episodes(self, rows):
        self.episodes += [episode_row(e) for e in rows if isinstance(e, dict) and e.get('ratingKey')]

    def add_history(self, rows):
        for h in rows:
            if not isinstance(h, dict):
                continue
            key = str(h.get('ratingKey') or str(h.get('key') or '').rsplit('/', 1)[-1])
            when = _epoch(h.get('viewedAt'))
            if key and when and when > self.plays.get(key, 0):
                self.plays[key] = when

    def index(self):
        return kodimap.KodiIndex(self.movies, self.shows, self.episodes)

    def empty(self):
        return not (self.movies or self.episodes)


# ------------------------------------------------------------------------------ services -> Plex
def actions(updates):
    """kodimap.simkl_to_kodi updates (run with dated=True) -> [('scrobble', key, None) | ('rate', key, (value, epoch))].
    Plex keeps no watch date, so `lastplayed` is dropped."""
    out = []
    for _method, p in updates:
        key = p.get('movieid') or p.get('episodeid') or p.get('tvshowid')
        if key is None:
            continue
        if p.get('playcount'):
            out.append(('scrobble', key, None))
        if p.get('userrating'):
            out.append(('rate', key, (p['userrating'], to_epoch(p.get('rated_at')))))
    return out


# ------------------------------------------------------------------------------ Plex -> services
class _Merge:
    """Items from several servers, merged when they share any id (or title + year)."""

    def __init__(self, combine):
        self.combine = combine
        self.entries = []                       # [item, extra, value]
        self._at = {}

    def add(self, item, extra, value):
        tokens = [(t, extra) for t in kodimap._tokens(item)]
        hit = next((self._at[t] for t in tokens if t in self._at), None)
        if hit is None:
            hit = len(self.entries)
            self.entries.append([dict(item, **({'ids': dict(item['ids'])} if item.get('ids') else {})), extra, value])
        else:
            entry = self.entries[hit]
            for k, v in (item.get('ids') or {}).items():
                entry[0].setdefault('ids', {}).setdefault(k, v)
            entry[2] = self.combine(entry[2], value)
        for t in tokens:
            self._at.setdefault(t, hit)


def _watch(a, b):
    """(newest history play, earliest lastViewedAt) of two copies of the same watch."""
    hist = max([x for x in (a[0], b[0]) if x] or [None])
    manual = min([x for x in (a[1], b[1]) if x] or [None])
    return hist, manual


def _newest_rating(a, b):
    return b if (b[1] or 0) > (a[1] or 0) else a


def _when(value):
    hist, manual = value
    return hist or manual


def plex_to_neutral(libraries, since, stats):
    """Libraries of several servers -> (history, ratings) neutral payloads. `since` (UTC ISO): only watches and
    ratings newer than that (undated ones only on the first run). Episode ratings are counted as ignored
    (the services take movie and show ratings from a sync)."""
    def fresh(epoch):
        return since is None if epoch is None else (since is None or iso(epoch) > since)

    movies, episodes = _Merge(_watch), _Merge(_watch)
    rmovies, rshows = _Merge(_newest_rating), _Merge(_newest_rating)
    for lib in libraries:
        shows = {s['tvshowid']: s for s in lib.shows}
        for m in lib.movies:
            if not (m['playcount'] or kodimap._rating(m['userrating'])):
                continue
            item = kodimap._item(m, 'movie')
            if item is None:
                stats['unmatched'] += 1
                continue
            if m['playcount'] > 0:
                movies.add(item, None, (lib.plays.get(m['movieid']), m.get('lastviewed')))
            rating = kodimap._rating(m['userrating'])
            if rating:
                rmovies.add(item, None, (rating, m.get('lastrated')))
        for s in lib.shows:
            rating = kodimap._rating(s['userrating'])
            item = kodimap._item(s, 'show') if rating else None
            if item is not None:
                rshows.add(item, None, (rating, s.get('lastrated')))
        for e in lib.episodes:
            if kodimap._rating(e['userrating']):
                stats['ignored'] += 1
            if e['playcount'] <= 0 or e['season'] is None or e['episode'] is None:
                continue
            show = shows.get(e['tvshowid'])
            item = kodimap._item(show, 'show') if show else None
            if item is None:
                stats['unmatched'] += 1
                continue
            episodes.add(item, (e['season'], e['episode']), (lib.plays.get(e['episodeid']), e.get('lastviewed')))

    history_movies = []
    for item, _, value in movies.entries:
        when = _when(value)
        if fresh(when):
            history_movies.append(dict(item, **({'watched_at': iso(when)} if when else {})))
            stats['movies'] += 1
            stats['dated' if when else 'undated'] += 1
    grouped = {}
    for item, (season, number), value in episodes.entries:
        when = _when(value)
        if not fresh(when):
            continue
        group = grouped.setdefault(syncmap.show_key(item), {'item': item, 'seasons': {}})
        group['seasons'].setdefault(season, {'number': season, 'episodes': []})['episodes'].append(
            dict({'number': number}, **({'watched_at': iso(when)} if when else {})))
        stats['episodes'] += 1
        stats['dated' if when else 'undated'] += 1
    history = {}
    if history_movies:
        history['movies'] = history_movies
    shows_out = [dict(g['item'], seasons=sorted(g['seasons'].values(), key=lambda s: s['number'])) for g in grouped.values()]
    if shows_out:
        history['shows'] = shows_out

    ratings = {}
    for key, merged in (('movies', rmovies), ('shows', rshows)):
        rows = []
        for item, _, (value, when) in merged.entries:
            if fresh(when):
                rows.append(dict(item, rating=value, **({'rated_at': iso(when)} if when else {})))
                stats['ratings'] += 1
        if rows:
            ratings[key] = rows
    return history, ratings
