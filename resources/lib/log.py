"""Logging that does not wear the disk and never leaks secrets.

* Debug lines live ONLY in memory (last MAX_LINES in a Kodi window property); nothing is written
  for them to disk. Warnings/errors (rare, short) also go to kodi.log.
* EVERY line goes through redact.text() first: tokens, hosts, IPs, credentials are removed.
"""
import threading
import time

import xbmc
import xbmcgui

from . import cfg, redact

PROP = 'service.watch.tracker.log'
MAX_LINES = 250
MAX_LINE_LEN = 600

_lock = threading.Lock()


def _push(level, msg):
    """Append to the shared buffer (a Kodi window property). Re-reading it first keeps lines written by
    the service and by settings-button scripts (separate processes) in one log."""
    line = '{} {} {}'.format(time.strftime('%H:%M:%S'), level, redact.text(str(msg).replace('\n', ' '))[:MAX_LINE_LEN])
    with _lock:
        try:
            window = xbmcgui.Window(10000)
            existing = window.getProperty(PROP)
            lines = existing.split('\n') if existing else []
            lines.append(line)
            window.setProperty(PROP, '\n'.join(lines[-MAX_LINES:]))
        except Exception:
            pass


def debug(msg):
    if cfg.get_bool('debug'):
        _push('D', msg)


def warning(msg):
    xbmc.log('Watch Tracker: {}'.format(redact.text(msg)), xbmc.LOGWARNING)
    if cfg.get_bool('debug'):
        _push('W', msg)


def error(msg):
    xbmc.log('Watch Tracker: {}'.format(redact.text(msg)), xbmc.LOGERROR)
    if cfg.get_bool('debug'):
        _push('E', msg)


def read_buffer():
    return xbmcgui.Window(10000).getProperty(PROP) or ''


def clear_buffer():
    with _lock:
        xbmcgui.Window(10000).clearProperty(PROP)
