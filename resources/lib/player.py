"""Kodi player monitor (service-agnostic core).

Per playback: the item is identified once; then each enabled service gets exactly
one start and one stop from its own worker thread. Kodi's player callbacks never block.
"""
import functools
import itertools
import json
import queue
import threading
import time
import urllib.parse

import xbmc
import xbmcgui

from . import cfg, ignore, journal, log, parsing, pending_ratings, providers, rating, redact
from .providers.base import ref_key

START_SETTLE_SECONDS = 1.0     # like script.trakt: let a resume seek happen before reading the position
RATING_DELAY_SECONDS = 2.5     # a player restart (stream switch) must not trigger the rating prompt
END_TOLERANCE_SECONDS = 120    # "ended" is trusted when at most this much (or 3 %) of the video was left
END_TOLERANCE_PERCENT = 3.0

_session_counter = itertools.count(1)


def to_int(value, default=None):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


class Session:
    """One playback. Created fresh for every play, so nothing leaks from earlier ones."""

    def __init__(self, info):
        self.id = '{}-{}'.format(int(time.time()), next(_session_counter))
        self.info = info
        self.ref = None
        self.providers = []        # names of the services this playback is reported to
        self.last_time = 0.0
        self.total_time = 0.0
        self.armed = False
        self.stop_position = 0.0   # seconds at stop (== duration when playback ended)
        self.stop_ts = None        # epoch seconds when playback stopped (the watch date)
        self.done = set()          # services whose stop already succeeded
        self.lock = threading.Lock()

    def progress(self):
        if self.total_time <= 0:
            return 0.0
        return round(max(0.0, min(100.0, self.last_time / self.total_time * 100.0)), 2)


class _Worker:
    """One queue + thread per service, so a slow or failing service never delays another."""

    def __init__(self, name, handler):
        self.queue = queue.Queue()
        self.thread = threading.Thread(target=self._run, args=(handler,), name='tracker-' + name, daemon=True)
        self.thread.start()

    def _run(self, handler):
        while True:
            job = self.queue.get()
            if job is None:
                return
            try:
                handler(*job)
            except Exception as e:
                log.error('worker error: {!r}'.format(e))


