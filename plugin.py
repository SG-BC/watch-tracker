"""Widget / video-add-on entry: lists the cached watchlist. No network calls here (the service keeps the cache fresh)."""
import sys
import urllib.parse

import xbmcgui
import xbmcplugin

from resources.lib import watchlist

BASE = 'plugin://service.watch.tracker/'
KINDS = {'movie': ('Your Next Movies', 'movies'), 'show': ('Your Next Shows', 'tvshows')}


def _url(**params):
    return BASE + '?' + urllib.parse.urlencode(params)


def _info(li, item, row, plot):
    mediatype = 'movie' if item['kind'] == 'movie' else 'tvshow'
    try:
        tag = li.getVideoInfoTag()
        tag.setTitle(item.get('title') or '')
        tag.setMediaType(mediatype)
        if item.get('year'):
            tag.setYear(int(item['year']))
        tag.setPlot(plot)
        if row:
            tag.setDbId(row.get('movieid') or row.get('tvshowid'))
    except AttributeError:                      # Kodi 19
        li.setInfo('video', {'title': item.get('title') or '', 'mediatype': mediatype, 'plot': plot,
                             'year': int(item['year']) if item.get('year') else 0})


def root(handle):
    for kind, (label, _) in KINDS.items():
        xbmcplugin.addDirectoryItem(handle, _url(action='list', kind=kind), xbmcgui.ListItem(label, offscreen=True), True)
    xbmcplugin.endOfDirectory(handle)


def listing(handle, kind):
    label, content = KINDS.get(kind, KINDS['movie'])
    xbmcplugin.setContent(handle, content)
    for item, row in watchlist.with_library(watchlist.merged(kind)):
        li = xbmcgui.ListItem(item.get('title') or '?', offscreen=True)
        sources = ', '.join(s.capitalize() for s in item.get('sources') or [])
        if row:
            art = dict(row.get('art') or {})
            li.setArt(art)
            _info(li, item, row, row.get('plot') or 'On your {} watchlist.'.format(sources))
            if kind == 'movie':
                li.setProperty('IsPlayable', 'true')
                xbmcplugin.addDirectoryItem(handle, row['file'], li, False)
            else:
                xbmcplugin.addDirectoryItem(handle, 'videodb://tvshows/titles/{}/'.format(row['tvshowid']), li, True)
        else:
            if item.get('poster'):
                li.setArt({'poster': item['poster'], 'thumb': item['poster']})
            _info(li, item, None, 'On your {} watchlist. Not in your Kodi library, so it cannot be played.'.format(sources))
            xbmcplugin.addDirectoryItem(handle, _url(action='info', title=item.get('title') or ''), li, False)
    xbmcplugin.endOfDirectory(handle)


def main():
    handle = int(sys.argv[1])
    params = dict(urllib.parse.parse_qsl(sys.argv[2][1:])) if len(sys.argv) > 2 else {}
    action = params.get('action')
    if action == 'list':
        listing(handle, params.get('kind', 'movie'))
    elif action == 'info':
        xbmcgui.Dialog().ok(params.get('title', 'Watchlist'), 'This title is on your watchlist, but it is not in your '
                                                              'Kodi library, so it cannot be played from here.')
    else:
        root(handle)


if __name__ == '__main__':
    main()
