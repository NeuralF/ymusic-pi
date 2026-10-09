#!/usr/bin/env python3
"""ymusic web panel: Yandex Music -> MPD, controlled from a browser on :8080."""
import json
import os
import sys
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
import ym_core as y  # noqa: E402

PORT = int(os.environ.get('YM_PORT', '8080'))
CACHE = os.environ.get('YM_CACHE', '/dev/shm/ymusic')   # tracks live in RAM
STATIC = os.path.join(_HERE, 'static')
os.makedirs(CACHE, exist_ok=True)
# Next track is queued in MPD this many seconds before the current one ends (gapless).
PRELOAD_BEFORE_END = 25
STATIC = os.path.join(_HERE, 'static')
LOCK = threading.RLock()
QUALITY = {}  # track id -> 'MP3 320'
S = {
    'queue': [],        # list of track dicts
    'idx': -1,
    'source': '',
    'wave': False,
    'want_play': False,  # we expect MPD to be playing (auto-advance on stop)
    'songid': None,
    'error': '',
    'liked': set(),
    'liked_ts': 0,
    'ready': False,
    'loading': '',
    'next_songid': None,  # MPD id of the preloaded next track
}


def log(*a):
    print(time.strftime('%H:%M:%S'), *a, flush=True)


def api(fn, *a, **kw):
    """Call Yandex API; on auth failure drop cached token/client and retry once."""
    try:
        return fn(*a, **kw)
    except Exception as e:
        name = type(e).__name__
        if 'Unauthorized' in name or 'Forbidden' in name or '401' in str(e):
            log('auth error, refreshing token:', name)
            y._state['token'] = None
            y._state['client'] = None
            return fn(*a, **kw)
        raise


def mpd_call(fn):
    m = y.MPD(timeout=20)
    try:
        return fn(m)
    finally:
        m.close()



def cache_path(tid):
    return os.path.join(CACHE, '%s.mp3' % tid)


def download(tid):
    """Fetch the whole track into RAM before playing it.

    Streaming over Wi-Fi while the DAC plays is what breaks USB audio on small
    boards: with the file local, the radio stays silent during the track.
    """
    os.makedirs(CACHE, exist_ok=True)
    path = cache_path(tid)
    if os.path.exists(path):
        return path
    url, QUALITY[tid] = api(y.get_link_info, tid)
    if not url:
        raise RuntimeError('no download link')
    tmp = '%s.part%d' % (path, threading.get_ident())
    with y.requests.get(url, stream=True, timeout=30) as r:
        r.raise_for_status()
        with open(tmp, 'wb') as f:
            for chunk in r.iter_content(32768):
                f.write(chunk)
    os.replace(tmp, path)
    return path


def cache_cleanup(keep):
    for fn in os.listdir(CACHE):
        if fn.split('.')[0] not in keep:
            try:
                os.remove(os.path.join(CACHE, fn))
            except OSError:
                pass


def liked_set(force=False):
    if force or time.time() - S['liked_ts'] > 300:
        try:
            S['liked'] = set(api(y.liked_ids))
            S['liked_ts'] = time.time()
        except Exception as e:
            log('liked_ids failed:', e)
    return S['liked']


def wave_extend():
    last = S['queue'][-1]['id'] if S['queue'] else None
    new = api(y.wave_tracks, last)
    have = {t['id'] for t in S['queue']}
    S['queue'].extend(t for t in new if t['id'] not in have)


def play_index(i, tries=3):
    """Play queue[i] from RAM; skip unavailable tracks."""
    with LOCK:
        while 0 <= i < len(S['queue']) and tries > 0:
            t = S['queue'][i]
            try:
                S['loading'] = t.get('title', '')
                path = download(t['id'])
                t['q'] = QUALITY.get(t['id'], '')

                def go(m):
                    m.cmd('consume 1')  # played entries leave the queue
                    # over the UNIX socket MPD reads the file directly;
                    # over TCP local files are refused, so serve it ourselves
                    return m.play_url(path if m.local else
                                      'http://127.0.0.1:%d/cache/%s.mp3' % (PORT, t['id']))
                sid = mpd_call(go)
                S.update(idx=i, songid=sid, next_songid=None, want_play=True,
                         error='', loading='')
                log('play', t['id'], t['artist'], '-', t['title'], t.get('q', ''))
                if S['wave'] and len(S['queue']) - i <= 2:
                    try:
                        wave_extend()
                    except Exception as e:
                        log('wave_extend failed:', e)
                keep = {t['id']}
                if i + 1 < len(S['queue']):
                    keep.add(S['queue'][i + 1]['id'])
                cache_cleanup(keep)
                return True
            except Exception as e:
                S['error'] = '%s: %s' % (t.get('title', ''), str(e)[:120])
                log('play failed', t['id'], type(e).__name__, str(e)[:120])
                tries -= 1
                i += 1
        S['want_play'] = False
        S['loading'] = ''
        return False


