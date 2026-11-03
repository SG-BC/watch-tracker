"""Settings-button flows: login with a PIN code shown on screen (works with a remote), and logout."""
import time

import xbmc
import xbmcaddon
import xbmcgui

from . import auth, cfg, log, net, providers, status, ui

POLL_LIMIT_SECONDS = 600     # give up earlier than the code lifetime


def _set_enabled(name, value):
    try:
        xbmcaddon.Addon(cfg.ADDON_ID).setSettingBool(name + '_enabled', value)
    except Exception:
        pass


def fetch_username(name):
    """Ask the service who is signed in and remember it for the status line. Returns the name or ''."""
    provider = providers.get(name)
    if provider is None:
        return ''
    try:
        username = provider.fetch_username()
    except Exception as e:
        log.debug('{} username lookup failed: {!r}'.format(name, e))
        return ''
    if username:
        auth.get(name).set_username(username)
    return username or ''


def login(name):
    label = providers.get(name).label
    account = auth.get(name)
    try:
        session = account.start_login()
    except auth.LoginError as e:
        log.warning(str(e))
        xbmcgui.Dialog().ok(label + ' login', 'Could not start the login.\n\n{}'.format(e))
        return
    total = min(session['expires_in'], POLL_LIMIT_SECONDS)
    deadline = time.time() + total
    interval = max(5, session['interval'])
    message = ('On your phone or computer open:\n[B]{}[/B]\nand enter the code:\n[B]{}[/B]\n\n'
               'Waiting for approval...').format(session['verification_uri'], session['user_code'])
    dialog = xbmcgui.DialogProgress()
    dialog.create(label + ' login', message)
    monitor = xbmc.Monitor()
    result = 'pending'
    try:
        while time.time() < deadline:
            if monitor.waitForAbort(interval) or dialog.iscanceled():
                result = 'cancelled'
                break
            result = account.poll_login(session)
            if result == 'slow_down':
                interval += 5
            elif result in ('ok', 'ok_readonly', 'expired', 'denied', 'error'):
                break
            dialog.update(int(100 * (1 - (deadline - time.time()) / total)), message)
        else:
            result = 'timeout'
    finally:
        dialog.close()
    if result in ('ok', 'ok_readonly'):
        _set_enabled(name, True)
        fetch_username(name)
        status.set_status(name, True)
        if result == 'ok_readonly':
            xbmcgui.Dialog().ok(label + ' login', 'Logged in, but {} granted read-only access, so tracking cannot '
                                                  'write. Please log in again.'.format(label))
        else:
            ui.notify('{}: logged in'.format(label))
    elif result == 'cancelled':
        ui.notify('{} login cancelled'.format(label))
    elif result == 'denied':
        xbmcgui.Dialog().ok(label + ' login', 'Access was denied on the {} website.'.format(label))
    else:
        xbmcgui.Dialog().ok(label + ' login', 'The login did not complete ({}). Please try again.'.format(result))


def logout(name):
    label = providers.get(name).label
    account = auth.get(name)
    if not account.is_logged_in():
        ui.notify('{}: not logged in'.format(label))
        return
    if xbmcgui.Dialog().yesno(label, 'Log out of {} on this device?'.format(label)):
        account.logout()
        _set_enabled(name, False)
        status.set_status(name, False)
        ui.notify('{}: logged out'.format(label))
