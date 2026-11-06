"""The last few things played, for the manual editor (recent.json in the profile folder).

Four lists (movies, shows, seasons, episodes), most recent first, MAX_PER_LIST each. Finished and unfinished plays
both count. An episode play also refreshes its show and its season. Only small entries: title, year, ids, numbers,
and `native`: the services' own ids of the movie / show when their answers carried one (e.g. {'simkl': 2142967})."""
import json
import os

from . import cfg, log

MAX_PER_LIST = 5
KINDS = ('movie', 'show', 'season', 'episode')


def _file():
    return os.path.join(cfg.profile_dir(), 'recent.json')


def load():
    try:
        with open(_file(), 'r', encoding='utf-8') as f:
            data = json.load(f)
    except (OSError, ValueError):
        data = {}
    return {k: [e for e in (data.get(k) or []) if isinstance(e, dict)] for k in KINDS}


def save(data):
    try:
        with open(_file(), 'w', encoding='utf-8') as f:
            json.dump({k: data.get(k, [])[:MAX_PER_LIST] for k in KINDS}, f)
    except OSError as e:
        log.warning('could not save recent.json: {}'.format(e))


def _same(a, b):
    if a['kind'] != b['kind'] or a.get('season') != b.get('season') or a.get('episode') != b.get('episode'):
        return False
    ids_a, ids_b = a.get('ids') or {}, b.get('ids') or {}
    if any(str(v) == str(ids_b.get(k)) for k, v in ids_a.items()):
        return True
    return bool(a.get('title') and a.get('title') == b.get('title') and a.get('year') == b.get('year'))


def entries_for_ref(ref, ts, progress):
    """A played ref -> the entries it adds: movie; or episode + its season + its show."""
    base = {'title': ref.get('title'), 'year': ref.get('year'), 'ids': ref.get('ids') or {}, 'ts': ts,
            'progress': round(progress or 0)}
    native = {k: v for k, v in (ref.get('native') or {}).items() if v}
    if native:
        base['native'] = native
    if ref['kind'] == 'movie':
        return [dict(base, kind='movie')]
    return [dict(base, kind='show'), dict(base, kind='season', season=ref['season']),
            dict(base, kind='episode', season=ref['season'], episode=ref['episode'])]


def add(entries):
    data = load()
    for entry in entries:
        rows = [e for e in data[entry['kind']] if not _same(e, entry)]
        data[entry['kind']] = [entry] + rows
    save(data)


def record(ref, ts, progress):
    """Called after a play. Never raises."""
    try:
        if ref and (ref.get('ids') or (ref.get('title') and ref.get('year'))):
            add(entries_for_ref(ref, ts, progress))
    except Exception as e:
        log.warning('could not record the recent item: {!r}'.format(e))
