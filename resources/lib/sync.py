"""Manual one-way sync from MDBList to Simkl or to the Kodi library (watch history, then ratings). Add-only.

History runs in two passes so watch dates survive:
  1. `plays=all`: one row per play with that play's own date. The latest play of each movie/episode
     is sent with its real date (this pass is read completely, then sent).
  2. the plain item list, page by page: anything pass 1 did not cover is sent with the item's own
     date, or -- when MDBList has no date for it -- as watched today (Simkl's default). Whole-show /
     whole-season markers are only sent when MDBList has no episode rows for them.
Rated items that are not in the watch history are filed on Simkl with their rating date (otherwise
Simkl would file them "today"). Every timestamp is passed through unchanged as the same instant
(offsets converted to UTC).
Only a timestamp is stored after a full success, so later runs fetch just what changed (`since`).
Simkl is only ever WRITTEN to (never polled), so Simkl's /sync/all-items rules do not apply.
Into Kodi the same passes are collected and applied through kodisync (nothing is rated-only back-dated there).
"""
import json
import os
import time
from collections import Counter

from . import cfg, log, net, pending_ratings, providers, syncmap

START_BATCH = 1000        # events per Simkl call; halved when Simkl refuses a call
MIN_BATCH = 10
PAGE_LIMIT = 1000


class SyncError(Exception):
    pass


class Cancelled(Exception):
    pass


def _state_file(dest='simkl'):
    return os.path.join(cfg.profile_dir(), 'sync_state.json' if dest == 'simkl' else 'sync_state_{}.json'.format(dest))


def load_state(dest='simkl'):
    try:
        with open(_state_file(dest), 'r', encoding='utf-8') as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_state(state, dest='simkl'):
    try:
        with open(_state_file(dest), 'w', encoding='utf-8') as f:
            json.dump(state, f)
    except OSError as e:
        log.warning('could not save sync state: {}'.format(e))


def reset_state():
    for dest in ('simkl', 'kodi', 'wetrakr', 'anilist'):
        try:
            os.remove(_state_file(dest))
        except OSError:
            pass


def batch_for(provider):
    """Events per sync call for a writing service (slow services ask for less)."""
    return min(START_BATCH, getattr(provider, 'batch_size', START_BATCH))


def utc_now():
    return time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())


def _label(item):
    if isinstance(item, dict):
        title = item.get('title') or (item.get('show') or {}).get('title') or (item.get('movie') or {}).get('title')
        if title:
            return '{} ({})'.format(title, item.get('year')) if item.get('year') else str(title)
        return json.dumps(item.get('ids') or item, default=str)[:80]
    return str(item)[:80]


class Collector:
    """Stands in for a destination service when the destination is the Kodi library: keeps what would have been sent."""
    name, label = 'kodi', 'Kodi'

    def __init__(self):
        self.history, self.ratings = {}, {}

    @staticmethod
    def _merge(into, payload):
        for key, rows in payload.items():
            into.setdefault(key, []).extend(rows)

    def add_history(self, payload):
        self._merge(self.history, payload)
        return net.Result(status=200, body={})

    def add_ratings(self, payload):
        self._merge(self.ratings, payload)
        return net.Result(status=200, body={})

    def add_to_list(self, payload):
        return net.Result(status=200, body={})


