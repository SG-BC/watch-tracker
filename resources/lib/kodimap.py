"""Kodi library <-> neutral (Simkl-shaped) payloads. Pure Python (no Kodi imports) so it is unit-tested.

Kodi stores `lastplayed` as local time ('YYYY-MM-DD HH:MM:SS'); Simkl uses UTC. Both directions convert
the SAME instant. Everything is add-only: nothing is ever un-watched, overwritten or deleted.
"""
import calendar
import time

try:
    from . import parsing, syncmap
except ImportError:          # imported as a top-level module by the unit tests
    import parsing, syncmap


def kodi_to_utc(value):
    """'2026-10-22 01:31:05' (local time) -> '2026-10-22T08:31:05Z'. None for empty / zero dates."""
    value = (value or '').strip()
    if len(value) < 19 or value.startswith('0000') or value.startswith('1970-01-01 00'):
        return None
    try:
        epoch = time.mktime(time.strptime(value[:19], '%Y-%m-%d %H:%M:%S'))
    except (ValueError, OverflowError):
        return None
    return time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(epoch))


def utc_to_kodi(value):
    """UTC ISO date -> Kodi's local 'YYYY-MM-DD HH:MM:SS'. None when unusable."""
    ts = syncmap.normalize_ts(value)
    if not ts:
        return None
    return time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(calendar.timegm(time.strptime(ts, '%Y-%m-%dT%H:%M:%SZ'))))


def _ids(row, kind):
    raw = dict(row.get('uniqueid') or {})
    imdb = str(row.get('imdbnumber') or '')
    if imdb.startswith('tt'):
        raw.setdefault('imdb', imdb)
    return {k: v for k, v in parsing.normalize_ids(raw, kind).items() if k in syncmap.SIMKL_KEYS[kind]}


def _item(row, kind):
    """Kodi movie/show row -> {'title','year','ids'} (None when it can't be told apart)."""
    return syncmap.media({'title': row.get('title'), 'year': row.get('year') or None, 'ids': _ids(row, kind)}, kind)


def _rating(value):
    try:
        value = int(round(float(value)))
    except (TypeError, ValueError):
        return None
    return value if 1 <= value <= 10 else None


# ------------------------------------------------------------------------------ Kodi -> Simkl
def kodi_to_simkl(movies, shows, episodes, since, stats):
    """-> (history payload, ratings payload). `since`: only items played after this UTC date are sent
    (None = everything). Ratings are sent with the watch they belong to (Kodi has no rating dates)."""
    def fresh(ts):
        return since is None or (ts is not None and ts > since)

    hist_movies, rate_movies, grouped, show_dates = [], [], {}, {}
    for row in movies:
        if (row.get('playcount') or 0) <= 0:
            continue
        ts = kodi_to_utc(row.get('lastplayed'))
        if not fresh(ts):
            continue
        item = _item(row, 'movie')
        if item is None:
            stats['unmatched'] += 1
            continue
        hist_movies.append(dict(item, **({'watched_at': ts} if ts else {})))
        stats['movies'] += 1
        stats['dated' if ts else 'undated'] += 1
        rating = _rating(row.get('userrating'))
        if rating:
            rate_movies.append(dict(item, rating=rating, **({'rated_at': ts} if ts else {})))
            stats['ratings'] += 1

    by_show = {s.get('tvshowid'): s for s in shows}
    for row in episodes:
        if (row.get('playcount') or 0) <= 0:
            continue
        ts = kodi_to_utc(row.get('lastplayed'))
        if not fresh(ts):
            continue
        show = by_show.get(row.get('tvshowid'))
        item = _item(show, 'show') if show else None
        if item is None:
            stats['unmatched'] += 1
            continue
        key = syncmap.show_key(item)
        group = grouped.setdefault(key, {'item': item, 'seasons': {}})
        season = group['seasons'].setdefault(row.get('season'), {'number': row.get('season'), 'episodes': []})
        season['episodes'].append(dict({'number': row.get('episode')}, **({'watched_at': ts} if ts else {})))
        stats['episodes'] += 1
        stats['dated' if ts else 'undated'] += 1
        if ts and ts > show_dates.get(key, ''):
            show_dates[key] = ts
        if _rating(row.get('userrating')):
            stats['ignored'] += 1                      # Simkl has no episode ratings

    rate_shows = []
    for show in shows:                                  # a show rating travels with the show's watched episodes
        item = _item(show, 'show')
        rating = _rating(show.get('userrating'))
        if item and rating and syncmap.show_key(item) in grouped:
            rated_at = show_dates.get(syncmap.show_key(item))
            rate_shows.append(dict(item, rating=rating, **({'rated_at': rated_at} if rated_at else {})))
            stats['ratings'] += 1

    history = {}
    if hist_movies:
        history['movies'] = hist_movies
    shows_out = [dict(g['item'], seasons=list(g['seasons'].values())) for g in grouped.values()]
    if shows_out:
        history['shows'] = shows_out
    ratings = {}
    if rate_movies:
        ratings['movies'] = rate_movies
    if rate_shows:
        ratings['shows'] = rate_shows
    return history, ratings


