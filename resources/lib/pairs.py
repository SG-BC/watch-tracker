"""Sync between any two of Kodi, MDBList, Simkl, WeTrakr and AniList (the "From" and "To" lists in the settings).

Every pair is one-way and add-only. A pair is a function in sync.py / kodisync.py; adding a service means
adding its name to NODES and its pairs to RUNNERS (readers and writers talk Simkl-shaped payloads)."""
from . import kodisync, log, plexsync, sync

NODES = ('kodi', 'mdblist', 'simkl', 'wetrakr', 'anilist', 'plex')
LABELS = {'kodi': 'Kodi', 'mdblist': 'MDBList', 'simkl': 'Simkl', 'wetrakr': 'WeTrakr', 'anilist': 'AniList', 'plex': 'Plex'}


def _mdblist_simkl(ui, dry):
    return sync.summary(sync.run(ui, dry=dry), dry=dry)


def _kodi_to(name):
    return lambda ui, dry: kodisync.summary_to(kodisync.to_service(ui, dry, name), dry, LABELS[name])


def _simkl_to(name):
    return lambda ui, dry: kodisync.summary_to_service(kodisync.simkl_to_service(ui, dry, name), dry, 'Simkl', LABELS[name])


def _mdblist_to(name):
    return lambda ui, dry: kodisync.summary_to_service(kodisync.mdblist_to_service(ui, dry, name), dry, 'MDBList', LABELS[name])


def _to_plex(src):
    return lambda ui, dry: plexsync.summary_to_plex(plexsync.to_plex(ui, dry, src), dry)


def _from_plex(dst):
    return lambda ui, dry: plexsync.summary_from_plex(plexsync.from_plex(ui, dry, dst), dry)


def _from(src, dst):
    return lambda ui, dry: kodisync.summary_from_service(kodisync.from_service(ui, dry, src, dst), dry, LABELS[src], LABELS[dst])


RUNNERS = {
    ('mdblist', 'simkl'): _mdblist_simkl,
    ('mdblist', 'kodi'): lambda ui, dry: kodisync.summary_mdblist_to_kodi(kodisync.mdblist_to_kodi(ui, dry), dry),
    ('kodi', 'simkl'): _kodi_to('simkl'),
    ('kodi', 'mdblist'): _kodi_to('mdblist'),
    ('simkl', 'kodi'): lambda ui, dry: kodisync.summary_from_simkl(kodisync.from_simkl(ui, dry), dry),
    ('simkl', 'mdblist'): lambda ui, dry: kodisync.summary_simkl_to_mdblist(kodisync.simkl_to_mdblist(ui, dry), dry),
    # WeTrakr and AniList: written to from Kodi, Simkl and MDBList; read from into everything that can take it
    ('kodi', 'wetrakr'): _kodi_to('wetrakr'),
    ('kodi', 'anilist'): _kodi_to('anilist'),
    ('simkl', 'wetrakr'): _simkl_to('wetrakr'),
    ('simkl', 'anilist'): _simkl_to('anilist'),
    ('mdblist', 'wetrakr'): _mdblist_to('wetrakr'),
    ('mdblist', 'anilist'): _mdblist_to('anilist'),
    ('wetrakr', 'kodi'): _from('wetrakr', 'kodi'),
    ('wetrakr', 'simkl'): _from('wetrakr', 'simkl'),
    ('wetrakr', 'mdblist'): _from('wetrakr', 'mdblist'),
    ('wetrakr', 'anilist'): _from('wetrakr', 'anilist'),
    ('anilist', 'kodi'): _from('anilist', 'kodi'),
    ('anilist', 'simkl'): _from('anilist', 'simkl'),
    ('anilist', 'wetrakr'): _from('anilist', 'wetrakr'),       # AniList -> MDBList is not offered: MDBList has no anime ids
    # Plex (the chosen servers and libraries): MDBList, Simkl and WeTrakr, both ways. Not AniList (Plex items carry
    # no AniList / MAL ids) and not Kodi (two libraries, not a tracking service).
    ('mdblist', 'plex'): _to_plex('mdblist'),
    ('simkl', 'plex'): _to_plex('simkl'),
    ('wetrakr', 'plex'): _to_plex('wetrakr'),
    ('plex', 'mdblist'): _from_plex('mdblist'),
    ('plex', 'simkl'): _from_plex('simkl'),
    ('plex', 'wetrakr'): _from_plex('wetrakr'),
}


def title(src, dst, dry=False):
    return '{} to {}{}'.format(LABELS[src], LABELS[dst], ' (preview)' if dry else '')


def check(src, dst):
    """None when the pair can run, else a short reason."""
    if src == dst:
        return 'Choose two different places: "From" and "To" are both {}.'.format(LABELS[src])
    if (src, dst) not in RUNNERS:
        return 'Syncing {} to {} is not available.'.format(LABELS[src], LABELS[dst])
    return None


def run(src, dst, ui, dry=False):
    """-> the summary text. Raises sync.SyncError / sync.Cancelled."""
    reason = check(src, dst)
    if reason:
        raise sync.SyncError(reason)
    text = RUNNERS[(src, dst)](ui, dry)
    log.debug('{} summary | {}'.format(title(src, dst, dry), text.replace('\n', ' | ')))
    return text


def reset_all():
    sync.reset_state()
    kodisync.reset_state()
    plexsync.reset_state()