class PlayerMonitor(xbmc.Player):
    def __init__(self):
        super().__init__()
        self._session = None
        self._lock = threading.Lock()
        self._abort = threading.Event()
        self._rating_lock = threading.Lock()
        self._jobs = queue.Queue()
        self._workers = {p.name: _Worker(p.name, functools.partial(self._provider_job, p)) for p in providers.ALL}
        self._main = threading.Thread(target=self._run, name='tracker-main', daemon=True)
        self._main.start()

    # ------------------------------------------------------------------ Kodi callbacks
    def onAVStarted(self):
        if not self.isPlayingVideo():
            return
        self._finalize(None)          # close a previous session whose stop event never arrived
        try:
            info = self._fetch_info()
        except Exception as e:
            log.error('could not read item info: {!r}'.format(e))
            return
        if not info:
            return
        session = Session(info)
        self._update_times(session)
        with self._lock:
            self._session = session
        self._jobs.put(('prepare', session, 0))

    def onPlayBackStopped(self):
        self._finalize(None)

    def onPlayBackEnded(self):
        self._finalize(100.0)

    def onPlayBackError(self):
        self._finalize(None)

    # ------------------------------------------------------------------ local progress tracking
    def tick(self):
        """Called every few seconds by service.py. Local only -- no network."""
        with self._lock:
            s = self._session
        if s is None:
            return
        self._update_times(s)
        if s.ref is not None and s.providers and s.progress() >= self._arm_threshold(s):
            self._ensure_armed(s, s.progress())

    def _update_times(self, s):
        try:
            if self.isPlayingVideo():
                total, current = self.getTotalTime(), self.getTime()
                if total > 0:
                    s.total_time, s.last_time = total, current
        except RuntimeError:
            pass

    @staticmethod
    def _arm_threshold(s):
        marks = [providers.get(n).watched_mark() for n in s.providers if providers.get(n)]
        return min(marks) if marks else 80

    @staticmethod
    def _ensure_armed(s, progress):
        # The file is written under the same lock that records finished services, so a service that
        # finishes at the same moment can never be left listed as pending.
        with s.lock:
            if s.armed:
                return
            pending = [n for n in s.providers if n not in s.done]
            if not pending:
                return
            s.armed = True
            journal.arm(s.id, s.ref, progress, pending, s.stop_position or s.last_time, s.total_time)

    @staticmethod
    def _really_finished(s):
        """Was the video (nearly) at its end when Kodi said "ended"? Unknown length: trust Kodi."""
        if s.total_time <= 0:
            return True
        left = s.total_time - s.last_time
        return left <= max(END_TOLERANCE_SECONDS, s.total_time * END_TOLERANCE_PERCENT / 100.0)

    def _finalize(self, forced_progress):
        with self._lock:
            s, self._session = self._session, None
        if s is None:
            return
        self._clear_hint()
        s.stop_ts = time.time()
        if forced_progress == 100.0 and not self._really_finished(s):
            # Kodi also reports "ended" when the stream breaks (server timeout): that is a stop, not a finish
            log.debug('playback ended early ({:.0f} of {:.0f} s) -> treated as a stop'.format(s.last_time, s.total_time))
            forced_progress = None
        progress = forced_progress if forced_progress is not None else s.progress()
        s.stop_position = s.total_time if (forced_progress == 100.0 and s.total_time) else s.last_time
        self._jobs.put(('stop', s, progress))

    def recover_pending(self):
        self._jobs.put(('recover', None, 0))

    def shutdown(self):
        self._abort.set()
        self._finalize(None)
        self._jobs.put(None)
        self._main.join(timeout=4)
        for w in self._workers.values():
            w.thread.join(timeout=3)

    # ------------------------------------------------------------------ item info (callback thread)
    @staticmethod
    def _safe(fn, default=''):
        try:
            return fn()
        except Exception:
            return default

    @staticmethod
    def _jsonrpc(method, params=None):
        req = {'jsonrpc': '2.0', 'id': 1, 'method': method}
        if params:
            req['params'] = params
        try:
            return json.loads(xbmc.executeJSONRPC(json.dumps(req))).get('result') or {}
        except ValueError:
            return {}

    @staticmethod
    def _window():
        return xbmcgui.Window(10000)

    def _read_hint_ids(self):
        """'script.trakt.ids': ids published by streaming addons (PM4K does if script.trakt is installed)."""
        try:
            raw = self._window().getProperty('script.trakt.ids')
            data = json.loads(raw) if raw else {}
        except Exception:
            return {}
        return data if isinstance(data, dict) else {}

    def _clear_hint(self):
        try:
            self._window().clearProperty('script.trakt.ids')     # consume it, like script.trakt
        except Exception:
            pass

    def _merge_infolabels(self, info):
        """Fill what Player.GetItem left empty from the player's infolabels (as script.trakt does)."""
        label = lambda name: xbmc.getInfoLabel(name) or ''  # noqa: E731
        if to_int(info.get('season'), -1) < 0:
            season = to_int(label('VideoPlayer.Season'), -1)
            if season >= 0:
                info['season'] = season
        if to_int(info.get('episode'), -1) < 1:
            episode = to_int(label('VideoPlayer.Episode'), -1)
            if episode >= 1:
                info['episode'] = episode
        if not info.get('showtitle'):
            info['showtitle'] = label('VideoPlayer.TVShowTitle')
        if not info.get('title'):
            info['title'] = label('VideoPlayer.Title')
        if not to_int(info.get('year'), 0):
            year = label('VideoPlayer.Year')
            if year.isdigit():
                info['year'] = int(year)
        imdb = label('VideoPlayer.IMDBNumber')
        if imdb.startswith('tt'):
            info['_label_imdb'] = imdb
        if info.get('type') != 'movie' and (
                to_int(info.get('season'), -1) < 0 or to_int(info.get('episode'), -1) < 1):
            for text in (info.get('title'), info.get('showtitle'), label('VideoPlayer.EpisodeName')):
                name, season, episode = parsing.regex_tvshow(text or '')
                if season >= 0 and episode >= 1:
                    info['season'], info['episode'] = season, episode
                    if name and not parsing.usable_title(info.get('showtitle')):
                        info['showtitle'] = name
                    break

    @staticmethod
    def _apply_tmdb_helper(info):
        file_path = info.get('file') or ''
        if not file_path.startswith('plugin://plugin.video.themoviedb.helper/'):
            return
        params = {k: v[-1] for k, v in urllib.parse.parse_qs(urllib.parse.urlsplit(file_path).query).items() if v}
        tmdb_type, tmdb_id = params.get('tmdb_type'), params.get('tmdb_id')
        if not tmdb_id:
            return
        if tmdb_type == 'movie':
            info['type'] = 'movie'
            info['uniqueid'] = {'tmdb': tmdb_id}
        elif tmdb_type == 'tv' and params.get('season', '').isdigit() and params.get('episode', '').isdigit():
            info['type'] = 'episode'
            info['season'], info['episode'] = int(params['season']), int(params['episode'])
            info['tvshow'] = {'uniqueid': {'tmdb': tmdb_id}}

    @staticmethod
    def _infer_type(info):
        t = info.get('type')
        if t in ('movie', 'episode'):
            return t
        parsed = info.get('_parsed', {})
        if to_int(info.get('season'), -1) >= 0 or to_int(info.get('episode'), -1) >= 1:
            return 'episode'
        if info.get('showtitle') or info.get('firstaired') or to_int(info.get('tvshowid'), -1) > 0:
            return 'episode'
        if parsed.get('is_episode'):
            return 'episode'
        if info.get('title') and (info.get('premiered') or info.get('year') or info.get('uniqueid')
                                  or info.get('_path_ids') or info.get('_label_imdb')):
            return 'movie'
        return t

    def _path_sources(self, info):
        """Every place that may still hold the real file path, labelled for the (redacted) log."""
        playing = self._safe(self.getPlayingFile)
        found = [('item.file', info.get('file') or ''), ('player.file', playing),
                 ('label', xbmc.getInfoLabel('Player.Filenameandpath'))]
        try:
            found.append(('tag.path', self.getVideoInfoTag().getPath()))   # PM4K sets the server path here
        except Exception:
            pass
        if 'X-Plex-' in (playing or ''):
            found.append(('plex.current_path', self._window().getProperty('videoinfo.current_path')))
        out, seen = [], set()
        for label, p in found:
            if p and p not in seen:
                seen.add(p)
                out.append((label, p))
        return out

    def _fetch_info(self):
        players = self._jsonrpc('Player.GetActivePlayers')
        playerid = 1
        if isinstance(players, list):
            playerid = next((p.get('playerid') for p in players if p.get('type') == 'video'), 1)
        info = self._jsonrpc('Player.GetItem', {
            'playerid': playerid,
            'properties': ['title', 'tvshowid', 'showtitle', 'season', 'episode', 'firstaired',
                           'premiered', 'year', 'uniqueid', 'file']}).get('item')
        if not info:
            log.debug('no item info -> nothing scrobbled')
            return None
        if (info.get('file') or '').startswith('pvr://'):
            return None

        self._apply_tmdb_helper(info)
        self._merge_infolabels(info)

        sources = self._path_sources(info)
        paths = [p for _, p in sources]
        rule = ignore.path_ignored(paths)
        if rule:
            log.debug('ignored: the file is in an ignored folder ({})'.format(redact.path(rule)))
            return None
        plugin = xbmc.getInfoLabel('Container.PluginName')
        addon_id = ignore.addon_ignored(paths + [xbmc.getInfoLabel('Player.Folderpath')], plugin)
        if addon_id:
            log.debug('ignored: played from the ignored video add-on {}'.format(addon_id))
            return None
        for label, p in sources:
            log.debug('path[{}] {}'.format(label, redact.path(p)))
        info['_path_ids'], info['_parsed'] = {}, {}
        for _, p in sources:
            parsed = parsing.parse_path(p)
            for k, v in parsed['ids'].items():
                info['_path_ids'].setdefault(k, v)
            for k in ('is_episode', 'season', 'episode', 'title', 'year', 'show_title', 'show_year'):
                if not info['_parsed'].get(k) and parsed.get(k):
                    info['_parsed'][k] = parsed[k]
        info['_hint_ids'] = self._read_hint_ids()

        info['type'] = self._infer_type(info)
        if info['type'] not in ('movie', 'episode'):
            log.debug('unsupported item type, skipping: {!r}'.format(info.get('title')))
            return None
        if not cfg.get_bool('scrobble_movie' if info['type'] == 'movie' else 'scrobble_episode', True):
            return None

        if info['type'] == 'episode':
            info['tvshow'] = info.get('tvshow') or {}
            tvshowid = to_int(info.get('tvshowid'), -1)
            if tvshowid > 0:       # the item is in the Kodi library
                info['tvshow'] = self._jsonrpc('VideoLibrary.GetTVShowDetails', {
                    'tvshowid': tvshowid, 'properties': ['uniqueid', 'imdbnumber', 'year']}
                ).get('tvshowdetails') or {}

        log.debug('item type={} title={!r} show={!r} S={} E={} year={} uniqueid={} lib_show={} hint={} path_ids={}'.format(
            info['type'], info.get('title'), info.get('showtitle'), info.get('season'), info.get('episode'),
            info.get('year'), info.get('uniqueid'), (info.get('tvshow') or {}).get('uniqueid'),
            info['_hint_ids'], info['_path_ids']))
        return info

    # ------------------------------------------------------------------ identification (worker thread)
    @staticmethod
    def _searcher():
        """The enabled service that finds ids from a title + year (cached), if any."""
        return next((p for p in providers.enabled() if p.searches_by_title), None)

    @staticmethod
    def _fallback_search(kind, title, max_year):
        """Title-only lookup by a service that can match without ids; used only when no service searches
        by title and the item has no ids and no year. -> (provider, found dict) or (None, {})."""
        fb = next((p for p in providers.enabled() if p.title_only_fallback), None)
        return (fb, fb.search_ids(kind, title, None, max_year)) if fb else (None, {})

    @staticmethod
    def _find_show(search, titles, year, aired, first_episode):
        """Show ids by title; `search(title, year, max_year)` -> ids dict. The folder name is tried before
        Kodi's show title. With a known year that year is used. Without one, the first episode of season 1 airs
        when the show premieres, so it pins the show to its air year; any other episode can only rule out
        shows that premiered later."""
        for t in titles:
            found = {}
            if year:
                found = search(t, year, None)
            else:
                if first_episode and aired:
                    found = search(t, aired, None)
                if not found:
                    found = search(t, None, aired)
            if found:
                return found
        return {}

    @staticmethod
    def _native(provider, found):
        return {provider.name: found['native_id']} if provider and found.get('native_id') else {}

    @staticmethod
    def _tag_ids(info):
        return {k: v for k, v in (info.get('uniqueid') or {}).items() if str(k).lower() != 'unknown'}

    def _resolve(self, info):
        """Which item is this? First hit wins; the Kodi library is never required.

        movie  : script.trakt.ids -> item ids -> ids in the path -> title+year search
        episode: show ids from library / script.trakt.ids / path (trusted); else the item's own ids
                 verified by a title search (addons disagree on show- vs episode-level ids); else
                 the search alone. tmdb, tvdb and imdb all pass through. The title/year are kept
                 so a service that needs no ids can still match.
        """
        parsed = info.get('_parsed', {})
        searcher = self._searcher()
        can_search = searcher is not None     # title + year search (cached) whenever a service offers it
        hint_ids = info.get('_hint_ids', {})

        if info['type'] == 'movie':
            item_ids = self._tag_ids(info)
            if info.get('_label_imdb'):
                item_ids.setdefault('imdb', info['_label_imdb'])
            ids, source = {}, ''
            for source, cand in (('hint', hint_ids), ('item', item_ids), ('path', info.get('_path_ids', {}))):
                ids = parsing.normalize_ids(cand, 'movie')
                if ids:
                    break
            title = info.get('title') if parsing.usable_title(info.get('title')) else parsed.get('title')
            title, title_year = parsing.regex_year(title)
            year = to_int(info.get('year')) or title_year or parsed.get('year')
            if not ids and can_search and parsing.usable_title(title):
                ids, source = searcher.search_ids('movie', title, year), 'search'     # year may be unknown: title-only
            native = {}
            if not ids and not can_search and not year and parsing.usable_title(title):
                fb, found = self._fallback_search('movie', title, None)
                native = self._native(fb, found)
                ids = parsing.normalize_ids({'tmdb': found['tmdb']}, 'movie') if found.get('tmdb') else {}
            if not ids and not native and not (parsing.usable_title(title) and year):
                log.debug('movie {!r}: no ids and no match by title -> ignored'.format(title))
                return None
            log.debug('movie ids from {}: {}'.format(source if ids else 'none (title only)', ids))
            return {'kind': 'movie', 'ids': ids, 'title': title if parsing.usable_title(title) else None, 'year': year,
                    'native': native}

        season = to_int(info.get('season'), -1)
        episode = to_int(info.get('episode'), -1)
        if season < 0 or episode < 1:
            season, episode = parsed.get('season'), parsed.get('episode')
        if season is None or episode is None:
            log.debug('episode without season/episode number -> skipped')
            return None

        tvshow = info.get('tvshow') or {}
        native = {}
        raw_title = info.get('showtitle') if parsing.usable_title(info.get('showtitle')) else parsed.get('show_title')
        title, title_year = parsing.regex_year(raw_title)                 # 'Show (2020)'
        year = title_year or parsed.get('show_year') or to_int(tvshow.get('year'))
        usable = parsing.usable_title(title)

        ids, source = parsing.normalize_ids(tvshow.get('uniqueid', {}), 'show'), 'kodi library'
        if not ids and tvshow.get('imdbnumber'):
            ids = parsing.normalize_ids(parsing.guess_id(tvshow['imdbnumber'], 'show'), 'show')
        if not ids:
            for source, cand in (('hint', hint_ids), ('path', info.get('_path_ids', {}))):
                ids = parsing.normalize_ids(cand, 'show')
                if ids:
                    break
        if not ids:
            item_ids = parsing.normalize_ids(self._tag_ids(info), 'show')
            found = {}
            # The item's year is the episode's air year: the show cannot have premiered later.
            aired = to_int((info.get('firstaired') or info.get('premiered') or '')[:4]) or to_int(info.get('year')) or None
            titles = [title]
            folder = parsing.regex_year(parsed.get('show_title') or '')[0]
            if usable and parsing.usable_title(folder) and parsing.normalize(folder) != parsing.normalize(title):
                titles.insert(0, folder)        # 'Naked Attraction Spain' (folder) before Kodi's 'Naked Attraction'
            first_episode = season == 1 and episode == 1
            if can_search and usable:
                found = self._find_show(lambda t, y, m: searcher.search_ids('show', t, y, m), titles, year, aired, first_episode)
            if found:
                ids, source = found, 'search'
                if item_ids and not any(found.get(k) == v for k, v in item_ids.items()):
                    log.debug('item ids {} differ from the show found by title {} -> episode-level, '
                              'using the search result'.format(item_ids, found))
            elif item_ids and (can_search or not (usable and year)):
                ids, source = item_ids, 'item (unverified)'
            if not ids and not can_search and usable and not year:
                fb = next((p for p in providers.enabled() if p.title_only_fallback), None)
                sf = self._find_show(lambda t, y, m: fb.search_ids('show', t, y, m), titles, year, aired, first_episode) if fb else {}
                native = self._native(fb, sf)
                ids = parsing.normalize_ids({'tmdb': sf['tmdb']}, 'show') if sf.get('tmdb') else {}
        if not ids and not native and not (usable and year):
            log.debug('show {!r}: no ids and no match by title -> ignored'.format(title))
            return None
        log.debug('show ids from {}: {}'.format(source if ids else 'none (title only)', ids))
        ep_title = info.get('title') if parsing.usable_title(info.get('title')) else None
        return {'kind': 'episode', 'ids': ids, 'season': season, 'episode': episode,
                'title': title if usable else None, 'year': year, 'episode_title': ep_title, 'native': native}

    # ------------------------------------------------------------------ main worker
    def _run(self):
        while True:
            job = self._jobs.get()
            if job is None:
                for w in self._workers.values():
                    w.queue.put(None)
                return
            try:
                action, session, progress = job
                if action == 'prepare':
                    self._do_prepare(session)
                elif action == 'stop':
                    self._do_stop(session, progress)
                elif action == 'recover':
                    self._do_recover()
            except Exception as e:
                log.error('worker error: {!r}'.format(e))

    def _do_prepare(self, session):
        time.sleep(START_SETTLE_SECONDS)
        self._update_times(session)
        minutes = cfg.get_int('min_minutes', 15)
        if minutes > 0 and 0 < session.total_time < minutes * 60:
            log.debug('too short ({:.0f} s, minimum {} min) -> not tracked'.format(session.total_time, minutes))
            return
        session.ref = self._resolve(session.info)
        if session.ref is None:
            log.warning('could not identify {!r} -> not scrobbling'.format(
                session.info.get('showtitle') or session.info.get('title')))
            return
        log.debug('ref {}'.format(session.ref))
        with self._lock:
            still_playing = self._session is session
        if still_playing:
            self._update_times(session)
        progress = session.progress()
        for p in providers.enabled():
            if p.accepts(session.ref):
                session.providers.append(p.name)
                self._workers[p.name].queue.put(('start', session, progress))
            else:
                log.debug('{}: cannot identify this item with what is known -> skipped'.format(p.label))

    def _do_stop(self, session, progress):
        if session.ref is None or not session.providers:
            return
        for name in session.providers:
            self._workers[name].queue.put(('stop', session, progress))
        self._maybe_rate(session, progress)

    def _do_recover(self):
        entry = journal.load()
        if not entry or not entry.get('ref'):
            journal.drop()
            return
        log.debug('re-sending pending stop from a previous run for {}'.format(entry.get('pending')))
        for name in list(entry.get('pending', [])):
            p = providers.get(name)
            if p is None or not p.enabled() or not p.accepts(entry['ref']):
                journal.done(entry['session'], name)
            else:
                self._workers[name].queue.put(('recover', entry, 0))

    # ------------------------------------------------------------------ per-service workers
    def _provider_job(self, provider, action, payload, progress):
        if action == 'recover':
            return self._provider_recover(provider, payload)
        session = payload
        if action == 'start':
            log.debug('{} start progress={}'.format(provider.label, progress))
            provider.start(session.ref, progress, session.last_time, session.total_time)
        elif action == 'stop':
            self._provider_stop(provider, session, progress)

    def _provider_stop(self, provider, session, progress):
        if not provider.accepts(session.ref):
            return                       # e.g. Simkl could not identify the item at start
        log.debug('{} stop progress={}'.format(provider.label, progress))
        res = provider.stop(session.ref, progress, session.stop_position, session.total_time)
        if res.ok:
            with session.lock:
                session.done.add(provider.name)
                journal.done(session.id, provider.name)
            if progress >= provider.watched_mark():
                self._redate_rating(provider, session)
        elif progress >= provider.watched_mark():
            self._ensure_armed(session, progress)
            self._stop_fallback(provider, res, session.ref, time.time(), session.id)

    def _provider_recover(self, provider, entry):
        res = provider.stop(entry['ref'], entry.get('progress', 100.0), entry.get('position'),
                            entry.get('duration'), when=entry.get('ts'))
        if res.ok:
            journal.done(entry['session'], provider.name)
        else:
            self._stop_fallback(provider, res, entry['ref'], entry.get('ts') or time.time(), entry['session'])

    @staticmethod
    def _stop_fallback(provider, res, ref, when, session_id):
        """The stop wasn't recorded although the item was watched. Transient trouble: keep the
        pending file for the next start. A permanent refusal (e.g. expired session): mark it
        watched directly instead (one extra request, only in this rare case)."""
        if res.transient or res.auth_error or res.daily_limit:
            log.debug('{} stop failed transiently; pending stop kept for the next start'.format(provider.label))
            return
        done = provider.mark_watched(ref, when)
        if done.ok or not done.transient:
            if not done.ok:
                log.warning('{}: could not record this watch and no fallback worked; giving up on it'.format(provider.label))
            journal.done(session_id, provider.name)

    @staticmethod
    def _redate_rating(provider, session):
        """Rated before it was watched: send the rating again, dated with this watch (once)."""
        entry = pending_ratings.find(provider.name, session.ref)
        if entry is None:
            return
        res = provider.rate(pending_ratings.target(entry), entry['rating'], when=session.stop_ts or time.time())
        log.debug('{}: rating re-dated with the watch date -> {}'.format(provider.label, 'ok' if res.ok else 'failed'))
        if res.ok or not res.transient:
            pending_ratings.remove(entry)

    # ------------------------------------------------------------------ rating prompt
    def _maybe_rate(self, session, progress):
        kind = session.ref['kind']
        if not cfg.get_bool('rate_movie' if kind == 'movie' else 'rate_episode', False):
            return
        if progress < cfg.get_int('rate_min_percent', 80):
            return
        targets = [providers.get(n) for n in session.providers if providers.get(n).rating_supported(kind)]
        if not targets:
            return
        threading.Thread(target=self._rating_thread, args=(session, targets), name='tracker-rate',
                         daemon=True).start()

    def _rating_thread(self, session, targets):
        if self._abort.wait(RATING_DELAY_SECONDS):
            return
        with self._lock:
            current = self._session
        if current is not None and current.ref is not None and ref_key(current.ref) == ref_key(session.ref):
            return                       # the same item started again (stream switch / replay)
        if not self._rating_lock.acquire(blocking=False):
            return
        try:
            name = rating.display_name(session.info, session.ref)
            value = rating.prompt(name, cfg.get_int('rate_default', 6))
            if value:
                rating.submit(session.ref, value, name, targets, when=session.stop_ts)
        finally:
            self._rating_lock.release()
