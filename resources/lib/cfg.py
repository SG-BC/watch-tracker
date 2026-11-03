"""Settings + constants. Read live on every call, so changes apply without a restart."""
import os

import xbmcaddon
import xbmcvfs

ADDON_ID = 'service.watch.tracker'
APP_NAME = 'Kodi'            # shown by MDBList as "Scrobbling from Kodi"
SIMKL_APP_NAME = 'watch-tracker'


def _addon():
    return xbmcaddon.Addon(ADDON_ID)


def get_bool(key, default=False):
    try:
        return _addon().getSettingBool(key)
    except Exception:
        return default


def get_int(key, default=0):
    try:
        return _addon().getSettingInt(key)
    except Exception:
        return default


def get_str(key, default=''):
    try:
        return (_addon().getSettingString(key) or default).strip()
    except Exception:
        return default


def version():
    try:
        return _addon().getAddonInfo('version')
    except Exception:
        return ''


def addon_path():
    return _addon().getAddonInfo('path')


def profile_dir():
    path = xbmcvfs.translatePath(_addon().getAddonInfo('profile'))
    os.makedirs(path, exist_ok=True)
    return path
