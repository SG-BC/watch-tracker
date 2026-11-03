"""Entry point for the settings buttons: RunScript(service.watch.tracker, <action>[, <service>])."""
import sys

import xbmcgui

from resources.lib import cache, cfg, log, logshare, ui


def view_log():
    text = log.read_buffer()
    if not text:
        text = ('The debug log is empty.\n\nTurn on "Verbose logging" in the settings, play something, then open '
                'this again. The log is kept in memory only (last 250 lines, sensitive data removed) and is lost '
                'when Kodi restarts.')
    xbmcgui.Dialog().textviewer('Watch Tracker - debug log', text)


def _run_sync(dry, confirm=True):
    from resources.lib import pairs, sync
    src, dst = cfg.get_str('sync_from', 'mdblist'), cfg.get_str('sync_to', 'simkl')
    reason = pairs.check(src, dst)
    if reason:
        xbmcgui.Dialog().ok('Sync', reason)
        return False
    title = pairs.title(src, dst, dry)
    if not dry and confirm and not xbmcgui.Dialog().yesno(
            title, 'Copy watch history (with dates) and ratings from {} to {}?\n\nThis only adds; nothing is '
                   'deleted or overwritten. It can be cancelled and resumed.'.format(pairs.LABELS[src], pairs.LABELS[dst])):
        return False
    dialog = xbmcgui.DialogProgress()
    dialog.create(title, 'Starting...')

    class Ui:
        def update(self, percent, text):
            dialog.update(percent, text)

        def cancelled(self):
            return dialog.iscanceled()

    try:
        text = pairs.run(src, dst, Ui(), dry)
    except sync.Cancelled:
        text = 'Cancelled.' + ('' if dry else ' Progress is saved; run it again to continue.')
    except sync.SyncError as e:
        text = str(e)
    except Exception as e:                      # never leave the dialog hanging
        log.error('sync failed: {!r}'.format(e))
        text = 'Sync failed: {}'.format(e)
    finally:
        dialog.close()
    xbmcgui.Dialog().ok(title, text)
    return True


def sync_reset():
    """Re-sync everything: forget what was synced before, then run the chosen From/To pair from the start."""
    from resources.lib import pairs
    src, dst = cfg.get_str('sync_from', 'mdblist'), cfg.get_str('sync_to', 'simkl')
    reason = pairs.check(src, dst)
    if reason:
        xbmcgui.Dialog().ok('Re-sync everything', reason)
        return
    if xbmcgui.Dialog().yesno('Re-sync everything', 'Forget what was synced before and copy everything from {} to {} '
                              'again?\n\nThis only adds; nothing is deleted or overwritten. It can take a while.'
                              .format(pairs.LABELS[src], pairs.LABELS[dst])):
        pairs.reset_all()
        _run_sync(False, confirm=False)


def main():
    action = sys.argv[1] if len(sys.argv) > 1 else 'view_log'
    service = sys.argv[2] if len(sys.argv) > 2 else ''
    if action == 'view_log':
        view_log()
    elif action == 'clear_log':
        log.clear_buffer()
        ui.notify('Debug log cleared')
    elif action == 'export_log':
        logshare.export_log()
    elif action == 'upload_log':
        logshare.upload_log()
    elif action == 'clear_cache':
        cache.clear()
        ui.notify('Search cache cleared')
    elif action == 'login':
        from resources.lib import login
        login.login(service)
    elif action == 'logout':
        from resources.lib import login
        login.logout(service)
    elif action == 'sync_run':
        _run_sync(False)
    elif action == 'sync_preview':
        _run_sync(True)
    elif action == 'ignore_folders':
        from resources.lib import ignore_ui
        ignore_ui.manage_folders()
    elif action == 'ignore_addons':
        from resources.lib import ignore_ui
        ignore_ui.manage_addons()
    elif action == 'sync_reset':
        sync_reset()
    elif action == 'edit_recent':
        from resources.lib import manual
        manual.edit_recent()
    elif action == 'watchlist_refresh':
        from resources.lib import watchlist
        ui.notify('Refreshing the watchlist...')
        count = watchlist.refresh(force=True)
        ui.notify('Watchlist updated ({} source{})'.format(count, '' if count == 1 else 's'))


if __name__ == '__main__':
    main()