# ------------------------------------------------------------------------------ Simkl -> Kodi
def _tokens(item):
    tokens = set(syncmap.item_tokens(item))
    if item.get('title') and item.get('year'):
        tokens.add(('t', parsing.normalize(item['title']), item['year']))
    return tokens


class KodiIndex:
    """The Kodi library, searchable by any shared id (or normalised title + year)."""

    def __init__(self, movies, shows, episodes):
        self._movies, self._shows, self._episodes = {}, {}, {}
        for row in movies:
            item = _item(row, 'movie')
            for t in (_tokens(item) if item else ()):
                self._movies.setdefault(t, row)
        for row in shows:
            item = _item(row, 'show')
            for t in (_tokens(item) if item else ()):
                self._shows.setdefault(t, row)
        for row in episodes:
            self._episodes.setdefault(row.get('tvshowid'), {})[(row.get('season'), row.get('episode'))] = row

    def movie(self, item):
        return next((self._movies[t] for t in _tokens(item) if t in self._movies), None)

    def show(self, item):
        return next((self._shows[t] for t in _tokens(item) if t in self._shows), None)

    def episodes(self, tvshowid):
        return self._episodes.get(tvshowid, {})


def simkl_to_kodi(rows, index, stats):
    """rows: [(kind, status, simkl row)]. -> [(JSON-RPC method, params)] that ADD what Kodi lacks:
    watched status + date, and a rating only where Kodi has none."""
    updates = []
    for kind, status, row in rows:
        if not isinstance(row, dict):
            continue
        if kind == 'movie':
            item = syncmap.media(row.get('movie'), 'movie')
            record = index.movie(item) if item else None
            if record is None:
                stats['not_in_kodi'] += 1
                continue
            upd = {}
            if status == 'completed' and (record.get('playcount') or 0) == 0:
                upd['playcount'] = 1
                when = utc_to_kodi(row.get('last_watched_at'))
                if when:
                    upd['lastplayed'] = when
                stats['movies'] += 1
            rating = _rating(row.get('user_rating'))
            if rating and not record.get('userrating'):
                upd['userrating'] = rating
                stats['ratings'] += 1
            if upd:
                updates.append(('VideoLibrary.SetMovieDetails', dict(upd, movieid=record['movieid'])))
            continue

        item = syncmap.media(row.get('show') or row.get('anime'), 'show')
        record = index.show(item) if item else None
        if record is None:
            stats['not_in_kodi'] += 1
            continue
        library = index.episodes(record['tvshowid'])
        if status == 'rated_episodes':                  # episode ratings only: never marks anything watched
            for season in row.get('seasons') or []:
                for ep in season.get('episodes') or []:
                    e = library.get((season.get('number'), ep.get('number')))
                    rating = _rating(ep.get('rating'))
                    if e is not None and rating and not e.get('userrating'):
                        updates.append(('VideoLibrary.SetEpisodeDetails', {'episodeid': e['episodeid'], 'userrating': rating}))
                        stats['episode_ratings'] += 1
            continue
        default = utc_to_kodi(row.get('last_watched_at'))
        targets = []                                    # (kodi episode row, local date)
        listed = row.get('listed_means_watched')        # rows built from a sync payload: every listed episode counts
        if status == 'completed':                       # every episode is watched
            targets = [(e, default) for e in library.values()]
        else:                                           # only episodes that carry a watch date
            for season in row.get('seasons') or []:
                if listed and not season.get('episodes'):               # a whole-season marker
                    targets += [(e, default) for (sn, _), e in library.items() if sn == season.get('number')]
                for ep in season.get('episodes') or []:
                    if ep.get('watched_at') or listed:
                        e = library.get((season.get('number'), ep.get('number')))
                        if e is not None:
                            targets.append((e, utc_to_kodi(ep.get('watched_at')) or default))
        for e, when in targets:
            if (e.get('playcount') or 0) == 0:
                upd = {'episodeid': e['episodeid'], 'playcount': 1}
                if when:
                    upd['lastplayed'] = when
                updates.append(('VideoLibrary.SetEpisodeDetails', upd))
                stats['episodes'] += 1
        rating = _rating(row.get('user_rating'))
        if rating and not record.get('userrating'):
            updates.append(('VideoLibrary.SetTVShowDetails', {'tvshowid': record['tvshowid'], 'userrating': rating}))
            stats['ratings'] += 1
    return updates


