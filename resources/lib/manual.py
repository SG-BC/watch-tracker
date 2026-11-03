"""Edit recent items by hand: mark watched (now / a picked date and time), mark unwatched, rate, remove a rating.

Opened from the settings ("Edit recent items..."). Works on the last plays kept in recent.json and sends the change
to every logged-in, switched-on service and (by default) to the Kodi library too. No rating prompt after a manual
"mark as watched"."""
import time

import xbmcgui

from . import cfg, kodimap, kodisync, log, manualmap, providers, rating, recent, ui

WATCH, UNWATCH, RATE, UNRATE = 'Mark as Watched', 'Mark as Unwatched', 'Rate...', 'Remove rating'


def _targets(check=None):
    return [p for p in providers.ALL if p.enabled() and (check is None or check(p))]


def _send(targets, method, payload):
    """Call `method` on each service. -> (labels that took it, labels that did not)."""
    done, failed = [], []
    for p in targets:
        try:
            res = getattr(p, method)(payload)
        except Exception as e:
            log.error('{} {} failed: {!r}'.format(p.label, method, e))
            failed.append(p.label)
            continue
        (done if res.ok else failed).append(p.label)
    return done, failed


def _report(what, done, failed, kodi):
    where = done + (['Kodi'] if kodi else [])
    if where:
        ui.notify('{}: {}'.format(what, ', '.join(where)))
    if failed:
        ui.notify('Could not update {}'.format(', '.join(failed)), error=True)
    if not where and not failed:
        ui.notify('{}: nothing to update (log in to a service or enable the Kodi option)'.format(what), error=True)


def _kodi_wanted():
    return cfg.get_bool('edit_update_kodi', True)


def _apply_kodi(entry, action, library, **kw):
    """Run the Kodi library updates for the action. -> True when something was changed."""
    if not _kodi_wanted():
        return False
    movies, shows, episodes = library
    updates = manualmap.kodi_updates(entry, action, kodimap.KodiIndex(movies, shows, episodes), **kw)
    applied = 0
    for method, params in updates:
        if 'error' not in kodisync._rpc(method, params):
            applied += 1
    return applied > 0


def pick_time():
    """-> epoch seconds ("Now" or a picked local date and time), or None when cancelled."""
    dialog = xbmcgui.Dialog()
    choice = dialog.select('When was it watched?', ['Now', 'Pick a date and time'])
    if choice is None or choice < 0:
        return None
    if choice == 0:
        return time.time()
    now = time.localtime()
    day = dialog.numeric(1, 'Date watched (DD/MM/YYYY)', time.strftime('%d/%m/%Y', now))
    if not day:
        return None
    clock = dialog.numeric(2, 'Time watched (HH:MM)', time.strftime('%H:%M', now))
    if not clock:
        return None
    try:
        return time.mktime(time.strptime('{} {}'.format(day.replace(' ', ''), clock.replace(' ', '').replace('.', ':')),
                                         '%d/%m/%Y %H:%M'))
    except (ValueError, OverflowError):
        dialog.ok('Date and time', 'That date or time is not valid.')
        return None


def _scope(entry):
    """Episodes: this one only, or everything up to it. -> 'one' | 'up_to' | None (cancelled)."""
    if entry['kind'] != 'episode':
        return 'one'
    choice = xbmcgui.Dialog().select('Mark as watched', ['This episode only',
                                                         'This episode and all previous episodes (all seasons)'])
    return None if choice is None or choice < 0 else ('one', 'up_to')[choice]


def _confirm_bulk(entry, scope, library):
    pairs = manualmap.episode_pairs(entry, scope, manualmap.library_pairs(kodimap.KodiIndex(*library), entry))
    if len(pairs) > 1 or entry['kind'] in ('show', 'season'):
        return xbmcgui.Dialog().yesno('Mark as Watched', 'Mark {} episode(s) of {} as watched?'.format(
            len(pairs) or 'all', manualmap.label(entry)))
    return True


