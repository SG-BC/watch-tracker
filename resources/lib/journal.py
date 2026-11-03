"""Crash safeguard (small form), per service.

Armed ONCE per playback when progress first reaches the earliest watched mark of an enabled
service. Lists which services still owe a stop. Each success removes its service; the file is
deleted when none remain. Items stopped below the mark never write anything. If Kodi dies or
the network is down, what is left is re-sent on the next Kodi start.
"""
import json
import os
import threading
import time

from . import cfg, log


# Several service threads finish at the same moment; every read-modify-write below holds this lock.
_lock = threading.RLock()


def _path():
    return os.path.join(cfg.profile_dir(), 'pending_stop.json')


def load():
    try:
        with open(_path(), 'r', encoding='utf-8') as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _write(entry):
    try:
        with open(_path(), 'w', encoding='utf-8') as f:
            json.dump(entry, f)
    except OSError as e:
        log.warning('could not write pending stop: {}'.format(e))


def arm(session_id, ref, progress, providers, position=None, duration=None):
    with _lock:
        _arm(session_id, ref, progress, providers, position, duration)


def _arm(session_id, ref, progress, providers, position, duration):
    _write({'session': session_id, 'ts': time.time(), 'ref': ref, 'progress': progress,
            'position': position, 'duration': duration, 'pending': list(providers)})


def done(session_id, provider):
    """This service no longer owes a stop for that session."""
    with _lock:
        entry = load()
        if not entry or entry.get('session') != session_id:
            return
        entry['pending'] = [p for p in entry.get('pending', []) if p != provider]
        if entry['pending']:
            _write(entry)
        else:
            drop()


def done_recovered(provider):
    entry = load()
    if entry:
        done(entry.get('session'), provider)


def drop():
    with _lock:
        try:
            os.remove(_path())
        except OSError:
            pass
