#!/usr/bin/env python3
"""ym_core - Yandex Music API + raw MPD client.

Two independent halves:
  * the service layer (search / links / likes / playlists / wave / artists) -
    this is the ONLY part to rewrite for another streaming service;
  * the MPD client (class MPD) - service-agnostic, keep as is.

Manual use:
  python3 ym_core.py search "beatles"
  python3 ym_core.py link <track_id>
  python3 ym_core.py play <track_id>
  python3 ym_core.py status
  python3 ym_core.py outputs

MPD protocol notes (0.23/0.24):
  * greeting is "OK MPD <version>";
  * a reply ends with "OK" (success) or "ACK [n@m] {cmd} text" (failure)  <-- not "ERROR";
  * there is no 'url' command: streams are queued with 'addid "https://..."',
    which answers "Id: <songid>", and started with 'playid <songid>';
  * over a UNIX socket MPD also accepts absolute local paths in 'addid',
    over TCP it refuses them ("Access to local files via TCP is not allowed").
"""
import json
import os
import socket
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, 'pylibs'))  # vendored yandex_music, optional

import requests
from yandex_music import Client

TOKEN_FILE = os.path.join(_HERE, 'token')      # refresh token, chmod 600
CLIENT_ID = '23cabbbdc6cd418abb4b39c32c41195d'  # Yandex Music device-flow app
CLIENT_SECRET = '53bc75238f0c4d08a118e51fe9203300'
MPD_HOST = '127.0.0.1'
MPD_PORT = 6600
MPD_SOCKET = '/run/mpd/socket'

_state = {'token': None, 'client': None}


class MPDError(IOError):
    """MPD protocol or connection error. Never carries a signed URL."""


# ---------------------------------------------------------------- service layer

def get_access_token():
    """Trade the stored refresh token for an access token (and rotate it)."""
    if _state['token']:
        return _state['token']
    rt = open(TOKEN_FILE).read().strip()
    r = requests.post('https://oauth.yandex.ru/token', data={
        'grant_type': 'refresh_token',
        'refresh_token': rt,
        'client_id': CLIENT_ID,
        'client_secret': CLIENT_SECRET,
        'service': 'music',
    }, timeout=30)
    r.raise_for_status()
    d = r.json()
    new_rt = d.get('refresh_token')
    if new_rt and new_rt != rt:
        with open(TOKEN_FILE, 'w') as f:
            f.write(new_rt)
        os.chmod(TOKEN_FILE, 0o600)
    _state['token'] = d['access_token']
    return _state['token']


def get_client():
    if _state['client'] is None:
        # .init() fetches the account info; without it playlist calls fail
        # with 'playlistIdBindingError: No uid for playlistId found'.
        _state['client'] = Client(token=get_access_token()).init()
    return _state['client']


def reset_client():
    """Call after an auth error, then retry the request once."""
    _state['token'] = None
    _state['client'] = None


def _cover(uri, size='200x200'):
    return ('https://' + uri.replace('%%', size)) if uri else ''


def _track_dict(t):
    alb = (t.albums or [None])[0] if getattr(t, 'albums', None) else None
    cover = getattr(t, 'cover_uri', None) or (getattr(alb, 'cover_uri', None) if alb else None)
    return {
        'id': str(t.id),
        'title': t.title or '',
        'artist': ', '.join(a.name for a in (t.artists or []) if a.name),
        'artists': [{'id': str(a.id), 'name': a.name} for a in (t.artists or []) if a.id and a.name],
        'album': (alb.title if alb else '') or '',
        'album_id': str(alb.id) if alb and alb.id else '',
        'duration': round((t.duration_ms or 0) / 1000),
        'cover': _cover(cover),
        'available': bool(getattr(t, 'available', True)),
    }


def _fetch(ids):
    """Full track objects for a list of ids (the API takes 100 at a time)."""
    out = []
    for i in range(0, len(ids), 100):
        out.extend(get_client().tracks([str(x) for x in ids[i:i + 100]]) or [])
    return [_track_dict(t) for t in out if t is not None]


def search(query, limit=30):
    res = get_client().search(query, type_='all')
    tracks = []
    if res is not None:
        best = getattr(res, 'best', None)
        if best is not None and best.type == 'track' and best.result is not None:
            tracks.append(best.result)
        if getattr(res, 'tracks', None) is not None:
            tracks.extend(res.tracks.results or [])
    seen, out = set(), []
    for t in tracks:
        if t.id in seen:
            continue
        seen.add(t.id)
        out.append(_track_dict(t))
    return out[:limit]


