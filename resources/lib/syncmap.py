"""MDBList -> Simkl conversion for the manual sync. Pure Python (no Kodi imports) so it is unit-tested.

MDBList rows (GET /sync/watched, /sync/ratings) come as separate arrays: movies[], shows[], seasons[],
episodes[]. The schema leaves season/episode row layouts open, so parsing is defensive and anything
unrecognised is counted as skipped (never guessed).
"""
import datetime
import math
import re

try:
    from . import parsing
except ImportError:        # imported as a top-level module by the unit tests
    import parsing

SIMKL_KEYS = {'movie': ('imdb', 'tmdb'), 'show': ('imdb', 'tmdb', 'tvdb')}


def round_half_up(value):
    """5.5 -> 6, 6.5 -> 7 (Python's round() would give 6 for 6.5)."""
    return int(math.floor(float(value) + 0.5))


def _num(value):
    if isinstance(value, dict):
        value = value.get('number')
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def media(obj, kind):
    """{'title','year','ids'} -> Simkl item, or None when it can't be matched reliably."""
    if not isinstance(obj, dict):
        return None
    item = {}
    ids = {k: v for k, v in parsing.normalize_ids(obj.get('ids') or {}, kind).items() if k in SIMKL_KEYS[kind]}
    if ids:
        item['ids'] = ids
    if obj.get('title'):
        item['title'] = obj['title']
    try:
        year = int(obj.get('year'))
        if year > 0:
            item['year'] = year
    except (TypeError, ValueError):
        pass
    return item if item.get('ids') or ('title' in item and 'year' in item) else None


def show_key(item):
    return tuple(sorted(item.get('ids', {}).items())) or (item.get('title'), item.get('year'))


def key_tokens(key):
    """The individual ids inside a show_key ((prov, value) pairs), or the (title, year) pair itself."""
    return set(key) if key and isinstance(key[0], tuple) else {key}


def item_tokens(item):
    return key_tokens(show_key(item))


_TS = re.compile(r'^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})(?::(\d{2}))?(?:\.\d+)?\s*(Z|[+-]\d{2}:?\d{2})?$')


def normalize_ts(value):
    """ISO-8601 -> 'YYYY-MM-DDTHH:MM:SSZ' (UTC) for the SAME instant. Offsets such as -08:00 are
    converted, never dropped. None when there is no usable date (missing, garbage, or epoch zero)."""
    if not value or not isinstance(value, str):
        return None
    m = _TS.match(value.strip())
    if not m:
        return None
    year, month, day, hour, minute, second, tz = m.groups()
    try:
        dt = datetime.datetime(int(year), int(month), int(day), int(hour), int(minute), int(second or 0))
    except ValueError:
        return None
    if tz and tz != 'Z':
        digits = tz[1:].replace(':', '')
        offset = datetime.timedelta(hours=int(digits[:2]), minutes=int(digits[2:4]))
        dt = dt - offset if tz[0] == '+' else dt + offset
    if dt.year <= 1970:
        return None
    return dt.strftime('%Y-%m-%dT%H:%M:%SZ')


def row_date(row):
    """Watch date of a row: play rows carry `watched_at`, plain item rows `last_watched_at`."""
    if not isinstance(row, dict):
        return None
    return normalize_ts(row.get('watched_at') or row.get('last_watched_at'))


def track_date(stats, ts):
    """Count dated/undated items and remember the date range (for the preview and the summary)."""
    if not ts:
        stats['undated'] += 1
        return
    stats['dated'] += 1
    if not stats.get('oldest') or ts < stats['oldest']:
        stats['oldest'] = ts
    if not stats.get('newest') or ts > stats['newest']:
        stats['newest'] = ts


def _episode_row(row):
    """-> (show item, season, number) or None."""
    if not isinstance(row, dict):
        return None
    ep = row.get('episode')
    if isinstance(ep, dict):
        number, season = _num(ep.get('number')), _num(ep.get('season'))
        show = ep.get('show') or row.get('show')
    else:
        number, season, show = _num(row.get('number')), _num(row.get('season')), row.get('show')
    item = media(show, 'show')
    if item is None or number is None or season is None:
        return None
    return item, season, number


def _movie_row(row):
    return media(row.get('movie') if isinstance(row, dict) else None, 'movie')


