"""Manual one-way syncs that involve the Kodi library, plus Simkl -> MDBList. Add-only.

Kodi -> Simkl / MDBList : watched movies/episodes with their last-played date (local time -> UTC), plus ratings
                that belong to those watches. Only items played since the last run are sent.
Simkl -> Kodi / MDBList : follows Simkl's two-step rule: GET /sync/activities first; if nothing changed since the
                saved snapshot, stop; otherwise read only the changed lists (date_from). Marks Kodi items
                watched (with Simkl's date) and fills in ratings where Kodi has none; or writes the same
                data to MDBList. Never un-watches.
MDBList -> Kodi lives in sync.py (it shares the MDBList reading passes).
Only the Kodi library is covered (PM4K and other streaming items are not in it).
"""
import json
import os
from collections import Counter

import xbmc

from . import cfg, kodimap, log, providers, sync

PAGE = 500
# (kind, Simkl type, status): which lists hold watched items. 'completed' shows need no episode list.
LISTS = [('movie', 'movies', 'completed')] + [('show', t, s) for t in ('shows', 'anime')
                                              for s in ('watching', 'hold', 'dropped', 'completed')]
EXTRA = {'watching': {'extended': 'full', 'episode_watched_at': 'yes'},
         'hold': {'extended': 'full', 'episode_watched_at': 'yes'},
         'dropped': {'extended': 'full', 'episode_watched_at': 'yes', 'include_all_episodes': 'original'}}


def _file():
    return os.path.join(cfg.profile_dir(), 'kodi_sync.json')


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
        log.warning('could not save kodi_sync.json: {}'.format(e))


def reset_state():
    try:
        os.remove(_file())
    except OSError:
        pass


