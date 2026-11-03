"""Rating prompt: Kodi's own list dialog (uses the active skin's fonts and colours, so it is readable
everywhere and works with a remote), then the rating goes to every service that supports it."""
import xbmcgui

from . import log, ui

LABELS = {10: 'Masterpiece', 9: 'Excellent', 8: 'Great', 7: 'Good', 6: 'Fine',
          5: 'Average', 4: 'Mediocre', 3: 'Poor', 2: 'Bad', 1: 'Awful'}
SKIP = "Don't rate"


def display_name(info, ref):
    if ref['kind'] == 'movie':
        title = info.get('title') or (info.get('_parsed') or {}).get('title') or 'Movie'
        year = info.get('year')
        return '{} ({})'.format(title, year) if year else title
    show = info.get('showtitle') or (info.get('_parsed') or {}).get('show_title') or 'Episode'
    return '{} S{:02d}E{:02d}'.format(show, ref['season'], ref['episode'])


def rows():
    return [SKIP] + ['{} - {}'.format(n, LABELS[n]) for n in range(10, 0, -1)]


def preselect_index(default):
    """default 1..10 highlights that rating; 0 (or anything else) highlights "Don't rate"."""
    return 11 - default if isinstance(default, int) and 1 <= default <= 10 else 0


def prompt(name, default=6):
    """-> rating 1..10, or None when skipped (Don't rate row, or Back)."""
    try:
        index = xbmcgui.Dialog().select('Rate: ' + name, rows(), preselect=preselect_index(default))
    except Exception as e:
        log.error('rating list failed: {!r}'.format(e))
        return None
    if index is None or index <= 0:
        return None
    return 11 - index


def submit(ref, value, name, targets, when=None):
    """Send the rating to every service that supports it; one summary notification.
    `when` (epoch seconds) dates it with the watch instead of the moment the list was answered."""
    done, failed = [], []
    for provider in targets:
        try:
            res = provider.rate(ref, value, when=when)
        except Exception as e:
            log.error('rating via {} failed: {!r}'.format(provider.label, e))
            failed.append(provider.label)
            continue
        (done if res.ok else failed).append(provider.label)
    if done:
        ui.notify('Rated {}: {}/10 ({})'.format(name, value, ', '.join(done)))
    if failed:
        ui.notify('Could not save the rating on {}'.format(', '.join(failed)), error=True)
    return bool(done)
