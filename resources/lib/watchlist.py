"""The watchlist behind the home-screen widget ("Your Next Movies").

The service refreshes it from time to time (never the widget): each source keeps its own list in watchlist.json,
and the widget merges the sources the user switched on, so toggling a source needs no network call. Items that
are in the Kodi library link to it; the others show their art and cannot be played."""
import json
import os
import time

from . import cfg, kodimap, kodisync, log, parsing, providers, syncmap

SOURCES = ('simkl', 'mdblist', 'wetrakr')
REFRESH_SECONDS = 6 * 3600


def _file():
    return os.path.join(cfg.profile_dir(), 'watchlist.json')


def load():
    try:
        with open(_file(), 'r', encoding='utf-8') as f:
            data = json.load(f)
    except (OSError, ValueError):
        data = {}
    return {'updated': data.get('updated', 0), 'stamps': data.get('stamps') or {}, 'lists': data.get('lists') or {}}


def save(data):
    try:
        with open(_file(), 'w', encoding='utf-8') as f:
            json.dump(data, f)
    except OSError as e:
        log.warning('could not save watchlist.json: {}'.format(e))


def source_on(name):
    return cfg.get_bool('watchlist_' + name, True)


def due(now=None):
    return (now or time.time()) - load()['updated'] >= REFRESH_SECONDS


def refresh(force=False):
    """Re-read every switched-on, logged-in source. Returns the number of sources read. Never raises."""
    data = load()
    done = 0
    for name in SOURCES:
        provider = providers.get(name)
        if not source_on(name) or provider is None or not provider.logged_in():
            continue
        try:
            stamp_fn = getattr(provider, 'watchlist_stamp', None)
            stamp = stamp_fn() if stamp_fn else None
            if not force and stamp and stamp == data['stamps'].get(name) and name in data['lists']:
                done += 1
                continue                                    # Simkl says nothing changed
            items = provider.watchlist()
        except Exception as e:
            log.warning('watchlist {} failed: {!r}'.format(name, e))
            continue
        if items is None:
            continue                                        # keep the old copy
        data['lists'][name] = items
        if stamp:
            data['stamps'][name] = stamp
        done += 1
    data['updated'] = time.time()
    save(data)
    log.debug('watchlist refreshed: {}'.format({k: len(v) for k, v in data['lists'].items()}))
    return done


def _key(item):
    ids = item.get('ids') or {}
    for k in ('imdb', 'tmdb', 'tvdb'):
        if ids.get(k):
            return (item['kind'], k, str(ids[k]))
    return (item['kind'], 't', parsing.normalize(item.get('title') or ''), item.get('year'))


def merged(kind=None):
    """The switched-on sources, merged and de-duplicated: [{'kind','title','year','ids','poster','sources':[..]}]."""
    data = load()
    by_ids, out = {}, []
    for name in SOURCES:
        if not source_on(name):
            continue
        for item in data['lists'].get(name) or []:
            if kind and item.get('kind') != kind:
                continue
            aliases = [(item['kind'], k, str(v)) for k, v in (item.get('ids') or {}).items() if k in ('imdb', 'tmdb', 'tvdb') and v]
            aliases.append((item['kind'], 't', parsing.normalize(item.get('title') or ''), item.get('year')))
            hit = next((by_ids[a] for a in aliases if a in by_ids), None)
            if hit is None:
                hit = dict(item, sources=[], ids=dict(item.get('ids') or {}))
                out.append(hit)
            else:
                hit['ids'].update({k: v for k, v in (item.get('ids') or {}).items() if k not in hit['ids']})
                hit['poster'] = hit.get('poster') or item.get('poster')
            if name not in hit['sources']:
                hit['sources'].append(name)
            for a in aliases:
                by_ids.setdefault(a, hit)
    return out


def with_library(items):
    """-> [(item, kodi row or None)]: the library entry of each item (movies and shows). Library entries come first;
    movies already watched in Kodi are left out."""
    movies = kodisync._library('VideoLibrary.GetMovies', 'movies',
                               ['title', 'year', 'playcount', 'uniqueid', 'imdbnumber', 'file', 'art', 'plot'])
    shows = kodisync._library('VideoLibrary.GetTVShows', 'tvshows', ['title', 'year', 'uniqueid', 'imdbnumber', 'art', 'plot'])
    index = kodimap.KodiIndex(movies, shows, [])
    inside, outside = [], []
    for item in items:
        neutral = syncmap.media({'title': item.get('title'), 'year': item.get('year'), 'ids': item.get('ids') or {}}, item['kind'])
        row = (index.movie(neutral) if item['kind'] == 'movie' else index.show(neutral)) if neutral else None
        if row is not None:
            if item['kind'] == 'movie' and (row.get('playcount') or 0) > 0:
                continue
            inside.append((item, row))
        else:
            outside.append((item, None))
    return inside + outside