class Pusher:
    def __init__(self, fn, batch, stats, unmatched=None, label='Simkl'):
        self.fn, self.batch, self.stats, self.label = fn, max(MIN_BATCH, batch), stats, label
        self.unmatched = unmatched if unmatched is not None else []
        self.failures = 0

    def push(self, payload):
        for chunk in syncmap.split(payload, self.batch):
            self._send(chunk)

    def _tally(self, body):
        added = body.get('added') or {}
        for key in ('movies', 'shows', 'episodes'):
            if isinstance(added.get(key), int):
                self.stats['added_' + key] += added[key]
        for kind, value in (body.get('not_found') or {}).items():
            if isinstance(value, list):
                self.stats['not_found'] += len(value)
                for item in value:
                    label = _label(item)
                    if len(self.unmatched) < 30:           # keep the first 30 for the debug log / summary
                        self.unmatched.append(label)
                        log.debug('not matched on {} ({}): {}'.format(self.label, kind, label))

    def _send(self, chunk):
        n = syncmap.count_events(chunk)
        res = self.fn(chunk)
        if not res.ok and res.network_error:
            time.sleep(3)
            res = self.fn(chunk)                       # one retry for a network blip
            if not res.ok and res.network_error:
                raise SyncError('Network problem while talking to {}. Run the sync again; it resumes where it stopped.'.format(self.label))
        if res.ok:
            self._tally(res.body)
            self.failures = 0
            self.stats['sent'] += n
            return
        if res.status == 429:
            raise SyncError('{} request limit reached. Run the sync again later; it resumes where it stopped.'.format(self.label))
        if res.auth_error:
            raise SyncError('{} login is missing or expired. Log in again in the settings.'.format(self.label))
        if n > MIN_BATCH:                              # refused: maybe too big, halve and retry
            self.batch = max(MIN_BATCH, n // 2)
            log.debug('{} refused {} events ({}), batch is now {}'.format(self.label, n, net.summarize(res), self.batch))
            for part in syncmap.split(chunk, self.batch):
                self._send(part)
            return
        self.failures += 1
        self.stats['failed'] += n
        log.warning('{} refused a small batch of {}: {}'.format(self.label, n, net.summarize(res)))
        if self.failures >= 3:
            raise SyncError('{} keeps refusing the data ({}). See the debug log.'.format(self.label, net.summarize(res)))


def _sample(label, page):
    """First rows of each kind, for the (redacted, in-memory) debug log: shows exactly what MDBList sends."""
    for key in ('movies', 'shows', 'seasons', 'episodes'):
        for row in (page.get(key) or [])[:2]:
            log.debug('sample {} {}: {}'.format(label, key, json.dumps(row, default=str)[:300]))


class _Run:
    def __init__(self, ui, dry, dest='simkl'):
        self.ui, self.dry, self.dest_name = ui, dry, dest
        self.mdb = providers.get('mdblist')
        self.sim = providers.get('simkl') if dest == 'simkl' else Collector()      # the destination writer
        self.state = {} if dry else load_state(dest)
        self.results = {'history': Counter(), 'ratings': Counter(), 'totals': Counter(), 'unmatched': []}
        self.ctx = None
        self.started = utc_now()

    def save(self):
        if not self.dry:
            save_state(self.state, self.dest_name)

    def check_cancel(self):
        if self.ui.cancelled():
            self.save()
            raise Cancelled()

    def read_page(self, fetch, cursor, since, **kw):
        res = fetch(cursor=cursor, since=since, limit=PAGE_LIMIT, **kw)
        if res.auth_error:
            raise SyncError('MDBList login is missing or expired. Log in again in the settings.')
        if not res.ok:
            raise SyncError('Could not read from MDBList ({}). Try again.'.format(net.summarize(res)))
        return res.body

    def pusher(self, write):
        return Pusher(write, self.state.get('batch', START_BATCH), self.results['totals'], self.results['unmatched'],
                      self.sim.label)

    def push(self, pusher, payload):
        if payload and (not self.dry or self.dest_name != 'simkl'):     # a collecting destination (Kodi, ...) still collects in the preview
            pusher.push(payload)
            self.state['batch'] = pusher.batch

    def progress(self, base, span, fetched, total, label):
        done = min(1.0, fetched / total) if total else 0.0
        self.ui.update(int(base + done * span), '{}: {} of {} rows'.format(label, fetched, total or '?'))

    # ---- pass 1: dated plays
    def plays(self):
        st = self.state.setdefault('plays', {})
        since, cursor, fetched = st.get('since'), None, 0
        index = syncmap.PlayIndex()
        stats = self.results['history']
        for n in range(10000):
            self.check_cancel()
            page = self.read_page(self.mdb.get_watched, cursor, since, plays=True)
            if n == 0:
                _sample('plays', page)
            index.add_page(page, stats)
            fetched += sum(len(page.get(k) or []) for k in ('movies', 'episodes'))
            pagination = page.get('pagination') or {}
            cursor = pagination.get('next_cursor')
            self.progress(0, 30, fetched, pagination.get('total') or 0, 'Watch history (plays)')
            if not cursor:
                break
        self.push(self.pusher(self.sim.add_history if self.sim else None), index.payload(stats))
        if not self.dry:
            st['since'] = self.started
            self.save()
        return index

    # ---- pass 2: item rows (undated items, markers)
    def items(self, index):
        st = self.state.setdefault('items', {})
        if not st.get('run_started') or self.dry:
            st.update(run_started=self.started, run_since=st.get('since'), cursor=None)
        since, cursor = st.get('run_since'), st.get('cursor')
        pusher = self.pusher(self.sim.add_history if self.sim else None)
        stats, ctx, fetched = self.results['history'], syncmap.Context(index), 0
        self.ctx = ctx
        first = True
        while True:
            self.check_cancel()
            page = self.read_page(self.mdb.get_watched, cursor, since)
            if first:
                _sample('items', page)
                first = False
            rows = sum(len(page.get(k) or []) for k in ('movies', 'shows', 'seasons', 'episodes'))
            fetched += rows
            self.push(pusher, syncmap.history_items_to_simkl(page, stats, ctx))
            pagination = page.get('pagination') or {}
            cursor = pagination.get('next_cursor')
            st['cursor'] = cursor
            self.save()
            self.progress(30, 40, fetched, pagination.get('total') or 0, 'Watch history (items)')
            if not cursor or rows == 0:
                break
        self.push(pusher, ctx.markers(stats))
        if not self.dry:
            st.update(since=st['run_started'], run_started=None, run_since=None, cursor=None)
            self.save()

    # ---- ratings (+ back-dated entries for rated-only items)
    def ratings(self):
        st = self.state.setdefault('ratings', {})
        if not st.get('run_started') or self.dry:
            st.update(run_started=self.started, run_since=st.get('since'), cursor=None)
        since, cursor = st.get('run_since'), st.get('cursor')
        sim = self.sim
        history_push = self.pusher(sim.add_history if sim else None)
        lists_push = self.pusher(sim.add_to_list if sim else None)
        ratings_push = self.pusher(sim.add_ratings if sim else None)
        ctx = self.ctx or syncmap.Context()
        stats, fetched, first = self.results['ratings'], 0, True
        while True:
            self.check_cancel()
            page = self.read_page(self.mdb.get_ratings, cursor, since)
            if first:
                _sample('ratings', page)
                first = False
            rows = sum(len(page.get(k) or []) for k in ('movies', 'shows', 'seasons', 'episodes'))
            fetched += rows
            ratings, backdated_history, backdated_lists = syncmap.ratings_page(
                page, stats, ctx.seen_movies, ctx.seen_shows, episodes=self.dest_name == 'kodi')
            # file rated-only items with their rating date FIRST, then rate them
            if self.dest_name != 'simkl':          # filing rated-only items with their date is Simkl's quirk
                backdated_history, backdated_lists = {}, {}
            self.push(history_push, backdated_history)
            self.push(lists_push, backdated_lists)
            self.push(ratings_push, ratings)
            if backdated_lists and not self.dry:       # rated first, watched later: re-dated when watched
                pending_ratings.add_from_payloads('simkl', backdated_lists, ratings)
            pagination = page.get('pagination') or {}
            cursor = pagination.get('next_cursor')
            st['cursor'] = cursor
            self.save()
            self.progress(70, 30, fetched, pagination.get('total') or 0, 'Ratings')
            if not cursor or rows == 0:
                break
        if not self.dry:
            st.update(since=st['run_started'], run_started=None, run_since=None, cursor=None)
            self.save()
        self.state['batch'] = min(history_push.batch, lists_push.batch, ratings_push.batch, self.state.get('batch', START_BATCH))


def run(ui, dry=False, dest='simkl'):
    """ui needs: update(percent, text) and cancelled(). dest: 'simkl' or 'kodi' (the Kodi library).
    dry=True (the preview): reads MDBList only -- nothing is written and nothing is saved.
    Returns {'history','ratings','totals'} Counters (+ 'collected' for Kodi)."""
    if not providers.get('mdblist').logged_in():
        raise SyncError('Log in to MDBList in the settings first.')
    if dest == 'simkl' and not dry and not providers.get('simkl').logged_in():
        raise SyncError('Log in to Simkl in the settings first.')
    job = _Run(ui, dry, dest)
    index = job.plays()
    job.items(index)
    job.ratings()
    job.save()
    if dest != 'simkl':
        job.results['collected'] = (job.sim.history, job.sim.ratings)
    if dest != 'simkl':      # the pair's own summary is logged by pairs.run; this adds what MDBList held
        log.debug('mdblist read {} | {}'.format('preview' if dry else 'sync',
                                               summary(job.results, dry, dest_label(dest)).replace('\n', ' | ')))
    return job.results


def _dates_line(h):
    line = '{} items keep their MDBList watch date'.format(h['dated'])
    if h.get('oldest'):
        line += ' ({} to {} UTC)'.format(h['oldest'][:10], h['newest'][:10])
    line += '; {} have no date in MDBList and are imported as watched today.'.format(h['undated'])
    return line


def dest_label(dest):
    return 'Kodi' if dest == 'kodi' else providers.get(dest).label


def summary(results, dry=False, label='Simkl'):
    h, r, t = results['history'], results['ratings'], results['totals']
    verb = 'Would send' if dry else 'Sent'
    lines = ['{} watch history: {} movies, {} episodes, {} whole shows, {} whole seasons.'.format(
        verb, h['movies'], h['episodes'], h['shows'], h['seasons']),
        _dates_line(h),
        '{} ratings: {} movies, {} shows (half stars rounded up).'.format(verb, r['movies'], r['shows'])]
    if label != 'Simkl':
        lines[0] = lines[0].replace('Would send', 'Would read').replace('Sent', 'Read')
        lines[2] = lines[2].replace('Would send', 'Would read').replace('Sent', 'Read')
    if label == 'Kodi' and r['episodes']:
        lines[2] += ' Episode ratings: {}.'.format(r['episodes'])
    if label == 'Simkl' and (r['backdated_movies'] or r['backdated_shows']):
        lines.append('Rated but never watched: {} movies and {} shows are filed on Simkl with their rating date. They get the watch date once you watch them.'.format(
            r['backdated_movies'], r['backdated_shows']))
    if t['not_found']:
        names = results.get('unmatched') or []
        more = ' (e.g. {})'.format(', '.join(names[:5])) if names else ''
        lines.append('{} items could not be matched on {}{}.'.format(t['not_found'], label, more))
    skipped = h['skipped'] + r['skipped']
    if skipped:
        lines.append('{} rows were skipped (no usable ids, or no title and year).'.format(skipped))
    if r['ignored']:
        if label == 'Kodi':
            lines.append('{} season ratings were ignored (Kodi gets movie, show and episode ratings).'.format(r['ignored']))
        else:
            lines.append('{} season/episode ratings were ignored ({}).'.format(
                r['ignored'], 'Simkl has none' if label == 'Simkl' else 'not synced to ' + label))
    if t['failed']:
        lines.append('{} items were refused by {}; see the debug log.'.format(t['failed'], label))
    if dry:
        lines.append('Nothing was sent or saved. The debug log shows sample rows if Verbose logging is on.')
    return '\n'.join(lines)