def preload_next():
    """Queue the next track in MPD so it starts without a gap."""
    j = S['idx'] + 1
    if S['wave'] and j >= len(S['queue']):
        try:
            wave_extend()
        except Exception as e:
            log('wave_extend failed:', e)
    if j >= len(S['queue']):
        S['next_songid'] = 'end'
        return
    t = S['queue'][j]
    try:
        path = download(t['id'])
        t['q'] = QUALITY.get(t['id'], '')

        def go(m):
            return m.cmd('addid "%s"' % m.esc(
                path if m.local else
                'http://127.0.0.1:%d/cache/%s.mp3' % (PORT, t['id'])))
        out = mpd_call(go)
        sid = next((l.split(': ', 1)[1] for l in out if l.startswith('Id: ')), None)
        S['next_songid'] = sid or 'fail'
        log('preloaded', t['id'], t['title'])
    except Exception as e:
        S['next_songid'] = 'fail'   # fall back: play it when the current one stops
        log('preload failed', t['id'], str(e)[:120])


def step(delta):
    with LOCK:
        if not S['queue']:
            return False
        j = S['idx'] + delta
        if S['wave'] and j >= len(S['queue']):
            wave_extend()
        if j < 0:
            j = 0
        if j >= len(S['queue']):
            S['want_play'] = False
            mpd_call(lambda m: m.stop())
            return False
        return play_index(j)


def monitor():
    """Follow MPD: preload next track, notice gapless switches, recover on stop."""
    miss = 0
    while True:
        time.sleep(1)
        with LOCK:
            try:
                st = mpd_call(lambda m: m.status())
                if miss:
                    log('mpd reachable again')
                miss = 0
            except Exception:
                miss += 1
                if miss == 1:
                    log('mpd unreachable, waiting')
                continue
            try:
                if not S['want_play']:
                    continue
                state, cur = st.get('state'), st.get('songid')
                nxt = S['next_songid']
                if nxt and cur == nxt:  # MPD moved on to the preloaded track
                    S['idx'] += 1
                    S['songid'], S['next_songid'] = cur, None
                    t = S['queue'][S['idx']]
                    log('play', t['id'], t['artist'], '-', t['title'], '(gapless)')
                    continue
                if state == 'stop':
                    log('stopped, advancing')
                    step(+1)
                    continue
                if state == 'play' and nxt is None:
                    dur = float(st.get('duration') or 0)
                    if not dur and 0 <= S['idx'] < len(S['queue']):
                        dur = S['queue'][S['idx']].get('duration') or 0
                    el = float(st.get('elapsed') or 0)
                    if dur and dur - el < PRELOAD_BEFORE_END:
                        preload_next()
            except Exception:
                log('monitor error', traceback.format_exc(limit=2))


def state_json():
    st = {}
    try:
        st = mpd_call(lambda m: m.status())
        mpd_ok = True
    except Exception:
        mpd_ok = False
    if True:
        q, ix = S['queue'], S['idx']
        cur = q[ix] if 0 <= ix < len(q) else None
        vol = st.get('volume')
        return {
            'ready': S['ready'],
            'mpd': mpd_ok,
            'state': st.get('state', 'stop'),
            'elapsed': float(st.get('elapsed', 0) or 0),
            'duration': float(st.get('duration', 0) or 0) or (cur or {}).get('duration', 0),
            'volume': int(vol) if vol not in (None, '-1') else None,
            'bitrate': st.get('bitrate'),
            'track': cur,
            'liked': bool(cur and cur['id'] in S['liked']),
            'idx': S['idx'],
            'qlen': len(q),
            'loading': S['loading'],
            'source': S['source'],
            'wave': S['wave'],
            'error': S['error'] or st.get('error', ''),
        }


def _marked(r):
    mark_liked(r['tracks'])
    return r


def mark_liked(tracks):
    ls = liked_set()
    for t in tracks:
        t['liked'] = t['id'] in ls
    return tracks