def _rpc(method, params):
    try:
        return json.loads(xbmc.executeJSONRPC(json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': method, 'params': params})))
    except ValueError:
        return {}


def _library(method, key, props):
    out, start = [], 0
    while True:
        rows = (_rpc(method, {'properties': props, 'limits': {'start': start, 'end': start + PAGE}}).get('result') or {}).get(key) or []
        out.extend(rows)
        if len(rows) < PAGE:
            return out
        start += PAGE


def read_library():
    movies = _library('VideoLibrary.GetMovies', 'movies', ['title', 'year', 'playcount', 'lastplayed', 'userrating', 'uniqueid', 'imdbnumber'])
    shows = _library('VideoLibrary.GetTVShows', 'tvshows', ['title', 'year', 'userrating', 'uniqueid', 'imdbnumber'])
    episodes = _library('VideoLibrary.GetEpisodes', 'episodes', ['tvshowid', 'season', 'episode', 'playcount', 'lastplayed', 'userrating'])
    return movies, shows, episodes


def _need(name):
    provider = providers.get(name)
    if not provider.logged_in():
        raise sync.SyncError('Log in to {} in the settings first.'.format(provider.label))
    return provider


def apply_to_kodi(ui, updates, stats, base=50, span=50):
    """Run JSON-RPC updates against the Kodi library; returns how many were applied."""
    applied = 0
    for n, (method, params) in enumerate(updates):
        if n % 25 == 0:
            if ui.cancelled():
                raise sync.Cancelled()
            ui.update(base + int(span * n / max(1, len(updates))), 'Updating Kodi: {} of {}'.format(n, len(updates)))
        if 'error' in _rpc(method, params):
            stats['failed'] += 1
        else:
            applied += 1
    return applied


# ------------------------------------------------------------------------------ Kodi -> a service
def to_service(ui, dry, name):
    dest = providers.get(name) if dry else _need(name)
    state, started = load_state(), sync.utc_now()
    key = 'to_' + name
    since = (state.get(key) or {}).get('since')
    ui.update(5, 'Reading the Kodi library...')
    movies, shows, episodes = read_library()
    stats, totals, unmatched = Counter(), Counter(), []
    history, ratings = kodimap.kodi_to_simkl(movies, shows, episodes, since, stats)
    if not dry:
        for base, payload, write in ((30, history, dest.add_history), (70, ratings, dest.add_ratings)):
            if ui.cancelled():
                raise sync.Cancelled()
            ui.update(base, 'Sending to {}...'.format(dest.label))
            if payload:
                sync.Pusher(write, sync.START_BATCH, totals, unmatched, dest.label).push(payload)
        state[key] = {'since': started}
        save_state(state)
    results = {'stats': stats, 'totals': totals, 'unmatched': unmatched, 'since': since}
    log.debug('kodi to {} {} | {}'.format(name, 'preview' if dry else 'done',
                                         summary_to(results, dry, dest.label).replace('\n', ' | ')))
    return results


def to_simkl(ui, dry=False):
    return to_service(ui, dry, 'simkl')


def summary_to(results, dry=False, label='Simkl'):
    s, t = results['stats'], results['totals']
    lines = ['{} to {}: {} movies, {} episodes ({} with their Kodi date, {} undated = watched today), {} ratings.'.format(
        'Would send' if dry else 'Sent', label, s['movies'], s['episodes'], s['dated'], s['undated'], s['ratings'])]
    if results.get('since'):
        lines.append('Only items played since the last sync were checked.')
    if s['unmatched']:
        lines.append('{} Kodi items have no usable ids or title and year and were skipped.'.format(s['unmatched']))
    if s['ignored']:
        lines.append('{} episode ratings were ignored (the services have none for shows\' episodes).'.format(s['ignored']))
    if t['not_found']:
        names = results.get('unmatched') or []
        lines.append('{} items could not be matched on {}{}.'.format(
            t['not_found'], label, ' (e.g. {})'.format(', '.join(names[:5])) if names else ''))
    if t['failed']:
        lines.append('{} items were refused by {}; see the debug log.'.format(t['failed'], label))
    return '\n'.join(lines)


def summary_to_simkl(results, dry=False):
    return summary_to(results, dry, 'Simkl')


# ------------------------------------------------------------------------------ Simkl -> Kodi / MDBList
def _read_simkl(ui, state, state_key):
    """-> (rows, activities, date_from) or None when Simkl has nothing new since the last run."""
    sim = _need('simkl')
    snapshot = state.get(state_key) or {}
    ui.update(3, 'Asking Simkl what changed...')
    act = sim.get_activities()
    if act.auth_error:
        raise sync.SyncError('Simkl login is missing or expired. Log in again in the settings.')
    if not act.ok or not isinstance(act.body, dict):
        raise sync.SyncError('Could not read Simkl activity (HTTP {}). Try again.'.format(act.status))
    if snapshot.get('all') and act.body.get('all') == snapshot.get('all'):
        return None
    date_from = snapshot.get('all')
    rows = []
    for i, (kind, type_, status) in enumerate(LISTS):
        if ui.cancelled():
            raise sync.Cancelled()
        params = dict(EXTRA.get(status, {}))
        if date_from:
            params['date_from'] = date_from
        res = sim.get_all_items(type_, status, params)
        if res.auth_error:
            raise sync.SyncError('Simkl login is missing or expired. Log in again in the settings.')
        if not res.ok:
            raise sync.SyncError('Could not read Simkl ({} {}: HTTP {}). Try again.'.format(type_, status, res.status))
        body = res.body if isinstance(res.body, dict) else {}
        rows += [(kind, status, r) for r in (body.get(type_) or [])]
        ui.update(5 + int(35 * (i + 1) / len(LISTS)), 'Reading Simkl: {} {}'.format(type_, status))
    return rows, act.body, date_from


def from_simkl(ui, dry=False):
    state = load_state()
    read = _read_simkl(ui, state, 'simkl_activities')
    if read is None:
        return {'unchanged': True}
    rows, activities, date_from = read
    ui.update(45, 'Reading the Kodi library...')
    movies, shows, episodes = read_library()
    stats = Counter()
    updates = kodimap.simkl_to_kodi(rows, kodimap.KodiIndex(movies, shows, episodes), stats)
    applied = 0
    if not dry:
        applied = apply_to_kodi(ui, updates, stats)
        state['simkl_activities'] = activities
        save_state(state)
    results = {'stats': stats, 'updates': len(updates), 'applied': applied, 'first': not date_from}
    log.debug('simkl to kodi {} | {}'.format('preview' if dry else 'done', summary_from_simkl(results, dry).replace('\n', ' | ')))
    return results


def simkl_to_mdblist(ui, dry=False):
    mdb = providers.get('mdblist') if dry else _need('mdblist')
    state = load_state()
    read = _read_simkl(ui, state, 'simkl_to_mdblist')
    if read is None:
        return {'unchanged': True}
    rows, activities, date_from = read
    stats, totals, unmatched = Counter(), Counter(), []
    history, ratings = kodimap.simkl_rows_to_neutral(rows, stats)
    if not dry:
        for base, payload, write in ((50, history, mdb.add_history), (75, ratings, mdb.add_ratings)):
            if ui.cancelled():
                raise sync.Cancelled()
            ui.update(base, 'Sending to MDBList...')
            if payload:
                sync.Pusher(write, sync.START_BATCH, totals, unmatched, mdb.label).push(payload)
        state['simkl_to_mdblist'] = activities
        save_state(state)
    results = {'stats': stats, 'totals': totals, 'unmatched': unmatched, 'first': not date_from}
    log.debug('simkl to mdblist {} | {}'.format('preview' if dry else 'done', summary_simkl_to_mdblist(results, dry).replace('\n', ' | ')))
    return results


def summary_simkl_to_mdblist(results, dry=False):
    if results.get('unchanged'):
        return 'Nothing has changed on Simkl since the last sync.'
    s, t = results['stats'], results['totals']
    lines = ['{} to MDBList: {} movies, {} episodes, {} whole shows, {} ratings.'.format(
        'Would send' if dry else 'Sent', s['movies'], s['episodes'], s['shows'], s['ratings'])]
    if s['unmatched']:
        lines.append('{} Simkl items have no usable ids and were skipped.'.format(s['unmatched']))
    if t['not_found']:
        lines.append('{} items could not be matched on MDBList.'.format(t['not_found']))
    if t['failed']:
        lines.append('{} items were refused by MDBList; see the debug log.'.format(t['failed']))
    return '\n'.join(lines)


def summary_from_simkl(results, dry=False):
    if results.get('unchanged'):
        return 'Nothing has changed on Simkl since the last sync.'
    s = results['stats']
    lines = ['{} in Kodi: {} movies and {} episodes marked watched, {} ratings added.'.format(
        'Would change' if dry else 'Changed', s['movies'], s['episodes'], s['ratings'])]
    if s['not_in_kodi']:
        lines.append('{} Simkl items are not in your Kodi library.'.format(s['not_in_kodi']))
    if s['failed']:
        lines.append('{} Kodi updates failed; see the debug log.'.format(s['failed']))
    lines.append('Nothing was un-watched or overwritten.' if not dry else 'Nothing was changed.')
    return '\n'.join(lines)


# ------------------------------------------------------------------------------ MDBList -> Kodi (collected by sync.run)
def mdblist_to_kodi(ui, dry=False):
    results = sync.run(ui, dry=dry, dest='kodi')
    history, ratings = results.pop('collected')
    ui.update(88, 'Reading the Kodi library...')
    movies, shows, episodes = read_library()
    stats = Counter()
    updates = kodimap.simkl_to_kodi(kodimap.neutral_to_rows(history, ratings),
                                    kodimap.KodiIndex(movies, shows, episodes), stats)
    applied = 0 if dry else apply_to_kodi(ui, updates, stats, base=90, span=10)
    return {'sync': results, 'stats': stats, 'updates': len(updates), 'applied': applied}


def summary_mdblist_to_kodi(results, dry=False):
    lines = [summary_from_simkl(results, dry).replace('Simkl items', 'MDBList items')]
    h = results['sync']['history']
    lines.append('MDBList held {} movies and {} episodes with a watch record.'.format(h['movies'], h['episodes']))
    return '\n'.join(lines)
