"""Settings-button dialogs for the ignore lists."""
import xbmcgui

from . import ignore, ui


def _add(paths):
    data = ignore.load()
    added = [p for p in paths if p and p not in data['paths']]
    if added:
        data['paths'].extend(added)
        ignore.save(data)
        ui.notify('Ignoring {} folder(s)'.format(len(added)))


def manage_folders():
    dialog = xbmcgui.Dialog()
    while True:
        entries = ignore.load()['paths']
        rows = ['[+] Add from my media sources', '[+] Browse for a folder', '[+] Type a folder name'] + [
            'Remove: ' + ignore.strip_credentials(e) for e in entries]
        choice = dialog.select('Ignored folders ({})'.format(len(entries)), rows)
        if choice is None or choice < 0:
            return
        if choice == 0:
            sources = ignore.media_sources()
            if not sources:
                dialog.ok('Ignored folders', 'No video sources found. Use "Browse for a folder" instead.')
                continue
            picked = dialog.multiselect('Folders to ignore', ['{}  -  {}'.format(
                label, ignore.strip_credentials(path)) for label, path in sources])
            if picked:
                _add([sources[i][1] for i in picked])
        elif choice == 1:
            _add([dialog.browse(0, 'Folder to ignore', 'video')])
        elif choice == 2:
            _add([(dialog.input('Folder name or path') or '').strip()])
        else:
            entry = entries[choice - 3]
            if dialog.yesno('Ignored folders', 'Stop ignoring this folder?\n{}'.format(ignore.strip_credentials(entry))):
                data = ignore.load()
                data['paths'] = [e for e in data['paths'] if e != entry]
                ignore.save(data)


def manage_addons():
    dialog = xbmcgui.Dialog()
    addons = ignore.video_addons()
    if not addons:
        dialog.ok('Ignored add-ons', 'No video add-ons are installed.')
        return
    data = ignore.load()
    preselect = [i for i, (addon_id, _) in enumerate(addons) if addon_id in data['addons']]
    picked = dialog.multiselect('Video add-ons to ignore', ['{}  ({})'.format(name, addon_id)
                                                          for addon_id, name in addons], preselect=preselect)
    if picked is None:
        return
    listed = {addon_id for addon_id, _ in addons}
    data['addons'] = [addons[i][0] for i in picked] + [a for a in data['addons'] if a not in listed]
    ignore.save(data)
    ui.notify('Ignoring {} video add-on(s)'.format(len(picked)))