class PlayIndex:
    """Pass 1: MDBList play rows (`plays=all`, one row per play with that play's own date). Keeps the
    most recent play of each movie/episode; Simkl (free) records one watch per item."""

    def __init__(self):
        self.movies = {}       # key -> (ts, item)
        self.episodes = {}     # (show_key, season, number) -> (ts, show item)

    @staticmethod
    def _keep(table, key, ts, item):
        cur = table.get(key)
        if cur is None or (ts and (not cur[0] or ts > cur[0])):
            table[key] = (ts, item)

    def add_page(self, page, stats):
        for row in page.get('movies') or []:
            item = _movie_row(row)
            if item is None:
                stats['skipped'] += 1
                continue
            self._keep(self.movies, show_key(item), row_date(row), item)
        for row in page.get('episodes') or []:
            parsed = _episode_row(row)
            if parsed is None:
                stats['skipped'] += 1
                continue
            item, season, number = parsed
            self._keep(self.episodes, (show_key(item), season, number), row_date(row), item)

    def payload(self, stats):
        movies, grouped = [], {}
        for ts, item in self.movies.values():
            entry = dict(item)
            if ts:
                entry['watched_at'] = ts
            movies.append(entry)
            stats['movies'] += 1
            track_date(stats, ts)
        for (key, season, number), (ts, item) in sorted(self.episodes.items(), key=lambda kv: (str(kv[0][0]), kv[0][1], kv[0][2])):
            group = grouped.setdefault(key, {'item': item, 'seasons': {}})
            season_entry = group['seasons'].setdefault(season, {'number': season, 'episodes': []})
            episode = {'number': number}
            if ts:
                episode['watched_at'] = ts
            season_entry['episodes'].append(episode)
            stats['episodes'] += 1
            track_date(stats, ts)
        out = {}
        if movies:
            out['movies'] = movies
        shows = [dict(g['item'], seasons=list(g['seasons'].values())) for g in grouped.values()]
        if shows:
            out['shows'] = shows
        return out


class Context:
    """Pass 2 (plain item rows). Skips what pass 1 already covered, uses an item's own date when it
    has one (otherwise it goes in as watched today), and holds whole-show / whole-season markers
    back until the end: they are sent only when there are NO episode rows for that show/season."""

    def __init__(self, index=None):
        self.covered_movies = set(index.movies) if index else set()
        self.covered_episodes = set(index.episodes) if index else set()
        self.eps_shows = {k[0] for k in self.covered_episodes}
        self.eps_seasons = {(k[0], k[1]) for k in self.covered_episodes}
        self.pending_shows = {}
        self.pending_seasons = {}
        self.seen_movies = set()      # every id of everything MDBList lists as watched, whether or not it
        self.seen_shows = set()       # had to be sent again (an item matches on ANY shared id)
        for key in self.covered_movies:
            self.seen_movies |= key_tokens(key)
        for key in self.eps_shows:
            self.seen_shows |= key_tokens(key)

    def markers(self, stats):
        shows, whole = [], set()
        for key, (item, ts) in self.pending_shows.items():
            if key in self.eps_shows:
                continue
            entry = dict(item)
            if ts:
                entry['watched_at'] = ts
            shows.append(entry)
            whole.add(key)
            stats['shows'] += 1
            track_date(stats, ts)
        grouped = {}
        for (key, number), (item, ts) in self.pending_seasons.items():
            if key in whole or (key, number) in self.eps_seasons:
                continue
            group = grouped.setdefault(key, {'item': item, 'seasons': [], 'ts': ts})
            group['seasons'].append({'number': number})
            stats['seasons'] += 1
            track_date(stats, ts)
        for group in grouped.values():
            entry = dict(group['item'], seasons=group['seasons'])
            if group['ts']:
                entry['watched_at'] = group['ts']
            shows.append(entry)
        return {'shows': shows} if shows else {}


def history_items_to_simkl(page, stats, ctx):
    """Pass 2 page -> Simkl payload for movies and episodes (markers are collected in ctx)."""
    movies, grouped = [], {}

    for row in page.get('movies') or []:
        item = _movie_row(row)
        if item is None:
            stats['skipped'] += 1
            continue
        ctx.seen_movies |= item_tokens(item)
        if show_key(item) in ctx.covered_movies:
            continue
        ts = row_date(row)
        if ts:
            item['watched_at'] = ts
        movies.append(item)
        stats['movies'] += 1
        track_date(stats, ts)

    for row in page.get('shows') or []:
        item = media(row.get('show') if isinstance(row, dict) else None, 'show')
        if item is None:
            stats['skipped'] += 1
            continue
        ctx.seen_shows |= item_tokens(item)
        ctx.pending_shows[show_key(item)] = (item, row_date(row))

    for row in page.get('seasons') or []:
        if not isinstance(row, dict):
            stats['skipped'] += 1
            continue
        se = row.get('season')
        number = _num(se) if se is not None else _num(row.get('number'))
        item = media((se.get('show') if isinstance(se, dict) else None) or row.get('show'), 'show')
        if item is None or number is None:
            stats['skipped'] += 1
            continue
        ctx.seen_shows |= item_tokens(item)
        ctx.pending_seasons[(show_key(item), number)] = (item, row_date(row))

    for row in page.get('episodes') or []:
        parsed = _episode_row(row)
        if parsed is None:
            stats['skipped'] += 1
            continue
        item, season, number = parsed
        key = show_key(item)
        ctx.eps_shows.add(key)
        ctx.seen_shows |= key_tokens(key)
        ctx.eps_seasons.add((key, season))
        if (key, season, number) in ctx.covered_episodes:
            continue
        group = grouped.setdefault(key, {'item': item, 'seasons': {}})
        season_entry = group['seasons'].setdefault(season, {'number': season, 'episodes': []})
        episode = {'number': number}
        ts = row_date(row)
        if ts:
            episode['watched_at'] = ts
        season_entry['episodes'].append(episode)
        stats['episodes'] += 1
        track_date(stats, ts)

    payload = {}
    if movies:
        payload['movies'] = movies
    shows = [dict(g['item'], seasons=list(g['seasons'].values())) for g in grouped.values()]
    if shows:
        payload['shows'] = shows
    return payload


