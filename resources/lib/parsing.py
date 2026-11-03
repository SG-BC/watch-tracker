"""Pure-Python helpers (no Kodi imports) so they can be unit-tested."""
import re
from urllib.parse import unquote

# {tmdb-123} [tmdbid-123] (tvdbid=123) {imdb-tt0123456} ...
ID_TAG = re.compile(
    r'[\{\[\(]\s*(imdb|tmdb|tvdb)(?:id)?\s*[-=:_ ]\s*(tt\d+|\d+)\s*[\}\]\)]', re.I)
BARE_IMDB = re.compile(r'(?<![A-Za-z0-9])(tt\d{7,10})(?![A-Za-z0-9])')
SEASON_DIR = re.compile(
    r'^(season|series|staffel|saison|temporada|specials?)[\s._-]*\d*$|^s\d{1,3}$', re.I)
SXE = re.compile(r'(?<![a-z0-9])s(\d{1,3})[\s._-]*e(\d{1,4})', re.I)
NXN = re.compile(r'(?<![a-z0-9])(\d{1,2})x(\d{2,3})(?![a-z0-9])', re.I)
EXT = re.compile(r'\.[A-Za-z0-9]{2,4}$')
PAREN_YEAR = re.compile(r'^(.*?)[\s._-]*\(((?:19|20)\d{2})\)')
SCENE_YEAR = re.compile(r'^(.+?)[\s.]+((?:19|20)\d{2})(?=[\s.]|$)')
REMOTE = re.compile(r'^(https?|plugin|rtsp|rtmp)://', re.I)


def is_remote(path):
    return bool(path and REMOTE.match(path))


def segments(path):
    """Split a Kodi path (local, smb://, nfs://, stack://, http://...) into decoded segments."""
    if not path:
        return []
    if path.startswith('stack://'):
        path = path[len('stack://'):].split(' , ')[0]
    path = unquote(path)
    path = re.sub(r'^[a-zA-Z][a-zA-Z0-9+.-]*://', '', path)
    path = re.sub(r'[?&#]', '/', path)
    return [s for s in re.split(r'[\\/]+', path) if s.strip()]


def extract_ids(path, allow_bare=True):
    """Return {'imdb': 'tt..', 'tmdb': '123', 'tvdb': '456'} found anywhere in the path.

    Every folder is scanned, not only the file name, so
    'Show (2020) {tvdb-123}/Season 1/Show - S01E01.mkv' works.
    """
    segs = segments(path)
    ids = {}
    for seg in reversed(segs):  # deepest first
        for m in ID_TAG.finditer(seg):
            prov, val = m.group(1).lower(), m.group(2).lower()
            if prov == 'imdb' and not val.startswith('tt'):
                continue
            if prov != 'imdb' and val.startswith('tt'):
                continue
            ids.setdefault(prov, val)
    if 'imdb' not in ids and allow_bare:
        for seg in reversed(segs):
            m = BARE_IMDB.search(seg)
            if m:
                ids['imdb'] = m.group(1)
                break
    return ids


def parse_episode_numbers(name):
    m = SXE.search(name) or NXN.search(name)
    if not m:
        return None
    return int(m.group(1)), int(m.group(2)), m.start()


def _clean(text):
    text = text.strip(' ._-')
    if ' ' not in text:
        text = text.replace('.', ' ').replace('_', ' ')
    return re.sub(r'\s+', ' ', text).strip(' -')


def split_title_year(name):
    n = re.sub(r'[\{\[][^\}\]]*[\}\]]', ' ', name)
    m = PAREN_YEAR.match(n)
    if m and _clean(m.group(1)):
        return _clean(m.group(1)), int(m.group(2))
    m = SCENE_YEAR.match(n)
    if m and _clean(m.group(1)):
        return _clean(m.group(1)), int(m.group(2))
    return _clean(n), None


def usable_title(title):
    return bool(title) and len(title) >= 2 and re.search(r'[^\W\d_]', title, re.U) is not None


def parse_path(path):
    """Extract everything useful from a file path."""
    out = {'ids': extract_ids(path, allow_bare=not is_remote(path)), 'is_episode': False, 'season': None, 'episode': None,
           'title': None, 'year': None, 'show_title': None, 'show_year': None}
    segs = segments(path)
    if not segs:
        return out
    fname = EXT.sub('', segs[-1])
    se = parse_episode_numbers(fname)
    if se:
        out['is_episode'] = True
        out['season'], out['episode'], start = se
    if is_remote(path):
        return out  # URL file names are meaningless (e.g. /library/parts/123/file.mkv)

    folders = segs[:-1]
    t, y = split_title_year(fname)
    if usable_title(t):
        out['title'], out['year'] = t, y
    if out['year'] is None and folders:
        _, fy = split_title_year(folders[-1])
        out['year'] = fy

    if out['is_episode']:
        show_folders = [f for f in folders if not SEASON_DIR.match(f.strip())]
        folder_t = folder_y = None
        has_hint = False
        if show_folders:
            sf = show_folders[-1]
            folder_t, folder_y = split_title_year(sf)
            has_hint = folder_y is not None or bool(ID_TAG.search(sf))
        prefix_t, prefix_y = split_title_year(fname[:se[2]]) if se[2] > 0 else (None, None)
        if usable_title(folder_t) and (has_hint or not usable_title(prefix_t)):
            out['show_title'], out['show_year'] = folder_t, folder_y
        elif usable_title(prefix_t):
            out['show_title'], out['show_year'] = prefix_t, prefix_y
    return out


def normalize(s):
    s = re.sub(r'^the\s+', '', (s or '').lower().replace('&', ' and '))
    return re.sub(r'[\W_]+', '', s, flags=re.U)


