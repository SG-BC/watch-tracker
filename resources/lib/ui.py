import xbmcgui

_once = set()


def notify(message, error=False, ms=4000):
    try:
        xbmcgui.Dialog().notification('Watch Tracker', message,
                                      xbmcgui.NOTIFICATION_ERROR if error else xbmcgui.NOTIFICATION_INFO, ms)
    except Exception:
        pass


def notify_once(key, message, error=True):
    if key in _once:
        return
    _once.add(key)
    notify(message, error=error, ms=5000)
