"""Share the debug log: export to one small file, or upload to paste.kodi.tv. Both only on request,
and both pass the whole text through the redaction a second time."""
import os

import xbmc
import xbmcgui
import xbmcvfs

from . import auth, cfg, log, net, redact

PASTE_URL = 'https://paste.kodi.tv'
EXPORT_NAME = 'watch_tracker_debug_log.txt'
SETTINGS_SNAPSHOT = (
    ('mdblist_enabled', 'bool'), ('simkl_enabled', 'bool'), ('scrobble_movie', 'bool'), ('scrobble_episode', 'bool'),
    ('rate_movie', 'bool'), ('rate_episode', 'bool'),
    ('rate_min_percent', 'int'), ('rate_default', 'int'), ('simkl_min_progress', 'int'))


def build_report():
    """Header (versions + non-sensitive settings, never keys) + the in-memory log, redacted."""
    header = ['Watch Tracker debug log',
              'add-on version: {}'.format(cfg.version()),
              'Kodi: {}'.format(xbmc.getInfoLabel('System.BuildVersion')),
              'MDBList logged in: {}'.format(auth.get('mdblist').is_logged_in()),
              'Simkl logged in: {}'.format(auth.get('simkl').is_logged_in())]
    for key, kind in SETTINGS_SNAPSHOT:
        header.append('{}: {}'.format(key, cfg.get_bool(key) if kind == 'bool' else cfg.get_int(key)))
    return redact.text('\n'.join(header) + '\n\n' + log.read_buffer())


def _has_log():
    if log.read_buffer():
        return True
    xbmcgui.Dialog().ok('Watch Tracker', 'The debug log is empty.\n\nTurn on "Verbose logging", play something, '
                                         'then try again. (The log lives in memory only.)')
    return False


def export_log():
    if not _has_log():
        return
    target = 'special://temp/' + EXPORT_NAME
    path = os.path.join(xbmcvfs.translatePath('special://temp/'), EXPORT_NAME)
    try:
        with open(path, 'w', encoding='utf-8') as f:
            f.write(build_report())
    except OSError as e:
        xbmcgui.Dialog().ok('Watch Tracker', 'Could not write the file: {}'.format(e))
        return
    xbmcgui.Dialog().ok('Watch Tracker', 'Debug log saved (sensitive data removed):\n{}\n\n'
                                         'The file is overwritten on every export.'.format(target))


def upload_log():
    if not _has_log():
        return
    report = build_report()
    lines = report.count('\n') + 1
    if not xbmcgui.Dialog().yesno(
            'Upload debug log',
            'Upload the debug log ({} lines, tokens, servers and addresses removed) to paste.kodi.tv?\n\n'
            'Anyone with the link can read it.'.format(lines)):
        return
    res = net.request('POST', PASTE_URL + '/documents', data=report.encode('utf-8'),
                      headers={'User-Agent': 'service.watch.tracker/{}'.format(cfg.version()),
                               'Content-Type': 'text/plain; charset=utf-8'}, timeout=20)
    key = res.body.get('key') if res.ok else None
    if key:
        xbmcgui.Dialog().ok('Debug log uploaded',
                            'Upload key:  [B]{}[/B]\nLink:  {}/{}\n\nSend the key or link when asking for help.'.format(
                                key, PASTE_URL, key))
        return
    log.warning('log upload failed: {}'.format(net.summarize(res)))
    if xbmcgui.Dialog().yesno('Upload failed', 'Could not upload the log. Save it to a file instead?'):
        export_log()
