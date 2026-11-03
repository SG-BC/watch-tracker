"""Tiny on-disk cache for MDBList title searches. One write per never-seen title, capped in size.
Matches expire after 90 days (a wrong match can't stick forever); misses after 7 days."""
import json
import os
import time

from . import cfg, log

MAX_ENTRIES = 1500
HIT_TTL = 90 * 86400
MISS_TTL = 7 * 86400

_data = None


def _path():
    return os.path.join(cfg.profile_dir(), 'search_cache.json')


def _load():
    global _data
    if _data is None:
        try:
            with open(_path(), 'r', encoding='utf-8') as f:
                _data = json.load(f)
        except Exception:
            _data = {}
    return _data


def get(key):
    """-> dict of ids (hit), {} (recent miss), or None (unknown / expired -> search again)."""
    entry = _load().get(key)
    if not entry:
        return None
    age = time.time() - entry.get('t', 0)
    if entry.get('ids'):
        return dict(entry['ids']) if age < HIT_TTL else None
    return {} if age < MISS_TTL else None


def put(key, ids):
    data = _load()
    data[key] = {'ids': ids, 't': time.time()}
    if len(data) > MAX_ENTRIES:
        for k, _ in sorted(data.items(), key=lambda kv: kv[1].get('t', 0))[:len(data) - MAX_ENTRIES]:
            del data[k]
    _save()


def clear():
    global _data
    _data = {}
    try:
        os.remove(_path())
    except OSError:
        pass


def _save():
    try:
        with open(_path(), 'w', encoding='utf-8') as f:
            json.dump(_load(), f)
    except OSError as e:
        log.warning('could not save search cache: {}'.format(e))
