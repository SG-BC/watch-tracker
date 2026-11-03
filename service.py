import threading

import xbmc

from resources.lib import log, status
from resources.lib.player import PlayerMonitor

TICK_SECONDS = 3         # local position tracking only; never touches the network
CHECK_DELAY = 3.0        # let a burst of settings changes finish before checking the connections


class SettingsMonitor(xbmc.Monitor):
    def __init__(self):
        super().__init__()
        self._timer = None

    def onSettingsChanged(self):
        if status.quiet():            # that was our own status write
            return
        if self._timer:
            self._timer.cancel()
        self._timer = threading.Timer(CHECK_DELAY, status.check_all)
        self._timer.daemon = True
        self._timer.start()


def main():
    monitor = SettingsMonitor()
    player = PlayerMonitor()
    player.recover_pending()      # re-send a stop that a crash/outage left behind (rare)
    startup_check = threading.Timer(5.0, status.check_all)
    startup_check.daemon = True
    startup_check.start()
    log.debug('service started')

    while not monitor.waitForAbort(TICK_SECONDS):
        player.tick()

    player.shutdown()


if __name__ == '__main__':
    main()