def mark_watched(entry, library):
    scope = _scope(entry)
    if scope is None:
        return
    when = pick_time()
    if when is None:
        return
    if not _confirm_bulk(entry, scope, library):
        return
    stamp = manualmap.iso(when)
    pairs = manualmap.library_pairs(kodimap.KodiIndex(*library), entry)
    payload = manualmap.history_payload(entry, stamp, scope, pairs)
    done, failed = _send(_targets(), 'add_history', payload) if payload else ([], [])
    kodi = _apply_kodi(entry, 'watch', library, scope=scope, when=stamp)
    _report('Marked as watched', done, failed, kodi)
    entry.update(ts=when, progress=100)


def mark_unwatched(entry, library):
    if not xbmcgui.Dialog().yesno('Mark as Unwatched', 'Mark {} as unwatched and remove its rating?'.format(manualmap.label(entry))):
        return
    pairs = manualmap.library_pairs(kodimap.KodiIndex(*library), entry)
    payload = manualmap.history_payload(entry, None, 'one', pairs, dated=False)
    done, failed = _send(_targets(), 'remove_history', payload) if payload else ([], [])
    kind = 'movie' if entry['kind'] == 'movie' else ('show' if entry['kind'] == 'show' else 'episode')
    rated = manualmap.rating_payload(entry)
    if rated:
        _send(_targets(lambda p: p.rating_supported(kind)), 'remove_ratings', rated)
    kodi = _apply_kodi(entry, 'unwatch', library, scope='one')
    _report('Marked as unwatched (rating removed)', done, failed, kodi)
    entry.update(progress=0)


def rate(entry, library):
    ref = manualmap.ref(entry)
    targets = _targets(lambda p: p.rating_supported(ref['kind']))
    value = rating.prompt(manualmap.label(entry), cfg.get_int('rate_default', 6))
    if not value:
        return
    when = entry.get('ts') if cfg.get_bool('rating_date', True) else None
    ok = rating.submit(ref, value, manualmap.label(entry), targets, when=when) if targets else False
    kodi = _apply_kodi(entry, 'rate', library, rating=value)
    if not ok and not kodi:
        ui.notify('Could not save the rating', error=True)


def unrate(entry, library):
    ref = manualmap.ref(entry)
    payload = manualmap.rating_payload(entry)
    done, failed = _send(_targets(lambda p: p.rating_supported(ref['kind'])), 'remove_ratings', payload) if payload else ([], [])
    kodi = _apply_kodi(entry, 'unrate', library)
    _report('Rating removed', done, failed, kodi)


def _can_rate(entry):
    if entry['kind'] == 'season':
        return False
    if entry['kind'] == 'episode':
        return cfg.get_bool('rate_episode', False)
    return True


def edit_recent():
    dialog = xbmcgui.Dialog()
    data = recent.load()
    kinds = [k for k in manualmap.KINDS if data[k]]
    if not kinds:
        dialog.ok('Edit recent items', 'Nothing yet. The last things you play will appear here.')
        return
    pick = dialog.select('Edit recent items', ['{} ({})'.format(manualmap.TITLES[k], len(data[k])) for k in kinds])
    if pick is None or pick < 0:
        return
    kind = kinds[pick]
    index = dialog.select(manualmap.TITLES[kind], [manualmap.label(e) for e in data[kind]])
    if index is None or index < 0:
        return
    entry = data[kind][index]
    actions = [WATCH, UNWATCH] + ([RATE, UNRATE] if _can_rate(entry) else [])
    chosen = dialog.select(manualmap.label(entry), actions)
    if chosen is None or chosen < 0:
        return
    library = kodisync.read_library()
    try:
        {WATCH: mark_watched, UNWATCH: mark_unwatched, RATE: rate, UNRATE: unrate}[actions[chosen]](entry, library)
    except Exception as e:                                      # never leave the user with a silent failure
        log.error('manual edit failed: {!r}'.format(e))
        ui.notify('The change failed: {}'.format(e), error=True)
        return
    recent.save(data)
