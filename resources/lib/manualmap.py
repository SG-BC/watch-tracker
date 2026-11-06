"""Manual edits of recent items: labels and the payloads they send. Pure Python (no Kodi imports), unit-tested.

A recent entry is {'kind': movie | show | season | episode, 'title', 'year', 'ids', 'season', 'episode', 'ts',
'progress'}; `year` is always the movie's / the show's own year. Payloads are Simkl-shaped (the neutral shape every
provider's add_history / add_ratings / remove_* already takes)."""
import time

try:
    from . import kodimap, syncmap
except ImportError:          # imported as a top-level module by the unit tests
    import kodimap, syncmap

KINDS = ('movie', 'show', 'season', 'episode')
TITLES = {'movie': 'Movies', 'show': 'TV Shows', 'season': 'Seasons', 'episode': 'Episodes'}


def label(entry):
    base = entry.get('title') or 'Unknown'
    if entry.get('year'):
        base = '{} ({})'.format(base, entry['year'])
    if entry['kind'] == 'season':
        return '{} / Season {}'.format(base, entry['season'])
    if entry['kind'] == 'episode':
        return '{} / S{:02d}E{:02d}'.format(base, entry['season'], entry['episode'])
    return base


def iso(epoch):
    return time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(epoch))


def item(entry):
    """{'title','year','ids'} for the movie / the show, or None when it cannot be told apart."""
    kind = 'movie' if entry['kind'] == 'movie' else 'show'
    return syncmap.media({'title': entry.get('title'), 'year': entry.get('year'), 'ids': entry.get('ids') or {}}, kind)


def ref(entry):
    """A provider-ready ref (kind movie | show | episode) for the entry; None for a season."""
    if entry['kind'] == 'season':
        return None
    out = {'kind': entry['kind'], 'ids': entry.get('ids') or {}, 'title': entry.get('title'), 'year': entry.get('year')}
    if entry['kind'] == 'episode':
        out.update(season=entry['season'], episode=entry['episode'])
    return out


def _seasons(pairs, stamp, dated=True):
    grouped = {}
    for season, number in sorted(set(pairs)):
        row = {'number': number}
        if dated and stamp:
            row['watched_at'] = stamp
        grouped.setdefault(season, []).append(row)
    return [{'number': s, 'episodes': eps} for s, eps in sorted(grouped.items())]


def episode_pairs(entry, scope, library):
    """(season, episode) pairs an action on the entry covers. `library` = every (season, episode) known for the show
    (the Kodi library, or a service's episode list). scope: 'one' | 'up_to' (episodes only). Specials (season 0)
    never count for "up to". With no list at all, "up to" covers episodes 1..N of this season (see whole_seasons)."""
    kind = entry['kind']
    if kind == 'episode':
        here = (entry['season'], entry['episode'])
        if scope == 'up_to':
            found = [p for p in library if p[0] > 0 and p <= here]
            if found:
                return found
            return [(here[0], n) for n in range(1, here[1] + 1)] if here[0] > 0 else [here]
        return [here]
    if kind == 'season':
        return [p for p in library if p[0] == entry['season']]
    return [p for p in library if p[0] > 0] or list(library)          # a whole show


def whole_seasons(entry, scope, library):
    """Seasons sent whole (each service fills in their episodes from its own catalog): only for "up to" with no
    episode list at all -> seasons 1 .. this one - 1."""
    if entry['kind'] != 'episode' or scope != 'up_to' or library:
        return []
    return list(range(1, entry['season'])) if entry['season'] > 1 else []


def scope_text(entry, scope, library):
    """What a mark covers, for the confirmation: '5 episodes (S01E01-S02E03)' or
    'Season 1 (whole) + S02E01-E03 (no episode list found)'."""
    pairs = episode_pairs(entry, scope, library)
    whole = whole_seasons(entry, scope, library)
    if entry['kind'] == 'season' and not pairs:
        return 'all episodes of season {}'.format(entry['season'])
    if entry['kind'] == 'show' and not pairs:
        return 'every episode'
    if not pairs:
        return '0 episodes'
    first, last = pairs[0], pairs[-1]
    if whole:
        names = 'Season {} (whole)'.format(whole[0]) if len(whole) == 1 else 'Seasons {}-{} (whole)'.format(whole[0], whole[-1])
        return '{} + S{:02d}E{:02d}-E{:02d} (no episode list found)'.format(names, first[0], first[1], last[1])
    span = 'S{:02d}E{:02d}'.format(*first) if first == last else 'S{:02d}E{:02d}-S{:02d}E{:02d}'.format(first[0], first[1], last[0], last[1])
    return '{} episode{} ({})'.format(len(pairs), '' if len(pairs) == 1 else 's', span)