def command(name, step_=5):
    """Simple GET-able commands for home-screen widgets: up, down, pause, next, prev, stop."""
    name = name.strip('/')
    if name in ('up', 'down'):
        def f(m):
            st = m.status()
            v = int(st.get('volume', -1))
            if v < 0:
                raise RuntimeError('no volume control')
            v = max(0, min(100, v + (step_ if name == 'up' else -step_)))
            m.cmd('setvol %d' % v)
            return v
        return {'ok': True, 'volume': mpd_call(f)}
    if name == 'pause':
        with LOCK:
            st = mpd_call(lambda m: m.status()).get('state')
            if st == 'play':
                mpd_call(lambda m: m.cmd('pause 1'))
            elif st == 'pause':
                mpd_call(lambda m: m.cmd('pause 0'))
            elif S['queue']:
                play_index(max(S['idx'], 0))
        return {'ok': True}
    if name == 'next':
        return {'ok': step(+1)}
    if name == 'prev':
        return {'ok': step(-1)}
    if name == 'stop':
        with LOCK:
            S['want_play'] = False
            mpd_call(lambda m: m.stop())
        return {'ok': True}
    raise ValueError('unknown command: ' + name)


# ---------------------------------------------------------------- HTTP

class H(BaseHTTPRequestHandler):
    server_version = 'ymusic/1'

    def log_message(self, fmt, *args):
        pass

    def _send(self, code, body, ctype='application/json; charset=utf-8'):
        if not isinstance(body, bytes):
            body = json.dumps(body, ensure_ascii=False).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        n = int(self.headers.get('Content-Length') or 0)
        if not n:
            return {}
        return json.loads(self.rfile.read(n).decode('utf-8') or '{}')

    def _run(self, fn):
        try:
            self._send(200, fn())
        except Exception as e:
            log('request error', self.path, type(e).__name__, str(e)[:200])
            self._send(500, {'error': '%s: %s' % (type(e).__name__, str(e)[:200])})

    def do_GET(self):
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        p = u.path
        if p in ('/', '/index.html'):
            with open(os.path.join(_HERE, 'index.html'), 'rb') as f:
                return self._send(200, f.read(), 'text/html; charset=utf-8')
        if p.startswith('/static/') or p in ('/manifest.json', '/sw.js'):
            return self._static(p)
        if p.startswith('/api/cmd/'):
            return self._run(lambda: command(p[9:]))
        if p.startswith('/cache/'):
            return self._cache(p[7:])
        if p == '/api/events':
            return self._events()
        if p == '/api/artist':
            return self._run(lambda: _marked(api(y.artist_page, q['id'])))
        if p == '/api/album':
            return self._run(lambda: _marked(api(y.album_page, q['id'])))
        if p == '/api/state':
            return self._run(state_json)
        if p == '/api/search':
            return self._run(lambda: {'tracks': mark_liked(api(y.search, q.get('q', '')))})
        if p == '/api/liked':
            def f():
                r = api(y.liked_tracks, int(q.get('offset', 0)), int(q.get('limit', 100)))
                liked_set()
                for t in r['tracks']:
                    t['liked'] = True
                return r
            return self._run(f)
        if p == '/api/playlists':
            return self._run(lambda: {'playlists': api(y.playlists)})
        if p == '/api/playlist':
            def f():
                r = api(y.playlist_tracks, q['kind'], int(q.get('offset', 0)), int(q.get('limit', 100)))
                mark_liked(r['tracks'])
                return r
            return self._run(f)
        if p == '/api/queue':
            with LOCK:
                return self._send(200, {'queue': S['queue'], 'idx': S['idx']})
        self._send(404, {'error': 'not found'})

    def _cache(self, name):
        """Only used when MPD talks to us over TCP and cannot read local files."""
        path = os.path.join(CACHE, os.path.basename(name))
        if not os.path.isfile(path):
            return self._send(404, {'error': 'not cached'})
        size = os.path.getsize(path)
        start, end = 0, size - 1
        rng = self.headers.get('Range', '')
        if rng.startswith('bytes='):
            a, _, b = rng[6:].partition('-')
            start = int(a) if a else 0
            end = min(int(b), size - 1) if b else size - 1
            self.send_response(206)
            self.send_header('Content-Range', 'bytes %d-%d/%d' % (start, end, size))
        else:
            self.send_response(200)
        self.send_header('Content-Type', 'audio/mpeg')
        self.send_header('Accept-Ranges', 'bytes')
        self.send_header('Content-Length', str(end - start + 1))
        self.end_headers()
        with open(path, 'rb') as f:
            f.seek(start)
            left = end - start + 1
            while left > 0:
                chunk = f.read(min(65536, left))
                if not chunk:
                    break
                try:
                    self.wfile.write(chunk)
                except (BrokenPipeError, ConnectionResetError):
                    return
                left -= len(chunk)

    def _events(self):
        """Server-sent events: push state only when it actually changes.

        The phone then keeps the Wi-Fi quiet while a track plays, which matters
        a lot for USB audio on small boards.
        """
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream; charset=utf-8')
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        last, last_beat = None, 0.0
        keys = ('state', 'track', 'volume', 'liked', 'idx', 'qlen', 'source',
                'wave', 'error', 'mpd', 'ready', 'duration', 'loading')
        while True:
            try:
                st = state_json()
                sig = json.dumps([st.get(k) for k in keys], ensure_ascii=False, sort_keys=True)
                now = time.time()
                if sig != last or now - last_beat > 25:
                    last, last_beat = sig, now
                    self.wfile.write(('data: %s\n\n' % json.dumps(st, ensure_ascii=False)).encode('utf-8'))
                    self.wfile.flush()
                time.sleep(1)
            except (BrokenPipeError, ConnectionResetError):
                return
            except Exception as e:
                log('events stream error:', str(e)[:100])
                return

    def _static(self, p):
        name = os.path.basename(p)
        path = os.path.join(STATIC, name)
        if not os.path.isfile(path):
            return self._send(404, {'error': 'not found'})
        ctype = {'.png': 'image/png', '.json': 'application/manifest+json',
                 '.js': 'text/javascript', '.svg': 'image/svg+xml'}.get(os.path.splitext(name)[1], 'application/octet-stream')
        with open(path, 'rb') as f:
            self._send(200, f.read(), ctype)

    def do_POST(self):
        p = urlparse(self.path).path
        try:
            b = self._body()
        except Exception:
            return self._send(400, {'error': 'bad json'})

        def play():
            with LOCK:
                S['queue'] = [t for t in b.get('tracks', []) if t.get('id')]
                S['source'] = b.get('source', '')
                S['wave'] = False
                ok = play_index(int(b.get('index', 0)))
            return {'ok': ok, 'error': S['error']}

        def wave():
            with LOCK:
                S['queue'] = []
                S['wave'] = True
                S['source'] = 'My Wave'
                wave_extend()
                ok = play_index(0)
            return {'ok': ok, 'error': S['error']}

        def append():
            with LOCK:
                have = {t['id'] for t in S['queue']}
                S['queue'].extend(t for t in b.get('tracks', []) if t.get('id') not in have)
            return {'ok': True, 'qlen': len(S['queue'])}

        def pause():
            with LOCK:
                st = mpd_call(lambda m: m.status()).get('state')
                if st == 'play':
                    mpd_call(lambda m: m.cmd('pause 1'))
                elif st == 'pause':
                    mpd_call(lambda m: m.cmd('pause 0'))
                elif S['queue']:
                    play_index(max(S['idx'], 0))
            return {'ok': True}

        def stop():
            with LOCK:
                S['want_play'] = False
                mpd_call(lambda m: m.stop())
            return {'ok': True}

        def volume():
            if 'delta' in b:
                return command('up' if int(b['delta']) > 0 else 'down', abs(int(b['delta'])))
            v = max(0, min(100, int(b.get('volume', 50))))
            mpd_call(lambda m: m.cmd('setvol %d' % v))
            return {'ok': True}

        def seek():
            pos = float(b.get('pos', 0))

            def run():
                m = y.MPD(timeout=60)
                try:
                    m.cmd('seekcur %.1f' % pos)
                except Exception as e:
                    log('seek failed:', e)
                finally:
                    m.close()
            threading.Thread(target=run, daemon=True).start()
            return {'ok': True}

        def like():
            tid = str(b['id'])
            on = bool(b.get('on', True))
            api(y.like, tid, on)
            (S['liked'].add if on else S['liked'].discard)(tid)
            return {'ok': True, 'liked': on}

        routes = {
            '/api/play': play, '/api/wave': wave, '/api/append': append,
            '/api/pause': pause, '/api/stop': stop,
            '/api/next': lambda: {'ok': step(+1)},
            '/api/prev': lambda: {'ok': step(-1)},
            '/api/volume': volume, '/api/seek': seek, '/api/like': like,
        }
        if p in routes:
            return self._run(routes[p])
        self._send(404, {'error': 'not found'})


def warmup():
    while True:
        try:
            y.get_client()
            liked_set(force=True)
            S['ready'] = True
            log('yandex client ready, liked:', len(S['liked']))
            return
        except Exception as e:
            log('warmup failed, retry in 15s:', type(e).__name__, str(e)[:120])
            time.sleep(15)


def main():
    threading.Thread(target=warmup, daemon=True).start()
    threading.Thread(target=monitor, daemon=True).start()
    srv = ThreadingHTTPServer(('0.0.0.0', PORT), H)
    srv.daemon_threads = True
    log('listening on :%d' % PORT)
    srv.serve_forever()


if __name__ == '__main__':
    main()
