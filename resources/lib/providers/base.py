from .. import auth, log, net, status, ui


class Provider:
    """A tracking service. All methods are called from that service's own worker thread.

    A new service = a subclass with `name`, `label` and the methods below, one line in providers/__init__.py,
    one entry in services_config.py / auth.ENDPOINTS, and a block in the settings."""
    name = ''
    label = ''
    searches_by_title = False       # can look up ids from title (+ year); used before anything else
    title_only_fallback = False     # can match by title alone when the item has no ids and no year
    batch_size = 1000               # events per sync call (slow services use less, so a sync stays cancellable)

    # ------------------------------------------------------------------ account
    @property
    def account(self):
        return auth.get(self.name)

    def logged_in(self):
        return self.account.is_logged_in()

    def enabled(self):
        return False

    def username_from(self, res):
        """Pull the user name out of the answer of `fetch_username`."""
        return ''

    def fetch_username(self):
        """Who is signed in? One request; '' when unknown."""
        return ''

    def check(self):
        """Update the Connected as @username / Disconnected status (see status.py)."""
        raise NotImplementedError

    # ------------------------------------------------------------------ http with a Bearer token
    def _call_authed(self, method, endpoint, params=None, json_data=None, _retry=True):
        token = self.account.access_token()
        if not token:
            ui.notify_once(self.name + '-expired', '{} login expired: log in again in the add-on settings'.format(self.label))
            return net.Result(auth_error=True)
        query = self.account.common_params()
        query.update(params or {})
        headers = {'User-Agent': auth.user_agent(), 'Authorization': 'Bearer ' + token,
                   'Content-Type': 'application/json'}
        headers.update(self.account.extra_headers())
        self._before_call()
        res = net.request(method, self.account.BASE + endpoint, params=query, json_data=json_data, headers=headers)
        log.debug('{} {} {} -> {}'.format(self.name, method, endpoint, net.summarize(res)))
        status.report(self.name, res)
        if res.status == 401 and _retry:
            newer = self.account.on_unauthorized(token)
            if newer:
                return self._call_authed(method, endpoint, params, json_data, _retry=False)
            ui.notify_once(self.name + '-expired', '{} login expired: log in again in the add-on settings'.format(self.label))
        elif res.daily_limit:
            ui.notify_once(self.name + '-daily', '{} daily API limit reached'.format(self.label))
        elif res.status == 429:
            ui.notify_once(self.name + '-limit', '{} request limit reached'.format(self.label))
        elif res.status and res.status >= 400 and res.status != 404:
            log.warning('{} {} {} -> HTTP {}'.format(self.name, method, endpoint, net.summarize(res)))
        return res

    def _before_call(self):
        """Hook: pacing etc."""

    # ------------------------------------------------------------------ identification
    def accepts(self, ref):
        """Can this service identify the item with what we know?"""
        return False

    def search_ids(self, kind, title, year=None, max_year=None):
        """Title lookup -> ids dict ({} when unknown). Only for services with `searches_by_title`
        or `title_only_fallback` (the latter may also return a 'native_id' of its own)."""
        return {}

    # ------------------------------------------------------------------ scrobbling
    def watched_mark(self):
        """Progress (%) at which a stop counts as watched on this service."""
        return 80

    def start(self, ref, progress, position=None, duration=None):
        """progress in %, position/duration in seconds (may be unknown)."""
        raise NotImplementedError

    def stop(self, ref, progress, position=None, duration=None, when=None):
        """`when` (epoch seconds) is set only when re-sending a stop from a previous run."""
        raise NotImplementedError

    def mark_watched(self, ref, when):
        """Fallback when a stop can't be recorded although the item was watched."""
        raise NotImplementedError

    # ------------------------------------------------------------------ ratings
    def rating_supported(self, kind):
        return False

    def rate(self, ref, value, when=None):
        """ref kind: movie | episode | show. `when` (epoch seconds) dates the rating (default: now)."""
        raise NotImplementedError

    # ------------------------------------------------------------------ sync (neutral Simkl-shaped payloads)
    def add_history(self, payload):
        raise NotImplementedError

    def add_ratings(self, payload):
        raise NotImplementedError

    def remove_history(self, payload):
        """Un-watch (every play of) the items in a neutral history payload. Used by the manual editor."""
        raise NotImplementedError

    def remove_ratings(self, payload):
        """Remove the ratings of the items in a neutral payload (the rating value is ignored)."""
        raise NotImplementedError

    # ------------------------------------------------------------------ watchlist widget
    def watchlist(self):
        """-> list of {'kind': movie | show, 'title', 'year', 'ids', 'poster'} ("plan to watch"), or None when
        the service could not be read. Called by the service from time to time, never by the widget itself."""
        return None


def ref_key(ref):
    return (ref['kind'], tuple(sorted((ref.get('ids') or {}).items())), ref.get('season'), ref.get('episode'),
            ref.get('title') if not ref.get('ids') else None)
