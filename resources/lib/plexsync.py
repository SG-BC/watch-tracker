"""Sync between Plex (the chosen servers and libraries) and MDBList, Simkl or WeTrakr. Add-only, both directions.

Services -> Plex: the source is read exactly as for Kodi (sync.run for MDBList, the Simkl / WeTrakr readers in
kodisync), then each chosen server is read and gets what it lacks: watched (Plex dates it "now": it keeps no chosen
watch date) and ratings where it has none (with their own date, `ratedAt`).
Plex -> services: every chosen library is read (items + the play history with real dates); the same item on several
servers counts once (see plexmap); then sent like any other pair.
A server that does not answer is skipped with a note, and the run is not marked as done, so the next run tries again."""
import json
import os
from collections import Counter

from . import cfg, kodimap, kodisync, log, plexapi, plexmap, providers, sync

LABELS = {'mdblist': 'MDBList', 'simkl': 'Simkl', 'wetrakr': 'WeTrakr'}


def _file():
    return os.path.join(cfg.profile_dir(), 'plex_sync.json')


def load_state():
    try:
        with open(_file(), 'r', encoding='utf-8') as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_state(state):
    try:
        with open(_file(), 'w', encoding='utf-8') as f:
            json.dump(state, f)
    except OSError as e:
        log.warning('could not save plex_sync.json: {}'.format(e))


def reset_state():
    try:
        os.remove(_file())
    except OSError:
        pass


def _ready():
    reason = plexapi.ready()
    if reason:
        raise sync.SyncError(reason)


def read_library(server, libraries, history_since=None, history=False):
    """Read the chosen libraries of one server -> plexmap.Library. Raises plexapi.ServerDown."""
    lib = plexmap.Library(server.name)
    for chosen in libraries:
        key = chosen['key']
        if chosen.get('type') == 'movie':
            lib.add_movies(server.pages('/library/sections/{}/all'.format(key), {'type': 1, 'includeGuids': 1}))
        elif chosen.get('type') == 'show':
            lib.add_shows(server.pages('/library/sections/{}/all'.format(key), {'type': 2, 'includeGuids': 1}))
            lib.add_episodes(server.pages('/library/sections/{}/allLeaves'.format(key), {'includeGuids': 1}))
        if history:
            params = {'librarySectionID': key, 'sort': 'viewedAt:desc'}
            if server.owned:
                params['accountID'] = 1                    # the owner's own plays (an admin sees every user's)
            if history_since:
                params['viewedAt>'] = plexmap.to_epoch(history_since)
            lib.add_history(server.pages('/status/sessions/history/all', params))
    return lib


# ------------------------------------------------------------------------------ services -> Plex
def _read_source(ui, dry, src):
    """-> (rows for kodimap.simkl_to_kodi, commit()) or (None, None) when the source has nothing new."""
    if src == 'mdblist':
        results = sync.run(ui, dry=dry, dest='plex')          # keeps its own progress (sync_state_plex.json)
        history, ratings = results.pop('collected')
        return kodimap.neutral_to_rows(history, ratings), lambda: None
    state = kodisync.load_state()
    key = '{}_to_plex'.format(src)
    if src == 'simkl':
        read = kodisync._read_simkl(ui, state, key)
        if read is None:
            return None, None
        rows, snapshot = read[0], read[1]
    else:
        read = kodisync._read_wetrakr(ui, state, key)
        if read is None:
            return None, None
        rows, snapshot = kodimap.neutral_to_rows(read[0], read[1]), read[2]

    def commit():
        state[key] = snapshot
        kodisync.save_state(state)
    return rows, commit


def _apply(server, acts, stats):
    for kind, key, value in acts:
        res = server.scrobble(key) if kind == 'scrobble' else server.rate(key, value[0], value[1])
        if not res.ok:
            stats['failed'] += 1
            if res.network_error:
                raise plexapi.ServerDown('{}: no answer'.format(server.name))


def to_plex(ui, dry, src):
    _ready()
    if src != 'mdblist' and not dry:
        kodisync._need(src)
    rows, commit = _read_source(ui, dry, src)
    if rows is None:
        return {'unchanged': True, 'src': src}
    ui.update(50, 'Connecting to Plex...')
    targets, skipped = plexapi.connect_chosen()
    done = []
    for n, (server, libraries) in enumerate(targets):
        if ui.cancelled():
            raise sync.Cancelled()
        ui.update(55 + int(40 * n / max(1, len(targets))), 'Plex: {}...'.format(server.name))
        stats = Counter()
        try:
            lib = read_library(server, libraries)
            updates = kodimap.simkl_to_kodi(rows, lib.index(), stats, dated=True)
            if not dry:
                _apply(server, plexmap.actions(updates), stats)
        except plexapi.ServerDown as e:
            skipped.append((server.name, str(e).split(': ', 1)[-1]))
            continue
        stats['empty'] = int(lib.empty())
        done.append((server.name, stats))
    if not dry and not skipped:
        commit()
    return {'servers': done, 'skipped': skipped, 'src': src}