def history_payload(entry, stamp, scope, library, dated=True):
    """Payload that marks the entry watched at `stamp` (add_history) or, with dated=False, names what to remove
    (remove_history). None when the item cannot be identified."""
    base = item(entry)
    if base is None:
        return None
    mark = {'watched_at': stamp} if (dated and stamp) else {}
    if entry['kind'] == 'movie':
        return {'movies': [dict(base, **mark)]}
    pairs = episode_pairs(entry, scope, library)
    if pairs:
        whole = [dict({'number': s}, **mark) for s in whole_seasons(entry, scope, library)]
        return {'shows': [dict(base, seasons=whole + _seasons(pairs, stamp, dated))]}
    if entry['kind'] == 'season':
        return {'shows': [dict(base, seasons=[{'number': entry['season']}])]}
    return {'shows': [dict(base, **mark)]}                              # no library episodes: the whole show


def rating_payload(entry, value=None, stamp=None):
    """Payload for add_ratings (value given) or remove_ratings (value None). Movies and shows by item;
    an episode nests under its show. None for a season (seasons are not rated)."""
    base = item(entry)
    if base is None or entry['kind'] == 'season':
        return None
    extra = {}
    if value is not None:
        extra['rating'] = value
        if stamp:
            extra['rated_at'] = stamp
    if entry['kind'] == 'movie':
        return {'movies': [dict(base, **extra)]}
    if entry['kind'] == 'show':
        return {'shows': [dict(base, **extra)]}
    return {'shows': [dict(base, seasons=[{'number': entry['season'],
                                           'episodes': [dict({'number': entry['episode']}, **extra)]}])]}


# ------------------------------------------------------------------------------ Kodi library
def library_pairs(index, entry):
    """Every (season, episode) the Kodi library holds for the entry's show."""
    base = item(entry)
    show = index.show(base) if base else None
    return sorted(index.episodes(show['tvshowid'])) if show else []


def kodi_updates(entry, action, index, scope='one', when=None, rating=None):
    """JSON-RPC updates for the Kodi library. action: watch | unwatch | rate | unrate. `when` = UTC ISO stamp.
    Returns [(method, params)]; [] when the item is not in the library."""
    base = item(entry)
    if base is None:
        return []
    local = kodimap.utc_to_kodi(when) if when else None
    out = []
    if entry['kind'] == 'movie':
        record = index.movie(base)
        if record is None:
            return []
        upd = {'movieid': record['movieid']}
        if action == 'watch':
            upd.update(playcount=max(1, record.get('playcount') or 0), **({'lastplayed': local} if local else {}))
        elif action == 'unwatch':
            upd.update(playcount=0, userrating=0)
        elif action in ('rate', 'unrate'):
            upd['userrating'] = rating if action == 'rate' else 0
        return [('VideoLibrary.SetMovieDetails', upd)]
    record = index.show(base)
    if record is None:
        return []
    episodes = index.episodes(record['tvshowid'])
    if action in ('rate', 'unrate'):
        if entry['kind'] == 'show':
            return [('VideoLibrary.SetTVShowDetails', {'tvshowid': record['tvshowid'],
                                                       'userrating': rating if action == 'rate' else 0})]
        row = episodes.get((entry['season'], entry['episode'])) if entry['kind'] == 'episode' else None
        if row is None:
            return []
        return [('VideoLibrary.SetEpisodeDetails', {'episodeid': row['episodeid'],
                                                    'userrating': rating if action == 'rate' else 0})]
    pairs = episode_pairs(entry, scope, sorted(episodes))
    for pair in pairs:
        row = episodes.get(pair)
        if row is None:
            continue
        upd = {'episodeid': row['episodeid']}
        if action == 'watch':                       # a new watch, also for episodes already watched (one more play)
            upd.update(playcount=(row.get('playcount') or 0) + 1, **({'lastplayed': local} if local else {}))
        else:
            upd.update(playcount=0, userrating=0)
        out.append(('VideoLibrary.SetEpisodeDetails', upd))
    if action == 'unwatch' and entry['kind'] in ('show',):
        out.append(('VideoLibrary.SetTVShowDetails', {'tvshowid': record['tvshowid'], 'userrating': 0}))
    return [(m, p) for m, p in out]
