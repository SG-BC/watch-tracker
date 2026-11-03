"""Sync between any two of Kodi, MDBList and Simkl (the "From" and "To" lists in the settings).

Every pair is one-way and add-only. A pair is a function in sync.py / kodisync.py; adding a service means
adding its name to NODES and its pairs to RUNNERS (readers and writers talk Simkl-shaped payloads)."""
from . import kodisync, sync

NODES = ('kodi', 'mdblist', 'simkl')
LABELS = {'kodi': 'Kodi', 'mdblist': 'MDBList', 'simkl': 'Simkl'}


def _mdblist_simkl(ui, dry):
    return sync.summary(sync.run(ui, dry=dry), dry=dry)


def _kodi_to(name):
    return lambda ui, dry: kodisync.summary_to(kodisync.to_service(ui, dry, name), dry, LABELS[name])


RUNNERS = {
    ('mdblist', 'simkl'): _mdblist_simkl,
    ('mdblist', 'kodi'): lambda ui, dry: kodisync.summary_mdblist_to_kodi(kodisync.mdblist_to_kodi(ui, dry), dry),
    ('kodi', 'simkl'): _kodi_to('simkl'),
    ('kodi', 'mdblist'): _kodi_to('mdblist'),
    ('simkl', 'kodi'): lambda ui, dry: kodisync.summary_from_simkl(kodisync.from_simkl(ui, dry), dry),
    ('simkl', 'mdblist'): lambda ui, dry: kodisync.summary_simkl_to_mdblist(kodisync.simkl_to_mdblist(ui, dry), dry),
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
    return RUNNERS[(src, dst)](ui, dry)


def reset_all():
    sync.reset_state()
    kodisync.reset_state()
