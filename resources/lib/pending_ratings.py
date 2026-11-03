"""Ratings given on a service for something not yet watched there ("rate first, watch later").

When "Date ratings with the watch date" is on, the rating is sent again with the watch date as soon as the
watch is recorded. The list is kept locally (no extra reads on the services); entries leave it when
they are re-dated. Only a few small entries: id, title, year, rating."""
import json
import os

from . import cfg, log, parsing

MAX_ENTRIES = 2000


def _file():
    return os.path.join(cfg.profile_dir(), 'pending_ratings.json')


def _load():
    try:
        with open(_file(), 'r', encoding='utf-8') as f:
            data = json.load(f)
            return data if isinstance(data, list) else []
    except (OSError, ValueError):
        return []


def _save(entries):
    try:
        with open(_file(), 'w', encoding='utf-8') as f:
            json.dump(entries[-MAX_ENTRIES:], f)
    except OSError as e:
        log.warning('could not save pending ratings: {}'.format(e))


def _same(entry, kind, ids, title, year):
    if entry['kind'] != kind:
        return False
    eids = entry.get('ids') or {}
    if any(str(v) == str((ids or {}).get(k)) for k, v in eids.items()):
        return True
    return bool(title and year and entry.get('title') and entry.get('year')
                and parsing.normalize(entry['title']) == parsing.normalize(title) and entry['year'] == year)


def add(service, kind, item, rating):
    """item: {'title','year','ids'}; kind: movie | show. A newer rating replaces the older entry."""
    entries = [e for e in _load()
               if not (e['service'] == service and _same(e, kind, item.get('ids'), item.get('title'), item.get('year')))]
    entries.append({'service': service, 'kind': kind, 'ids': item.get('ids') or {}, 'title': item.get('title'),
                    'year': item.get('year'), 'rating': rating})
    _save(entries)


def add_from_payloads(service, shows_filed, ratings):
    """Sync helper: `shows_filed` = {'shows': [...]} filed without a watch, `ratings` = {'shows': [{..., rating}]}."""
    rated = {}
    for row in (ratings.get('shows') or []):
        rated[json.dumps(row.get('ids') or {}, sort_keys=True)] = row.get('rating')
    count = 0
    for item in (shows_filed.get('shows') or []):
        value = rated.get(json.dumps(item.get('ids') or {}, sort_keys=True))
        if value:
            add(service, 'show', item, value)
            count += 1
    return count


def find(service, ref):
    """The pending rating that belongs to the item being watched, or None."""
    kind = 'movie' if ref['kind'] == 'movie' else 'show'
    for entry in _load():
        if entry['service'] == service and _same(entry, kind, ref.get('ids'), ref.get('title'), ref.get('year')):
            return entry
    return None


def remove(entry):
    _save([e for e in _load() if e != entry])


def target(entry):
    """A rate()-ready ref for the entry."""
    return {'kind': entry['kind'], 'ids': entry.get('ids') or {}, 'title': entry.get('title'), 'year': entry.get('year')}
