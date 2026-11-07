"""Edit recent items by hand: mark watched (now / a picked date and time), mark unwatched, rate, remove a rating.

Opened from the settings ("Edit recent items..."). Works on the last plays kept in recent.json and sends the change
to every logged-in, switched-on service and (by default) to the Kodi library too. No rating prompt after a manual
"mark as watched"."""
import time

import xbmcgui

from . import cfg, kodimap, kodisync, log, manualmap, plexapi, plexmap, providers, rating, recent, ui

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


# ------------------------------------------------------------------------------ Plex (the chosen libraries)
def _plex_wanted():
    return cfg.get_bool('edit_update_plex', True) and plexapi.ready() is None


def _plex_find(server, libraries, entry):
    """-> (record, episodes) on one server: the movie / show (Kodi-shaped row) found in the chosen libraries by a
    title search checked against the ids, and for a show its episodes {(season, episode): row}."""
    base = manualmap.item(entry)
    if base is None:
        return None, {}
    kind = 'movie' if entry['kind'] == 'movie' else 'show'
    lib = plexmap.Library(server.name)
    for chosen in libraries:
        if chosen.get('type') == kind:
            rows = server.pages('/library/sections/{}/all'.format(chosen['key']),
                                {'type': 1 if kind == 'movie' else 2, 'includeGuids': 1, 'title': entry.get('title') or ''})
            (lib.add_movies if kind == 'movie' else lib.add_shows)(rows)
    index = lib.index()
    record = index.movie(base) if kind == 'movie' else index.show(base)
    if record is None or kind == 'movie':
        return record, {}
    lib.add_episodes(server.pages('/library/metadata/{}/allLeaves'.format(record['tvshowid']), {'includeGuids': 1}))
    return record, {(e['season'], e['episode']): e for e in lib.episodes}


def _plex_calls(entry, action, record, episodes, scope, rating, when):
    """-> [(Server method name, args)] for one server."""
    if entry['kind'] == 'movie':
        key = record['movieid']
        return [{'watch': ('scrobble', (key,)), 'unwatch': ('unscrobble', (key,)),
                 'rate': ('rate', (key, rating, when))}[action]]
    if action == 'rate':
        if entry['kind'] == 'show':
            return [('rate', (record['tvshowid'], rating, when))]
        row = episodes.get((entry['season'], entry['episode']))
        return [('rate', (row['episodeid'], rating, when))] if row else []
    if action == 'unwatch' and entry['kind'] == 'show':
        return [('unscrobble', (record['tvshowid'],))]
    pairs = manualmap.episode_pairs(entry, scope if action == 'watch' else 'one', sorted(episodes))
    single = entry['kind'] == 'episode' and (scope == 'one' or action == 'unwatch')
    calls = []
    for pair in pairs:
        row = episodes.get(pair)
        if row is None:
            continue
        if action == 'watch' and (single or row['playcount'] == 0):    # "all previous": only what is unwatched there
            calls.append(('scrobble', (row['episodeid'],)))
        elif action == 'unwatch' and (single or row['playcount'] > 0):
            calls.append(('unscrobble', (row['episodeid'],)))
    return calls


def _apply_plex(entry, action, scope='one', rating=None, when=None):
    """Run the action on every chosen Plex library that has the item. -> (labels done, labels failed)."""
    done, failed = [], []
    if not _plex_wanted():
        return done, failed
    if action == 'unrate':
        ui.notify('Plex: removing a rating is not supported yet')
        return done, failed
    targets, skipped = plexapi.connect_chosen()
    failed += ['Plex ({})'.format(name) for name, _ in skipped]
    for server, libraries in targets:
        label = 'Plex ({})'.format(server.name)
        try:
            record, episodes = _plex_find(server, libraries, entry)
            if record is None:
                log.debug('plex {}: {} is not in the chosen libraries'.format(server.name, manualmap.label(entry)))
                continue
            calls = _plex_calls(entry, action, record, episodes, scope, rating, when)
            results = [getattr(server, name)(*args) for name, args in calls]
        except plexapi.ServerDown as e:
            log.debug('plex edit: {}'.format(e))
            failed.append(label)
            continue
        if results:
            (done if all(r.ok for r in results) else failed).append(label)
    return done, failed


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


LISTERS = ('simkl', 'wetrakr')         # episode lists when the show is not in the Kodi library, in this order


def _lister_ok(p):
    """Simkl's catalog needs no login (only finding its show id does); WeTrakr needs one."""
    return p.name == 'simkl' or p.enabled()


def episode_list(entry, library):
    """Every (season, episode) of the entry's show: the Kodi library first, else the first service that can list
    them (aired episodes, no specials). A show id a service looks up is kept in entry['native'].
    -> (pairs, where) with where 'Kodi' | a service label | None (nothing found: pairs == [])."""
    pairs = manualmap.library_pairs(kodimap.KodiIndex(*library), entry)
    if pairs:
        return pairs, 'Kodi'
    ref = {'kind': 'show', 'ids': entry.get('ids') or {}, 'title': entry.get('title'), 'year': entry.get('year'),
           'native': entry.setdefault('native', {})}
    for name in LISTERS:
        p = providers.get(name)
        if p is None or not _lister_ok(p):
            continue
        try:
            found = p.episode_list(ref)
        except Exception as e:
            log.error('{} episode list failed: {!r}'.format(p.label, e))
            continue
        if found:
            log.debug('episode list from {}: {} episodes'.format(p.label, len(found)))
            return found, p.label
    log.debug('no episode list for {}'.format(manualmap.label(entry)))
    return [], None


def _confirm_bulk(entry, scope, pairs):
    if entry['kind'] == 'episode' and scope == 'one':
        return True
    return xbmcgui.Dialog().yesno('Mark as Watched', 'Mark {} of {} as watched?'.format(
        manualmap.scope_text(entry, scope, pairs), manualmap.label(entry)))


def mark_watched(entry, library):
    scope = _scope(entry)
    if scope is None:
        return
    when = pick_time()
    if when is None:
        return
    if scope == 'up_to':
        pairs, _ = episode_list(entry, library)
    else:
        pairs = manualmap.library_pairs(kodimap.KodiIndex(*library), entry)
    if not _confirm_bulk(entry, scope, pairs):
        return
    stamp = manualmap.iso(when)
    payload = manualmap.history_payload(entry, stamp, scope, pairs)
    done, failed = _send(_targets(), 'add_history', payload) if payload else ([], [])
    kodi = _apply_kodi(entry, 'watch', library, scope=scope, when=stamp)
    plex_done, plex_failed = _apply_plex(entry, 'watch', scope=scope)
    _report('Marked as watched', done + plex_done, failed + plex_failed, kodi)
    if plex_done and when < time.time() - 600:
        ui.notify('Plex keeps no chosen date: marked as watched now')
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
    plex_done, plex_failed = _apply_plex(entry, 'unwatch')
    _report('Marked as unwatched (rating removed)', done + plex_done, failed + plex_failed, kodi)
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
    plex_done, plex_failed = _apply_plex(entry, 'rate', rating=value, when=when)
    if plex_failed:
        ui.notify('Could not rate on {}'.format(', '.join(plex_failed)), error=True)
    if not ok and not kodi and not plex_done:
        ui.notify('Could not save the rating', error=True)


def unrate(entry, library):
    ref = manualmap.ref(entry)
    payload = manualmap.rating_payload(entry)
    done, failed = _send(_targets(lambda p: p.rating_supported(ref['kind'])), 'remove_ratings', payload) if payload else ([], [])
    kodi = _apply_kodi(entry, 'unrate', library)
    _apply_plex(entry, 'unrate')
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