def ratings_page(page, stats, seen_movies=frozenset(), seen_shows=frozenset()):
    """One MDBList ratings page -> (ratings, history, lists) Simkl payloads. Movies and shows only
    (Simkl has no season/episode ratings); 5.5 -> 6.

    Rating something Simkl does not know yet files it at that moment ("today"). So a rated item that
    is NOT in the watch history gets a back-dated entry first, using its rating date: a movie as a
    watch (what Simkl itself would do: it files a rated movie as completed), a show as 'watching'
    with its list date set. Items already in the history keep their real dates."""
    ratings = {'movies': [], 'shows': []}
    history, lists = [], []
    for key, kind, field in (('movies', 'movie', 'movie'), ('shows', 'show', 'show')):
        for row in page.get(key) or []:
            item = media(row.get(field) if isinstance(row, dict) else None, kind)
            value = None
            if isinstance(row, dict):
                raw = row.get('rating_precise', row.get('rating'))
                try:
                    value = round_half_up(raw) if raw is not None else None
                except (TypeError, ValueError):
                    value = None
            if item is None or value is None or not 1 <= value <= 10:
                stats['skipped'] += 1
                continue
            entry = dict(item, rating=value)
            rated_at = normalize_ts(row.get('rated_at'))
            if rated_at:
                entry['rated_at'] = rated_at
                if kind == 'movie' and not (item_tokens(item) & seen_movies):
                    history.append(dict(item, watched_at=rated_at))
                    stats['backdated_movies'] += 1
                elif kind == 'show' and not (item_tokens(item) & seen_shows):
                    lists.append(dict(item, to='watching', added_at=rated_at))
                    stats['backdated_shows'] += 1
            ratings[key].append(entry)
            stats[key] += 1
    stats['ignored'] += len(page.get('seasons') or []) + len(page.get('episodes') or [])
    return ({k: v for k, v in ratings.items() if v},
            {'movies': history} if history else {},
            {'shows': lists} if lists else {})


def ratings_to_simkl(page, stats):
    return ratings_page(page, stats)[0]


# ----------------------------------------------------------------------------- batching

def _units(payload):
    units = [('m', m) for m in payload.get('movies', [])]
    for s in payload.get('shows', []):
        meta = {k: v for k, v in s.items() if k != 'seasons'}
        seasons = s.get('seasons')
        if not seasons:
            units.append(('s', meta))
            continue
        for se in seasons:
            eps = se.get('episodes')
            if not eps:
                units.append(('se', meta, se['number']))
            else:
                for e in eps:
                    units.append(('e', meta, se['number'], e))
    return units


def _build(units):
    out = {}
    movies, shows, grouped = [], [], {}
    for u in units:
        if u[0] == 'm':
            movies.append(u[1])
        elif u[0] == 's':
            shows.append(dict(u[1]))
        else:
            meta, number = u[1], u[2]
            entry = grouped.setdefault(show_key(meta), {'meta': meta, 'seasons': {}})
            if u[0] == 'se':
                entry['seasons'][number] = {'number': number}
            else:
                season = entry['seasons'].setdefault(number, {'number': number, 'episodes': []})
                if 'episodes' in season:
                    season['episodes'].append(u[3])
    for entry in grouped.values():
        shows.append(dict(entry['meta'], seasons=list(entry['seasons'].values())))
    if movies:
        out['movies'] = movies
    if shows:
        out['shows'] = shows
    return out


def count_events(payload):
    return len(_units(payload))


def split(payload, max_events):
    units = _units(payload)
    max_events = max(1, int(max_events))
    return [_build(units[i:i + max_events]) for i in range(0, len(units), max_events)]
