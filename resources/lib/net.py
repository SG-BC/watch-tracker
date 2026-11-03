"""One HTTP helper for every service. Never logs URLs or headers (they carry keys and tokens)."""
import requests

TIMEOUT = 10


class Result:
    def __init__(self, status=None, body=None, network_error=False, auth_error=False,
                 retry_after=None, daily_limit=False):
        self.status = status
        self.raw = body                                   # the parsed answer as it came (may be a list)
        self.body = body if isinstance(body, dict) else {}
        self.network_error = network_error
        self.auth_error = auth_error
        self.retry_after = retry_after
        self.daily_limit = daily_limit
        self.note = ''
        self.headers = {}                                 # lower-case names; only filled by request()

    @property
    def ok(self):
        return self.status is not None and self.status < 400

    @property
    def unmatched(self):
        return self.status == 404

    @property
    def transient(self):
        """Worth retrying later: network trouble, 5xx, or a short 429 (not the daily limit)."""
        if self.network_error or (self.status is not None and self.status >= 500):
            return True
        return self.status == 429 and not self.daily_limit and (self.retry_after or 1) <= 10


def request(method, url, params=None, json_data=None, data=None, headers=None, timeout=TIMEOUT):
    try:
        r = requests.request(method, url, params=params, json=json_data, data=data,
                             headers=headers, timeout=timeout)
    except requests.exceptions.RequestException:
        return Result(network_error=True)
    try:
        body = r.json()
    except ValueError:
        body = {}
    res = Result(status=r.status_code, body=body)
    res.headers = {str(k).lower(): v for k, v in (r.headers or {}).items()}
    if r.status_code in (401, 403):
        res.auth_error = True
    elif r.status_code == 429:
        try:
            res.retry_after = int(r.headers.get('Retry-After', 5))
        except ValueError:
            res.retry_after = 5
        res.daily_limit = 'daily' in str(res.body.get('error', '')).lower()
    return res


def summarize(res):
    """Short, useful, secret-free description of a response for the debug log."""
    if res.network_error:
        return 'network error'
    b = res.body
    bits = [str(res.status)]
    for key in ('action', 'result', 'error', 'message'):
        if b.get(key):
            bits.append('{}={}'.format(key, str(b[key])[:80]))
    if b.get('progress') is not None:
        bits.append('progress={}'.format(b['progress']))
    for key in ('movie', 'show', 'anime', 'media'):
        item = b.get(key)
        if isinstance(item, dict):
            bits.append('{}="{}" ({}) ids={}'.format(key, item.get('title'), item.get('year'), item.get('ids')))
    for key in ('added', 'updated', 'not_found'):
        if isinstance(b.get(key), dict):
            bits.append('{}={}'.format(key, {k: (len(v) if isinstance(v, list) else v) for k, v in b[key].items()
                                              if k != 'statuses'}))
    if res.note:
        bits.append(res.note)
    return ' '.join(bits)
