"""What not to track: folders and video add-ons the user chose, plus where the choices come from.

* Folders: from Kodi's own video sources (Files.GetSources: the same data as sources.xml, without
  parsing credentials; sources.xml is the fallback), from Kodi's folder browser, or typed.
  An entry that looks like a path (scheme://, /, C:/) matches that folder and everything below it;
  a plain name ("Family Videos") matches any folder with that name anywhere in the path.
* Video add-ons: every installed video add-on (Addons.GetAddons), the user ticks the ones to ignore.
  Matched by the add-on's plugin:// address or the container's plugin name.
Choices live in one tiny file (ignored.json) that is written only when the user changes them.
"""
import json
import os
import re
from urllib.parse import unquote

import xbmc
import xbmcvfs

from . import cfg, log

_ABSOLUTE = re.compile(r'^([a-z][a-z0-9+.\-]*://|/|[a-z]:/)', re.I)


def _file():
    return os.path.join(cfg.profile_dir(), 'ignored.json')


def load():
    try:
        with open(_file(), 'r', encoding='utf-8') as f:
            data = json.load(f)
    except (OSError, ValueError):
        data = {}
    return {'paths': list(data.get('paths') or []), 'addons': list(data.get('addons') or [])}


def save(data):
    try:
        with open(_file(), 'w', encoding='utf-8') as f:
            json.dump({'paths': data['paths'], 'addons': data['addons']}, f)
    except OSError as e:
        log.warning('could not save ignored.json: {}'.format(e))


def strip_credentials(path):
    return re.sub(r'^([a-z][a-z0-9+.\-]*://)[^/@]*@', r'\1', str(path or ''), flags=re.I)


def norm(path):
    return strip_credentials(unquote(str(path or '')).strip().replace('\\', '/')).lower().rstrip('/')


def path_ignored(paths, entries=None):
    """The ignored entry that matches one of these file paths, or None."""
    entries = load()['paths'] if entries is None else entries
    for raw in paths:
        p = norm(raw)
        if not p:
            continue
        for entry in entries:
            n = norm(entry)
            if not n:
                continue
            if _ABSOLUTE.match(n):
                if p == n or p.startswith(n + '/'):
                    return entry
            elif '/' + n + '/' in '/' + p + '/':
                return entry
    return None


def addon_ignored(paths, plugin_name='', ids=None):
    """The ignored add-on id this playback came from, or None."""
    ids = load()['addons'] if ids is None else ids
    for addon_id in ids:
        if plugin_name and plugin_name == addon_id:
            return addon_id
        marker = 'plugin://' + addon_id.lower() + '/'
        if any(marker in unquote(str(p or '')).lower() + '/' for p in paths):
            return addon_id
    return None


# ------------------------------------------------------------------ where the choices come from
def _rpc(method, params):
    try:
        return json.loads(xbmc.executeJSONRPC(json.dumps(
            {'jsonrpc': '2.0', 'id': 1, 'method': method, 'params': params}))).get('result') or {}
    except ValueError:
        return {}


def expand_multipath(path):
    if str(path).startswith('multipath://'):
        return [unquote(p) for p in str(path)[len('multipath://'):].split('/') if p]
    return [path]


def media_sources():
    """[(label, path)] of Kodi's video sources."""
    out = []
    for src in _rpc('Files.GetSources', {'media': 'video'}).get('sources', []):
        for path in expand_multipath(src.get('file')):
            out.append((src.get('label') or 'Source', path))
    if not out:                                     # fallback: read sources.xml directly
        try:
            import xml.etree.ElementTree as ET
            root = ET.parse(xbmcvfs.translatePath('special://profile/sources.xml')).getroot()
            for source in root.findall('./video/source'):
                for p in source.findall('path'):
                    for path in expand_multipath(p.text or ''):
                        out.append((source.findtext('name') or 'Source', path))
        except Exception as e:
            log.debug('could not read sources.xml: {!r}'.format(e))
    seen, unique = set(), []
    for label, path in out:
        if path and path not in seen:
            seen.add(path)
            unique.append((label, path))
    return unique


def video_addons():
    """[(addon id, name)] of the installed, enabled video add-ons."""
    res = _rpc('Addons.GetAddons', {'type': 'xbmc.addon.video', 'content': 'video', 'enabled': True,
                                    'properties': ['name']})
    return sorted(((a.get('addonid'), a.get('name') or a.get('addonid')) for a in res.get('addons', [])
                   if a.get('addonid')), key=lambda a: a[1].lower())