# ------------------------------------------------------------------------------ neutral payloads <-> rows
def neutral_to_rows(history, ratings):
    """Simkl-shaped history / ratings payloads (what a sync would send) -> rows for simkl_to_kodi.
    Watched entries mark Kodi items watched; rating-only rows never mark anything watched."""
    rows = []
    for item in history.get('movies') or []:
        rows.append(('movie', 'completed', {'movie': item, 'last_watched_at': item.get('watched_at')}))
    for item in history.get('shows') or []:
        seasons = item.get('seasons') or []
        if not seasons:
            rows.append(('show', 'completed', {'show': item, 'last_watched_at': item.get('watched_at')}))
        else:
            rows.append(('show', 'watching', {'show': item, 'seasons': seasons, 'listed_means_watched': True}))
    for key, kind in (('movies', 'movie'), ('shows', 'show')):
        for item in ratings.get(key) or []:
            if kind == 'show' and item.get('seasons'):           # episode ratings (Kodi keeps them per episode)
                rows.append(('show', 'rated_episodes', {'show': {k: v for k, v in item.items() if k != 'seasons'},
                                                        'seasons': item['seasons']}))
            else:
                rows.append((kind, 'rated', {kind: item, 'user_rating': item.get('rating')}))
    return rows


def simkl_rows_to_neutral(rows, stats):
    """Rows read from Simkl [(kind, status, row)] -> (history, ratings) payloads for another service."""
    movies_h, shows_h, movies_r, shows_r = [], [], [], []
    for kind, status, row in rows:
        if not isinstance(row, dict):
            continue
        item = syncmap.media(row.get('movie') if kind == 'movie' else (row.get('show') or row.get('anime')), kind)
        if item is None:
            stats['unmatched'] += 1
            continue
        last = syncmap.normalize_ts(row.get('last_watched_at'))
        rating = _rating(row.get('user_rating'))
        rated_at = syncmap.normalize_ts(row.get('user_rated_at'))
        if kind == 'movie':
            if status == 'completed':
                movies_h.append(dict(item, **({'watched_at': last} if last else {})))
                stats['movies'] += 1
            if rating:
                movies_r.append(dict(item, rating=rating, **({'rated_at': rated_at} if rated_at else {})))
                stats['ratings'] += 1
            continue
        seasons = []
        for season in row.get('seasons') or []:
            eps = [dict({'number': e.get('number')}, watched_at=syncmap.normalize_ts(e.get('watched_at')))
                   for e in season.get('episodes') or [] if e.get('watched_at')]
            if eps:
                seasons.append({'number': season.get('number'), 'episodes': eps})
                stats['episodes'] += len(eps)
        if seasons:
            shows_h.append(dict(item, seasons=seasons))
        elif status == 'completed':
            shows_h.append(dict(item, **({'watched_at': last} if last else {})))      # whole show
            stats['shows'] += 1
        if rating:
            shows_r.append(dict(item, rating=rating, **({'rated_at': rated_at} if rated_at else {})))
            stats['ratings'] += 1
    history = {k: v for k, v in (('movies', movies_h), ('shows', shows_h)) if v}
    ratings = {k: v for k, v in (('movies', movies_r), ('shows', shows_r)) if v}
    return history, ratings
