"""Settings button "Choose Plex servers and libraries...": pick the servers, then the movie / show libraries of each.

Nothing is synced or edited on Plex until something is chosen. Every server line shows what matters to judge it
(owned or shared, local / remote / relay, the answer time); the warning comes first. The choice is saved in the
profile (plex_libraries.json: server id + library key, no tokens)."""
import xbmcgui

from . import log, plexapi, ui


def choose():
    dialog = xbmcgui.Dialog()
    if not plexapi.logged_in():
        dialog.ok('Plex', 'Log in to Plex first (Accounts > Plex > Log in).')
        return
    dialog.ok('Plex servers and libraries', plexapi.WARNING)
    progress = xbmcgui.DialogProgress()
    progress.create('Plex', 'Finding your servers...')
    try:
        found = plexapi.servers()
        if found is None:
            progress.close()
            dialog.ok('Plex', 'plex.tv did not answer. Check the connection and try again.')
            return
        servers = []
        for n, info in enumerate(found):
            progress.update(int(100 * n / max(1, len(found))), 'Checking {}...'.format(info['name']))
            server = plexapi.Server(info)
            server.connect()
            servers.append(server)
    finally:
        progress.close()
    if not servers:
        dialog.ok('Plex', 'No Plex Media Server was found in your account.')
        return
    saved = {s['id']: s for s in plexapi.chosen()['servers']}
    picked = dialog.multiselect('Plex servers to sync', [s.describe() for s in servers],
                                preselect=[i for i, s in enumerate(servers) if s.id in saved])
    if picked is None:
        return
    choice = []
    for i in picked:
        server = servers[i]
        if not server.uri:
            ui.notify('{}: no answer, skipped'.format(server.name), error=True)
            continue
        try:
            sections = server.sections()
        except plexapi.ServerDown as e:
            log.debug('plex sections: {}'.format(e))
            ui.notify('{}: could not list its libraries'.format(server.name), error=True)
            continue
        if not sections:
            ui.notify('{}: no movie or show libraries'.format(server.name))
            continue
        before = {lib['key'] for lib in (saved.get(server.id) or {}).get('libraries', [])}
        labels = ['{} ({})'.format(sec['title'], 'movies' if sec['type'] == 'movie' else 'shows') for sec in sections]
        libs = dialog.multiselect('{}: libraries to sync'.format(server.name), labels,
                                  preselect=[j for j, sec in enumerate(sections) if sec['key'] in before])
        if libs is None:
            return                                      # cancelled: keep the previous choice
        if libs:
            choice.append({'id': server.id, 'name': server.name, 'libraries': [sections[j] for j in libs]})
    plexapi.save_choice(choice)
    count = sum(len(c['libraries']) for c in choice)
    ui.notify('Plex: {} librar{} on {} server{} chosen'.format(count, 'y' if count == 1 else 'ies',
                                                             len(choice), '' if len(choice) == 1 else 's'))