def pick_best(results, title, year=None, max_year=None):
    """Pick the search hit whose title matches exactly.

    year      -> hit must be within +-1 year of it
    max_year  -> (episodes of an unknown-year show) hit must not premiere after this year
    """
    target = normalize(title)
    if not target:
        return None
    cands = []
    for i, r in enumerate(results or []):
        if normalize(r.get('title')) != target:
            continue
        diff = 0
        ry = r.get('year')
        if year and ry:
            try:
                diff = abs(int(ry) - int(year))
            except (TypeError, ValueError):
                diff = 0
            if diff > 1:
                continue
        elif max_year and ry:
            try:
                if int(ry) > int(max_year):
                    continue
            except (TypeError, ValueError):
                pass
        cands.append((diff, i, r))
    if not cands:
        return None
    if year:
        cands.sort(key=lambda c: (c[0], c[1]))
        return cands[0][2]
    # No year to go on (title-only search): take the best-scored exact match, but refuse when two
    # different titles score almost the same -- a wrong match is worse than no match.
    def score(c):
        try:
            return float(c[2].get('score') or 0)
        except (TypeError, ValueError):
            return 0.0
    cands.sort(key=lambda c: (-score(c), c[1]))
    if len(cands) > 1 and score(cands[1]) >= score(cands[0]) * 0.9 and cands[1][2].get('year') != cands[0][2].get('year'):
        return None
    return cands[0][2]


_SEASON_EPISODE_WORDS = re.compile(r'(.*?)[\s._-]*season[\s._-]*(\d+)[\s._-]*-?[\s._-]*episode[\s._-]*(\d+)', re.I)
_TRAILING_YEAR = re.compile(r'^(.+?)\s*\((\d{4})\)\s*$')


def regex_year(label):
    """'Show (2020)' -> ('Show', 2020); no trailing year -> (label, None). (script.trakt: regex_year)"""
    m = _TRAILING_YEAR.match(label or '')
    if m:
        return m.group(1).strip(), int(m.group(2))
    return (label or '').strip(), None


def regex_tvshow(label):
    """Find season/episode in a label such as 'Show.S01E09', 'Show 1x09' or
    'Show Season 1 Episode 9'. Returns (show_title, season, episode) or ('', -1, -1).
    (script.trakt: regex_tvshow, without its ambiguous 'Show.109' pattern that would hit movie titles.)"""
    label = label or ''
    m = SXE.search(label) or NXN.search(label)
    if m:
        prefix, season, episode = label[:m.start()], int(m.group(1)), int(m.group(2))
    else:
        m = _SEASON_EPISODE_WORDS.search(label)
        if not m:
            return '', -1, -1
        prefix, season, episode = m.group(1), int(m.group(2)), int(m.group(3))
    return _clean(re.sub(r'[\[\]_().]', ' ', prefix)), season, episode


def guess_id(value, kind):
    """Kodi's single 'imdbnumber' field: 'tt...' -> imdb, digits -> tmdb (movie) / tvdb (show)."""
    value = str(value or '').strip()
    if value.startswith('tt'):
        return {'imdb': value}
    if value.isdigit() and value != '0':
        return {('tmdb' if kind == 'movie' else 'tvdb'): value}
    return {}


# ----------------------------------------------------------------------------- IDs for the API
_ALLOWED = {
    'movie': ('imdb', 'tmdb', 'trakt', 'mdblist'),
    'show': ('imdb', 'tmdb', 'tvdb', 'trakt', 'mdblist'),
}
_ALIASES = {
    'imdb': ('imdb', 'imdb_id', 'imdbid', 'imdbnumber'),
    'tmdb': ('tmdb', 'themoviedb', 'tmdb_id', 'tmdbid'),
    'tvdb': ('tvdb', 'thetvdb', 'tvdb_id', 'tvdbid'),
    'trakt': ('trakt', 'trakt_id', 'traktid'),
    'mdblist': ('mdblist', 'mdblist_id'),
}


def _num(value):
    """'1399/1/2' (legacy Plex tmdb agent guid), 1399, '1399' -> 1399; anything else -> None."""
    value = re.split(r'[/?]', str(value or '').strip())[0]
    return int(value) if value.isdigit() and int(value) > 0 else None


def normalize_ids(raw, kind):
    """Clean an ids dict from any source into what MDBList accepts.

    kind 'movie' or 'show'. imdb -> 'tt..' string, tmdb/tvdb/trakt -> int, mdblist -> string.
    Unknown keys (Kodi's opaque 'unknown', slugs, ...) are dropped.
    """
    raw = {str(k).lower(): v for k, v in (raw or {}).items()}
    out = {}
    for prov in _ALLOWED['movie' if kind == 'movie' else 'show']:
        for name in _ALIASES[prov]:
            v = raw.get(name)
            if v in (None, '', 0, '0'):
                continue
            if prov == 'imdb':
                v = str(v).strip().lower()
                if re.fullmatch(r'tt\d+', v):
                    out[prov] = v
            elif prov == 'mdblist':
                v = str(v).strip()
                if re.fullmatch(r'[A-Za-z0-9]+', v):
                    out[prov] = v
            else:
                n = _num(v)
                if n:
                    out[prov] = n
            if prov in out:
                break
    return out


def ids_from_search_item(item):
    """Read ids out of one /search result. The schema leaves result fields unspecified, so accept
    the likely spellings: flat (tmdbid, tmdb_id, tmdb) and nested ({'ids': {...}})."""
    flat = dict(item or {})
    nested = flat.get('ids')
    if isinstance(nested, dict):
        for k, v in nested.items():
            flat.setdefault(k, v)
    return flat
