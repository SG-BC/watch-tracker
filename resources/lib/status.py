"""Connection status shown in the settings (Accounts tab): "Connected as @username" / "Disconnected".

Kept in three read-only string settings. It reflects what really happened: the last call of a service
succeeded, or was rejected / could not connect. A check runs once at start and after settings change
(a cheap request, or a local token check). The setting is written only
when the value changes, so it costs no disk wear."""
import threading
import time

import xbmcaddon

from . import auth, cfg, log

CONNECTED, DISCONNECTED = 'Connected', 'Disconnected'

_last = {}
_quiet_until = [0.0]
_lock = threading.Lock()


def text_for(name, connected):
    if not connected:
        return DISCONNECTED
    try:
        user = auth.get(name).username()
    except Exception:
        user = ''
    return '{} as @{}'.format(CONNECTED, user) if user else CONNECTED


def set_status(name, connected):
    text = text_for(name, connected)
    with _lock:
        if _last.get(name) == text:
            return
        _last[name] = text
        _quiet_until[0] = time.time() + 5       # our own write triggers onSettingsChanged: ignore it
    try:
        xbmcaddon.Addon(cfg.ADDON_ID).setSettingString(name + '_status', text)
    except Exception as e:
        log.debug('could not store the {} status: {!r}'.format(name, e))


def quiet():
    """True right after we wrote a status ourselves (so the settings-changed event can be ignored)."""
    return time.time() < _quiet_until[0]


def report(name, res):
    """Called by every provider request with its result."""
    if res.ok:
        set_status(name, True)
    elif res.auth_error or res.network_error:
        set_status(name, False)


def check_all():
    """One lightweight check per configured service."""
    from . import providers
    for provider in providers.ALL:
        try:
            provider.check()
        except Exception as e:
            log.debug('status check of {} failed: {!r}'.format(provider.label, e))
    try:
        from . import plexapi                       # Plex is not a provider (it is never scrobbled to)
        plexapi.check()
    except Exception as e:
        log.debug('status check of Plex failed: {!r}'.format(e))