def summary_to_plex(results, dry=False):
    src = LABELS.get(results.get('src'), results.get('src'))
    if results.get('unchanged'):
        return 'Nothing has changed on {} since the last sync.'.format(src)
    verb = 'Would mark' if dry else 'Marked'
    lines, watched = [], 0
    for name, s in results['servers']:
        line = '{}: {} {} movies and {} episodes watched, {} ratings and {} episode ratings added.'.format(
            name, verb, s['movies'], s['episodes'], s['ratings'], s['episode_ratings'])
        if s['empty']:
            line += ' The chosen libraries are empty.'
        elif s['not_in_kodi']:
            line += ' {} {} items are not in the chosen libraries.'.format(s['not_in_kodi'], src)
        if s['failed']:
            line += ' {} changes were refused; see the debug log.'.format(s['failed'])
        lines.append(line)
        watched += s['movies'] + s['episodes']
    for name, reason in results['skipped']:
        lines.append('{}: skipped ({}).'.format(name, reason))
    if watched:
        lines.append('Plex keeps no watch dates: items marked watched are dated today. Ratings keep their date.')
    if results['skipped'] and not dry:
        lines.append('Run the sync again when the skipped servers are back.' if results.get('src') != 'mdblist' else
                     'MDBList remembers what it sent: use "Re-sync everything" once the skipped servers are back.')
    lines.append('Nothing was changed.' if dry else 'Nothing was un-watched or overwritten.')
    return '\n'.join(lines)


# ------------------------------------------------------------------------------ Plex -> services
def from_plex(ui, dry, dst):
    _ready()
    dest = providers.get(dst) if dry else kodisync._need(dst)
    state, started = load_state(), sync.utc_now()
    key = 'to_' + dst
    since = (state.get(key) or {}).get('since')
    targets, skipped = plexapi.connect_chosen(ui)
    libraries = []
    for n, (server, chosen) in enumerate(targets):
        if ui.cancelled():
            raise sync.Cancelled()
        ui.update(10 + int(45 * n / max(1, len(targets))), 'Reading Plex: {}...'.format(server.name))
        try:
            libraries.append(read_library(server, chosen, history_since=since, history=True))
        except plexapi.ServerDown as e:
            skipped.append((server.name, str(e).split(': ', 1)[-1]))
    stats, totals, unmatched = Counter(), Counter(), []
    history, ratings = plexmap.plex_to_neutral(libraries, since, stats)
    if not dry:
        kodisync._push(ui, dest, history, ratings, totals, unmatched, 60)
        if not skipped:
            state[key] = {'since': started}
            save_state(state)
    return {'stats': stats, 'totals': totals, 'unmatched': unmatched, 'since': since,
            'servers': [lib.name for lib in libraries], 'skipped': skipped, 'dst': dst}


def summary_from_plex(results, dry=False):
    dst = LABELS.get(results['dst'], results['dst'])
    s, t = results['stats'], results['totals']
    lines = ['Read from Plex: {}.'.format(', '.join(results['servers']) or 'no server answered')]
    lines.append('{} to {}: {} movies, {} episodes ({} with their Plex date, {} undated = watched today), {} ratings.'.format(
        'Would send' if dry else 'Sent', dst, s['movies'], s['episodes'], s['dated'], s['undated'], s['ratings']))
    if results.get('since'):
        lines.append('Only items watched or rated since the last sync were checked.')
    if s['unmatched']:
        lines.append('{} Plex items have no usable ids or title and year and were skipped.'.format(s['unmatched']))
    if s['ignored']:
        lines.append('{} episode ratings were ignored (the sync sends movie and show ratings).'.format(s['ignored']))
    if t['not_found']:
        names = results.get('unmatched') or []
        lines.append('{} items could not be matched on {}{}.'.format(
            t['not_found'], dst, ' (e.g. {})'.format(', '.join(str(x) for x in names[:5])) if names else ''))
    if t['failed']:
        lines.append('{} items were refused by {}; see the debug log.'.format(t['failed'], dst))
    for name, reason in results['skipped']:
        lines.append('{}: skipped ({}).'.format(name, reason))
    if results['skipped'] and not dry:
        lines.append('Run the sync again when the skipped servers are back.')
    if dry:
        lines.append('Nothing was sent or saved.')
    return '\n'.join(lines)