def get_link_info(track_id):
    """(signed url, 'MP3 320') - the best quality this account is given.

    Signed links expire: fetch one right before playing, never cache it.
    """
    infos = get_client().tracks_download_info(str(track_id), get_direct_links=True)
    cands = [i for i in (infos or []) if getattr(i, 'direct_link', None) and not i.preview] or \
            [i for i in (infos or []) if getattr(i, 'direct_link', None)]
    mp3 = [i for i in cands if i.codec == 'mp3'] or cands
    if not mp3:
        return None, ''
    best = max(mp3, key=lambda i: i.bitrate_in_kbps or 0)
    return best.direct_link, '%s %s' % ((best.codec or '').upper(), best.bitrate_in_kbps or '')


def get_direct_link(track_id):
    return get_link_info(track_id)[0]


def liked_ids():
    likes = get_client().users_likes_tracks()
    return [str(t.id) for t in (getattr(likes, 'tracks', None) or [])]


def liked_tracks(offset=0, limit=100):
    ids = liked_ids()
    return {'total': len(ids), 'tracks': _fetch(ids[offset:offset + limit])}


def like(track_id, on=True):
    c = get_client()
    (c.users_likes_tracks_add if on else c.users_likes_tracks_remove)(str(track_id))


def playlists():
    out = []
    for p in (get_client().users_playlists_list() or []):
        c = getattr(p, 'cover', None)
        cov = None
        if c is not None:
            cov = getattr(c, 'uri', None) or ((getattr(c, 'items_uri', None) or [None])[0])
        cov = cov or getattr(p, 'og_image', None)
        out.append({'kind': p.kind, 'name': p.title, 'count': p.track_count or 0,
                    'cover': _cover(cov, '400x400')})
    return out


def playlist_tracks(kind, offset=0, limit=100):
    pl = get_client().users_playlists(int(kind))
    if isinstance(pl, list):
        pl = pl[0] if pl else None
    shorts = (getattr(pl, 'tracks', None) or []) if pl else []
    ids = [str(s.track_id).split(':')[0] if getattr(s, 'track_id', None) else str(s.id)
           for s in shorts]
    return {'total': len(ids), 'tracks': _fetch(ids[offset:offset + limit])}


def wave_tracks(queue_after=None):
    """Next batch of 'My Wave' (endless personal radio)."""
    res = get_client().rotor_station_tracks('user:onyourwave', queue=queue_after)
    seq = getattr(res, 'sequence', None) or []
    return [_track_dict(s.track) for s in seq if getattr(s, 'track', None)]


def artist_page(artist_id):
    c = get_client()
    bi = c.artists_brief_info(str(artist_id))
    a = bi.artist
    cov = (getattr(a.cover, 'uri', None) if a.cover else None) or getattr(a, 'og_image', None)
    try:
        albums = c.artists_direct_albums(str(artist_id), page_size=100).albums or []
    except Exception:
        albums = bi.albums or []
    albums = sorted(albums, key=lambda x: x.year or 0, reverse=True)
    return {
        'artist': {'id': str(a.id), 'name': a.name, 'cover': _cover(cov, '400x400')},
        'tracks': [_track_dict(t) for t in (bi.popular_tracks or [])],
        'albums': [{'id': str(x.id), 'title': x.title, 'year': x.year or '',
                    'count': x.track_count or 0, 'type': x.type or '',
                    'cover': _cover(x.cover_uri, '400x400')} for x in albums],
    }


def album_page(album_id):
    al = get_client().albums_with_tracks(str(album_id))
    return {
        'album': {'id': str(al.id), 'title': al.title, 'year': al.year or '',
                  'artist': ', '.join(a.name for a in (al.artists or [])),
                  'artists': [{'id': str(a.id), 'name': a.name} for a in (al.artists or [])],
                  'cover': _cover(al.cover_uri, '400x400')},
        'tracks': [_track_dict(t) for vol in (al.volumes or []) for t in vol],
    }


# ---------------------------------------------------------------- MPD client

class MPD:
    """Raw MPD protocol client. Prefers the UNIX socket (local files allowed)."""

    def __init__(self, host=MPD_HOST, port=MPD_PORT, timeout=20):
        self.last_cmd = ''
        self.local = False
        try:
            if os.path.exists(MPD_SOCKET):
                try:
                    self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                    self.sock.settimeout(timeout)
                    self.sock.connect(MPD_SOCKET)
                    self.local = True
                except OSError:
                    self.sock = socket.create_connection((host, port), timeout=timeout)
            else:
                self.sock = socket.create_connection((host, port), timeout=timeout)
        except OSError as e:
            raise MPDError('cannot connect to MPD: %s' % e)
        self.buf = b''
        try:
            greeting = self._line()
            if not greeting.startswith('OK MPD'):
                raise MPDError('unexpected MPD greeting: %r' % greeting[:60])
        except MPDError:
            self.close()
            raise
        except (OSError, socket.timeout) as e:
            self.close()
            raise MPDError('MPD greeting failed: %s' % e)

    def close(self):
        try:
            self.sock.close()
        except Exception:
            pass

    def _line(self):
        while b'\n' not in self.buf:
            data = self.sock.recv(4096)
            if not data:
                raise MPDError('mpd connection closed during: %s' % self.last_cmd)
            self.buf += data
        line, self.buf = self.buf.split(b'\n', 1)
        return line.decode('utf-8', 'replace').rstrip('\r')

    def _remember(self, text):
        # never keep a command body that may contain a signed URL
        self.last_cmd = text.split(' ', 1)[0] if text.startswith('addid') else text[:60]

    def cmd(self, text):
        self._remember(text)
        try:
            self.sock.sendall((text + '\r\n').encode('utf-8'))
            out = []
            while True:
                line = self._line()
                if line == 'OK':
                    return out
                if line.startswith('ACK'):
                    raise MPDError(line)
                out.append(line)
        except socket.timeout:
            raise MPDError('mpd timeout after: %s' % self.last_cmd)

    def status(self):
        return dict(l.split(': ', 1) for l in self.cmd('status') if ': ' in l)

    def outputs(self):
        outs, cur = [], None
        for line in self.cmd('outputs'):
            if line.startswith('outputid: '):
                if cur is not None:
                    outs.append(cur)
                cur = {'outputid': line.split(': ', 1)[1]}
            elif cur is not None and ': ' in line:
                k, v = line.split(': ', 1)
                cur[k] = v
        if cur is not None:
            outs.append(cur)
        return outs

    def enableoutput(self, output_id):
        self.cmd('enableoutput %s' % output_id)

    def disableoutput(self, output_id):
        self.cmd('disableoutput %s' % output_id)

    @staticmethod
    def esc(value):
        return str(value).replace('\\', '\\\\').replace('"', '\\"')

    def play_url(self, url):
        """clear -> addid -> playid. Accepts an http(s) URL or a local path."""
        if not url:
            raise MPDError('no url to play')
        self.cmd('clear')
        out = self.cmd('addid "%s"' % self.esc(url))
        songid = next((l.split(': ', 1)[1].strip() for l in out if l.startswith('Id: ')), None)
        if songid is None:
            raise MPDError('addid returned no Id')
        self.cmd('playid %s' % songid)
        return songid

    def stop(self):
        self.cmd('stop')

    def volume(self, level=None):
        if level is None:
            try:
                return int(self.status().get('volume', -1))
            except ValueError:
                return -1
        self.cmd('setvol %d' % max(0, min(100, int(level))))


def _main():
    what = sys.argv[1] if len(sys.argv) > 1 else ''
    if what == 'search':
        for t in search(sys.argv[2]):
            print(json.dumps(t, ensure_ascii=False))
    elif what == 'link':
        url, q = get_link_info(sys.argv[2])
        print('%s len=%s' % (q, len(url or '')))
    elif what == 'play':
        url, q = get_link_info(sys.argv[2])
        m = MPD()
        try:
            print(q, m.play_url(url), m.status().get('state'))
        finally:
            m.close()
    elif what in ('status', 'outputs'):
        m = MPD()
        try:
            print(json.dumps(m.status() if what == 'status' else m.outputs(), ensure_ascii=False))
        finally:
            m.close()
    else:
        print(__doc__)


if __name__ == '__main__':
    _main()
