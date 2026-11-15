#!/usr/bin/env python3
r"""
MusicBee Web - listen to your MusicBee library & playlists from any browser.

DISCLAIMER
    This is a personal project, written with AI assistance under human guidance.
    It is free for anyone to use at no cost, but it
    is provided "as is" without any warranty - the author takes no responsibility
    for it or for anything that happens while using it (including data loss,
    damage or security incidents). When running it, apply best-practice security
    measures: bind to 127.0.0.1 when testing, use AUTH_PASSWORD (or a reverse
    proxy / Cloudflare) on shared networks, and never expose it directly to the
    internet.

Strictly read-only: it only opens files for reading - nothing on disk is ever
written, renamed or deleted.

Reads your existing MusicBee data:
  * Playlists : <data_dir>\Playlists\*.mbp  (MusicBee native binary format)
  * Library DB: <data_dir>\MusicBeeLibrary.mbl (for track durations)

Single file, Python standard library only.  Run:

    python musicbee_web.py                          # uses the CONFIG section below
    python musicbee_web.py --host 127.0.0.1 --port 9000

Settings live in the CONFIGURATION block near the top of this file.
Command-line arguments override the config values when given:

Options (override the corresponding CONFIG value):
    --port PORT          port to listen on            (config: PORT)
    --host HOST          address to bind              (config: HOST)
    --data-dir DIR       MusicBee data dir            (config: DATA_DIR, else auto-detect)
    --playlists-dir DIR  playlists folder             (config: PLAYLISTS_DIR)
    --music-root DIR [DIR ...]  music folders         (config: MUSIC_ROOTS)
    --auth-password PW   require a shared password    (config: AUTH_PASSWORD)
"""
import argparse
import base64
import hmac
import json
import mimetypes
import os
import re
import socketserver
import struct
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, unquote, quote

# ============================================================================
#  CONFIGURATION - edit these to match your setup
#
#  Any value left as None is auto-detected. Command-line arguments
#  (e.g. "python musicbee_web.py --host 127.0.0.1") override these values.
#
#  Windows paths: keep the r"..." form shown in the examples below - the r
#  makes Python treat backslashes literally. Without it, paths like
#  "C:\Users\You" break (\U is a special escape sequence) or silently turn
#  into wrong folders (\t becomes a tab character). Alternative: double
#  every backslash instead - "C:\\Users\\You".
# ============================================================================

HOST = "0.0.0.0"     # Address the server listens on:
                     #   "0.0.0.0"   -> reachable from other devices on your network
                     #   "127.0.0.1" -> this PC only (use this if you're just testing)

PORT = 8321          # Port for the web player (any free port works)

DATA_DIR = None      # MusicBee data folder, e.g. r"C:\Users\You\Music\MusicBee"
                     # None = auto-detect (%APPDATA%\MusicBee or
                     # C:\Users\<you>\Music\MusicBee if it exists there)

PLAYLISTS_DIR = None  # Playlists folder, e.g. r"C:\Users\You\Music\MusicBee\Playlists"
                      # None = use <DATA_DIR>\Playlists

MUSIC_ROOTS = []     # Folders that contain your music files - list as many as you like:
                     #   MUSIC_ROOTS = [r"C:\Users\You\Music", r"D:\Music"]
                     # Empty list = auto-detect (parent of DATA_DIR or <you>\Music)

ENFORCE_SAFE_DIRS = True  # Refuse music roots that would expose sensitive folders
                          # (Windows, Program Files, user profile dirs) - e.g. if you
                          # point a root at C:\ or C:\Users\You. Set to False to disable.

AUTH_PASSWORD = None  # Optional shared password for the player. When set to a
                      # non-empty string, browsers show their standard login prompt
                      # before anything loads. The prompt also asks for a user name -
                      # that field is ignored, so anything can be typed there; only
                      # the password (checked on this PC only) matters. Leave as None
                      # to keep it open. This is basic shared-secret auth, not per-user
                      # accounts - for stronger security put a reverse proxy in front
                      # (e.g. nginx with proper authentication) or, better yet, use
                      # Cloudflare (Tunnel + Access), which adds real per-user logins
                      # and HTTPS.

AUDIO_EXTS = {
    '.mp3', '.m4a', '.aac', '.ogg', '.opus', '.flac', '.wav',
    '.wma', '.ape', '.mpc', '.wv', '.aiff', '.aif',
}
VIDEO_EXTS = {
    '.mp4', '.m4v', '.webm', '.mov', '.mkv', '.avi', '.wmv', '.flv',
}
MEDIA_EXTS = AUDIO_EXTS | VIDEO_EXTS
# formats modern browsers (Chrome/Edge/Firefox) can decode
BROWSER_OK = {'.mp3', '.m4a', '.aac', '.ogg', '.opus', '.flac', '.wav',
              '.mp4', '.m4v', '.webm'}
CONTENT_TYPES = {
    '.mp3': 'audio/mpeg', '.m4a': 'audio/mp4', '.aac': 'audio/aac',
    '.ogg': 'audio/ogg', '.opus': 'audio/ogg', '.flac': 'audio/flac',
    '.wav': 'audio/wav', '.wma': 'audio/x-ms-wma', '.aiff': 'audio/aiff',
    '.aif': 'audio/aiff', '.ape': 'audio/x-ape',
    '.mp4': 'video/mp4', '.m4v': 'video/mp4', '.webm': 'video/webm',
    '.mov': 'video/quicktime', '.mkv': 'video/x-matroska',
    '.avi': 'video/x-msvideo', '.wmv': 'video/x-ms-wmv', '.flv': 'video/x-flv',
}

# --------------------------------------------------------------------------
# .mbp playlist parsing (MusicBee native binary format)
# Entries are framed with 0xFFFFFFFF separators; each track path is a run of
# printable bytes starting with a drive letter (occasionally the drive letter
# byte is missing for files that used to live on another drive).
# --------------------------------------------------------------------------
def parse_mbp(data: bytes):
    tracks = []
    for chunk in data.split(b'\xff\xff\xff\xff'):
        i = 0
        n = len(chunk)
        while i < n:
            if chunk[i:i + 2] in (b'C:', b'D:') or chunk[i:i + 1] == b':':
                j = i
                while j < n and 32 <= chunk[j] < 127:
                    j += 1
                s = chunk[i:j].decode('ascii', 'replace')
                if len(s) > 15 and '\\' in s[4:] and \
                        os.path.splitext(s)[1].lower() in MEDIA_EXTS:
                    tracks.append(s)
                i = max(j, i + 1)
            else:
                i += 1
    return tracks


def build_duration_index(mbl_path):
    """Scan MusicBeeLibrary.mbl and return {full_path: duration_seconds}.
    Records are located via their path field; the duration (ms) sits at a
    fixed offset after the path in the record layout."""
    idx = {}
    try:
        data = open(mbl_path, 'rb').read()
    except OSError:
        return idx
    i = 0
    n = len(data)
    while True:
        i = data.find(b'H', i)
        if i < 0 or i + 5 > n:
            break
        ln = data[i + 3]
        p = data[i + 4:i + 4 + ln]
        if 20 < ln < 400 and len(p) == ln and p[:2] in (b'C:', b'D:') \
                and b'\x00' not in p:
            try:
                s = p.decode('utf-8')
            except UnicodeDecodeError:
                i += 1
                continue
            end = i + 4 + ln
            if end + 16 <= n:
                dur_ms = struct.unpack_from('<I', data, end + 12)[0]
                if 3000 < dur_ms < 1_800_000:      # sanity: 3s .. 30min
                    idx[s] = dur_ms // 1000
            i = end
        else:
            i += 1
    return idx

# --------------------------------------------------------------------------
# Metadata derived from file layout (Artist\Album\Track naming)
# --------------------------------------------------------------------------
BLACKLIST_DIRS = {
    'music', 'itunes media', 'unknown artist', 'unknown album',
    'podcasts', 'musicbee', 'itunes', 'soundtrack',
}


def meta_from_path(p):
    parts = p.split('\\')
    fname = os.path.splitext(parts[-1])[0]
    title = re.sub(r'^\s*\d{1,3}(?:[\.\-\u2013)\]]|\s)+\s*', '', fname).strip() or fname
    artist = album = None
    if len(parts) >= 4:
        a_dir, al_dir = parts[-3], parts[-2]
        if al_dir.lower() not in BLACKLIST_DIRS and a_dir.lower() not in BLACKLIST_DIRS:
            album, artist = al_dir, a_dir
        elif al_dir.lower() not in BLACKLIST_DIRS:
            album = al_dir
    if not artist and not album and ' - ' in title:
        a, t = title.split(' - ', 1)
        artist, title = a.strip(), t.strip()
    return title, artist, album


def norm_name(name):
    """Normalize a file name for fuzzy matching (strip zero-padded numbers)."""
    base = os.path.splitext(os.path.basename(name))[0]
    m = re.match(r'^(\d+)\s*[\.\-\u2013)\]]+\s*(.*)$', base)
    if m:
        return (int(m.group(1)), m.group(2).strip().lower())
    return (None, base.strip().lower())

# --------------------------------------------------------------------------
# Library / playlist model
# --------------------------------------------------------------------------
class MusicBeeWeb:
    def __init__(self, data_dir, playlists_dir, music_roots):
        self.data_dir = data_dir
        self.playlists_dir = playlists_dir
        self.music_roots = [os.path.normpath(r) for r in (music_roots or []) if r]
        self.durations = {}
        self.disk_index = {}      # norm_name -> [full paths]
        self.library_tracks = []  # built-in 'Full Library'
        self.scan_time = time.time()
        self._playlist_cache = None
        self._cache_sig = None
        self.refresh()

    # -- scanning ----------------------------------------------------------
    def _walk_audio(self, root):
        out = []
        if not root or not os.path.isdir(root):
            return out
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames
                           if d not in ('.SynologyWorkingDirectory', 'MusicBee')]
            for fn in filenames:
                if os.path.splitext(fn)[1].lower() in MEDIA_EXTS:
                    out.append(os.path.normpath(os.path.join(dirpath, fn)))
        return out

    def refresh(self):
        t0 = time.time()
        mbl = os.path.join(self.data_dir, 'MusicBeeLibrary.mbl')
        self.durations = build_duration_index(mbl)

        files, seen = [], set()
        for root in self.music_roots:
            for f in self._walk_audio(root):
                if f not in seen:
                    seen.add(f)
                    files.append(f)
        self.disk_index = {}
        for f in files:
            self.disk_index.setdefault(norm_name(f), []).append(f)
        self.library_tracks = [self._track_info(f) for f in sorted(files)]

        sig = []
        if os.path.isdir(self.playlists_dir):
            for fn in sorted(os.listdir(self.playlists_dir)):
                if fn.lower().endswith('.mbp'):
                    fp = os.path.join(self.playlists_dir, fn)
                    try:
                        st = os.stat(fp)
                        sig.append((fn, int(st.st_mtime), st.st_size))
                    except OSError:
                        pass
        self._cache_sig = tuple(sig)
        self._playlist_cache = self._build_playlists()
        self.scan_time = time.time()
        print(f"[scan] {len(files)} audio files, {len(self.durations)} durations from DB, "
              f"{len(self._playlist_cache)} playlists in {time.time() - t0:.1f}s")

    def _maybe_refresh(self):
        sig = []
        if os.path.isdir(self.playlists_dir):
            for fn in sorted(os.listdir(self.playlists_dir)):
                if fn.lower().endswith('.mbp'):
                    fp = os.path.join(self.playlists_dir, fn)
                    try:
                        st = os.stat(fp)
                        sig.append((fn, int(st.st_mtime), st.st_size))
                    except OSError:
                        pass
        if tuple(sig) != self._cache_sig:
            print("[scan] playlists changed - rescanning")
            self.refresh()
            # the set of streamable folders depends on the playlist contents,
            # so recompute it whenever the playlists change
            Handler.allowed_roots = self.allowed_roots()

    def _resolve(self, raw):
        """Resolve a playlist path; returns (abs_path_or_None, matched_fuzzy)."""
        p = os.path.normpath(raw)
        if os.path.isfile(p):
            return p, False
        # drive letter missing? try each music root / the C: drive
        for cand in [os.path.join(r, os.path.basename(p)) for r in self.music_roots] \
                   + (['C:' + p] if p.startswith('\\') else []):
            if os.path.isfile(cand):
                return cand, True
        # fuzzy match on normalized file name
        hits = self.disk_index.get(norm_name(raw))
        if hits:
            return hits[0], True
        return None, False

    def _track_info(self, path):
        title, artist, album = meta_from_path(path)
        ext = os.path.splitext(path)[1].lower()
        return {
            'title': title,
            'kind': 'video' if ext in VIDEO_EXTS else 'audio',
            'artist': artist or '',
            'album': album or '',
            'path': path,
            'dur': self.durations.get(path),
            'exists': True,
            'ok': ext in BROWSER_OK,
            'ext': ext.lstrip('.'),
        }

    def _build_playlists(self):
        pls = []
        if not os.path.isdir(self.playlists_dir):
            return pls
        for fn in sorted(os.listdir(self.playlists_dir)):
            low = fn.lower()
            if low.endswith('.mbp'):
                stem = fn[:-4]
                if stem.lower().startswith('nowplaying') or '_conflict' in low:
                    continue          # internal / sync-conflict copies
                raw_paths = parse_mbp(open(os.path.join(self.playlists_dir, fn), 'rb').read())
                tracks, missing, fuzzy = [], 0, 0
                for rp in raw_paths:
                    resolved, was_fuzzy = self._resolve(rp)
                    if resolved is None:
                        t = self._track_info(rp)
                        t['exists'] = False
                        missing += 1
                    else:
                        t = self._track_info(resolved)
                        fuzzy += int(was_fuzzy)
                    tracks.append(t)
                pls.append({'name': stem, 'count': len(tracks),
                            'missing': missing, 'fuzzy': fuzzy,
                            'tracks': tracks})
            elif low.endswith('.xautopf'):
                pls.append({'name': fn[:-8], 'count': 0, 'missing': 0,
                            'fuzzy': 0, 'smart': True, 'tracks': []})
        if self.library_tracks:
            pls.insert(0, {'name': '\u2605 Full Library', 'count': len(self.library_tracks),
                           'missing': sum(1 for t in self.library_tracks if not t['exists']),
                           'fuzzy': 0, 'library': True, 'tracks': self.library_tracks})
        return pls

    # -- queries used by the HTTP layer -------------------------------------
    def playlist_summary(self):
        self._maybe_refresh()
        return [{'name': p['name'], 'count': p['count'], 'missing': p['missing'],
                 'fuzzy': p.get('fuzzy', 0), 'smart': p.get('smart', False),
                 'library': p.get('library', False)} for p in self._playlist_cache]

    def playlist_tracks(self, name):
        self._maybe_refresh()
        for p in self._playlist_cache:
            if p['name'] == name:
                out = []
                for t in p['tracks']:
                    out.append({
                        'title': t['title'], 'artist': t['artist'], 'album': t['album'],
                        'dur': t['dur'], 'exists': t['exists'], 'ok': t['ok'], 'ext': t['ext'],
                        'kind': t['kind'],
                        'url': '/music?p=' + quote(t['path']),
                    })
                return out
        return None

    def allowed_roots(self):
        """Folders the HTTP layer may stream from (realpath-resolved).

        Paths are compared after os.path.realpath(), so a symlink/junction
        pointing outside these folders is NOT covered - if you use a junction
        inside your music folder to pull in files from another drive, add the
        target folder to MUSIC_ROOTS as well."""
        roots = set()
        for r in self.music_roots:
            roots.add(os.path.normcase(os.path.realpath(r)))
        # existing playlist tracks may live outside the music roots - allow their
        # own folders only (not whole drives) so they can still be streamed
        for p in self._playlist_cache or []:
            for t in p['tracks']:
                path = t['path']
                if os.path.isfile(path):
                    roots.add(os.path.normcase(
                        os.path.realpath(os.path.dirname(os.path.normpath(path)))))
        return roots


# --------------------------------------------------------------------------
# HTTP server
# --------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    timeout = 30                  # release idle keep-alive connections (threads)
    mbw: MusicBeeWeb = None       # set in main()
    allowed_roots = set()         # recomputed at startup and on playlist refresh
    auth_password = ''            # set in main(); empty string = no auth required

    def log_message(self, fmt, *args):
        msg = "  %s\n" % (fmt % args)
        # keep full music file paths out of the console log
        sys.stdout.write(re.sub(r'(/music\?p=)[^"\s]*', r'\1[redacted]', msg))

    def _authorized(self):
        if not self.auth_password:
            return True
        h = self.headers.get('Authorization', '')
        if not h.startswith('Basic '):
            return False
        try:
            decoded = base64.b64decode(h[6:].strip()).decode('utf-8', 'replace')
        except (ValueError, TypeError):
            return False
        _user, _, pw = decoded.partition(':')
        # any user name is accepted; only the shared password matters
        return hmac.compare_digest(pw.encode('utf-8'),
                                   self.auth_password.encode('utf-8'))

    def _require_auth(self):
        """Return True if the request may proceed; else send 401 and stop."""
        if self._authorized():
            return True
        body = json.dumps({'error': 'authentication required'}).encode('utf-8')
        self.send_response(401)
        self.send_header('WWW-Authenticate', 'Basic realm="MusicBee Web"')
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('X-Frame-Options', 'DENY')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            pass
        return False

    def _send(self, code, body, ctype='application/json; charset=utf-8', extra=None):
        data = body if isinstance(body, bytes) else json.dumps(body).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Frame-Options', 'DENY')
        self.send_header('X-Content-Type-Options', 'nosniff')
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            pass

    def handle(self):
        # Browsers routinely abort connections (switching tracks, seeking,
        # closing the tab). On Windows that surfaces as WinError 10053/10054
        # while we wait for the next request on a keep-alive socket - it's
        # harmless, so don't print a scary traceback for it.
        try:
            super().handle()
        except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError,
                TimeoutError):
            pass

    def do_GET(self):
        if not self._require_auth():
            return
        u = urlparse(self.path)
        try:
            if u.path == '/':
                self._send(200, UI_HTML.encode('utf-8'), 'text/html; charset=utf-8')
            elif u.path == '/api/playlists':
                self._send(200, self.mbw.playlist_summary())
            elif u.path == '/api/tracks':
                name = parse_qs(u.query).get('playlist', [''])[0]
                tracks = self.mbw.playlist_tracks(name)
                if tracks is None:
                    self._send(404, {'error': 'playlist not found'})
                else:
                    self._send(200, tracks)
            elif u.path == '/music':
                p = unquote(parse_qs(u.query).get('p', [''])[0])
                self.stream_file(p)
            else:
                self._send(404, {'error': 'not found'})
        except Exception as e:                      # keep the server alive
            print("[error]", e)
            try:
                self._send(500, {'error': str(e)})
            except Exception:
                pass

    def stream_file(self, p):
        if not p or '\\' not in p and ':' not in p[:3]:
            self._send(400, {'error': 'bad path'})
            return
        # realpath() resolves symlinks/junctions so a link inside the music
        # folder can't be used to reach files outside the allowed roots
        real = os.path.realpath(os.path.normpath(p))
        rc = os.path.normcase(real)
        if not any(rc == r or rc.startswith(r + '\\') for r in self.allowed_roots):
            self._send(403, {'error': 'forbidden'})
            return
        if not os.path.isfile(real):
            self._send(404, {'error': 'file not found'})
            return
        size = os.path.getsize(real)
        ctype = CONTENT_TYPES.get(os.path.splitext(real)[1].lower(),
                                  mimetypes.guess_type(real)[0] or 'application/octet-stream')
        rng = self.headers.get('Range')
        start, end = 0, size - 1
        status = 200
        if rng and rng.startswith('bytes='):
            spec = rng[6:].split(',')[0]
            a, _, b = spec.partition('-')
            try:
                if a:
                    start = int(a)
                    end = int(b) if b else size - 1
                elif b:
                    start = max(0, size - int(b))
            except ValueError:
                self._send(400, {'error': 'bad range'})
                return
            if start >= size or end < start:
                self.send_response(416)
                self.send_header('Content-Range', f'bytes */{size}')
                self.send_header('Content-Length', '0')
                self.send_header('X-Frame-Options', 'DENY')
                self.send_header('X-Content-Type-Options', 'nosniff')
                self.end_headers()
                return
            status = 206
        length = end - start + 1
        self.send_response(status)
        self.send_header('Content-Type', ctype)
        self.send_header('Accept-Ranges', 'bytes')
        self.send_header('X-Frame-Options', 'DENY')
        self.send_header('X-Content-Type-Options', 'nosniff')
        if status == 206:
            self.send_header('Content-Range', f'bytes {start}-{end}/{size}')
        self.send_header('Content-Length', str(length))
        self.end_headers()
        try:
            with open(real, 'rb') as fh:
                fh.seek(start)
                remaining = length
                while remaining > 0:
                    chunk = fh.read(min(1 << 20, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            pass

# --------------------------------------------------------------------------
# UI
# --------------------------------------------------------------------------
UI_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>MusicBee Web</title>
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'%3E%3Ctext y='.9em' font-size='90'%3E%F0%9F%8E%B5%3C/text%3E%3C/svg%3E">
<style>
:root{
  --bg:#101318; --panel:#171b23; --panel2:#1d222d; --line:#262c3a;
  --text:#e8ebf2; --dim:#8b93a7; --accent:#4da3ff; --accent2:#7ee0a3;
  --warn:#ffb454; --bad:#ff6b6b;
}
*{box-sizing:border-box; margin:0; padding:0}
html,body{height:100%; height:100dvh}
body{background:var(--bg); color:var(--text);
  font:14px/1.45 -apple-system,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;
  display:flex; flex-direction:column; overflow:hidden}
header{display:flex; align-items:center; gap:10px; padding:10px 16px;
  background:linear-gradient(90deg,#141926,#101318);
  border-bottom:1px solid var(--line); flex:0 0 auto}
header .logo{font-size:18px; font-weight:700; letter-spacing:.3px}
header .logo b{color:var(--accent)}
header .sub{color:var(--dim); font-size:12px}
#menubtn{display:none; width:38px; height:38px}
body,button,.trow,input[type=range]{-webkit-tap-highlight-color:transparent;
  touch-action:manipulation}
#main{flex:1 1 auto; display:flex; min-height:0}
/* sidebar */
aside{width:300px; flex:0 0 auto; background:var(--panel);
  border-right:1px solid var(--line); display:flex; flex-direction:column; min-height:0}
#search{margin:10px; padding:8px 12px; border-radius:8px; border:1px solid var(--line);
  background:var(--panel2); color:var(--text); outline:none; font-size:13px}
#search:focus{border-color:var(--accent)}
#pllist{flex:1 1 auto; overflow-y:auto; padding:0 6px 6px}
.pl{padding:8px 12px; border-radius:8px; cursor:pointer; display:flex;
  justify-content:space-between; gap:8px; align-items:center; margin-bottom:2px}
.pl:hover{background:var(--panel2)}
.pl.active{background:linear-gradient(90deg,rgba(77,163,255,.18),rgba(77,163,255,.05));
  box-shadow:inset 2px 0 0 var(--accent)}
.pl .nm{overflow:hidden; text-overflow:ellipsis; white-space:nowrap}
.pl .ct{color:var(--dim); font-size:12px; flex:0 0 auto}
.badge{font-size:10px; padding:1px 6px; border-radius:8px; margin-left:4px}
.badge.miss{background:rgba(255,107,107,.15); color:var(--bad)}
.badge.smart{background:rgba(126,224,163,.15); color:var(--accent2)}
#status{padding:8px 12px; border-top:1px solid var(--line); color:var(--dim); font-size:11px}
/* track list */
section{flex:1 1 auto; display:flex; flex-direction:column; min-width:0; min-height:0}
#thead,.trow{display:grid; grid-template-columns:44px minmax(220px,3fr) minmax(140px,2fr)
  minmax(140px,2fr) 64px; gap:8px; padding:7px 14px; align-items:center}
#thead{color:var(--dim); font-size:11px; text-transform:uppercase; letter-spacing:.8px;
  border-bottom:1px solid var(--line); background:var(--panel); flex:0 0 auto}
#tlist{flex:1 1 auto; overflow-y:auto}
.trow{cursor:pointer; border-bottom:1px solid rgba(38,44,58,.5); user-select:none;
  -webkit-user-select:none}  /* no blue word-highlight when double-clicking to play */
.trow:hover{background:var(--panel2)}
.trow.sel{background:rgba(77,163,255,.14)}
.trow.playing{background:linear-gradient(90deg,rgba(77,163,255,.16),transparent)}
.trow.missing{opacity:.4}
.cell{overflow:hidden; text-overflow:ellipsis; white-space:nowrap}
.cell.dim{color:var(--dim)}
.eq{display:inline-flex; gap:2px; align-items:flex-end; height:14px; width:18px; justify-content:center}
.eq i{width:3px; background:var(--accent); animation:eq 1s ease-in-out infinite}
.eq i:nth-child(2){animation-delay:.25s}.eq i:nth-child(3){animation-delay:.5s}
@keyframes eq{0%,100%{height:4px}50%{height:14px}}
#empty{flex:1; display:flex; align-items:center; justify-content:center;
  color:var(--dim); font-size:15px}
/* player bar */
footer{flex:0 0 auto; background:var(--panel); border-top:1px solid var(--line);
  padding:8px 16px; display:grid; grid-template-columns:1fr auto 1fr;
  gap:18px; align-items:center}
.frow-top{display:contents}
#npinfo{order:1; min-width:0}
#nptitle{font-weight:600; overflow:hidden; text-overflow:ellipsis; white-space:nowrap}
#npartist{color:var(--dim); font-size:12px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap}
.ctr{display:flex; flex-direction:column; gap:4px; min-width:480px; order:2}
.btns{display:flex; gap:14px; justify-content:center; align-items:center}
button{background:var(--panel2); border:1px solid var(--line); color:var(--text);
  border-radius:8px; padding:7px 12px; cursor:pointer; font-size:14px}
button:hover{border-color:var(--accent)}
button.on{color:var(--accent); border-color:var(--accent)}
.pbtn{width:42px; height:42px; border-radius:50%; padding:0; display:flex;
  align-items:center; justify-content:center; background:var(--panel2);
  border:1px solid var(--line); color:var(--text); cursor:pointer; transition:all .15s}
.pbtn svg{width:20px; height:20px; fill:currentColor; display:block}
.pbtn:hover{border-color:var(--accent); color:var(--accent)}
.pbtn.on{color:var(--accent); border-color:var(--accent); background:rgba(77,163,255,.12)}
#playbtn{width:54px; height:54px; background:linear-gradient(135deg,var(--accent),#2f6fd8);
  border:none; color:#fff}
#playbtn:hover{color:#fff; filter:brightness(1.15)}
#playbtn svg{width:26px; height:26px}
#repbtn{position:relative}
#repbtn .rbadge{position:absolute; top:-3px; right:-3px; min-width:15px; height:15px;
  padding:0 3px; border-radius:8px; background:var(--accent); color:#fff; font-size:9px;
  font-weight:700; display:flex; align-items:center; justify-content:center}
.hide{display:none!important}
.hidden{display:none!important}
.seekrow{display:flex; align-items:center; gap:10px}
time{color:var(--dim); font-size:12px; font-variant-numeric:tabular-nums; flex:0 0 auto}
/* sliders (seek + volume + EQ faders) */
input[type=range]{-webkit-appearance:none; appearance:none; height:18px; background:transparent;
  outline:none; cursor:pointer; --fill:0%}
input[type=range]::-webkit-slider-runnable-track{height:6px; border-radius:3px;
  background:linear-gradient(90deg, var(--accent) var(--fill), var(--line) var(--fill))}
input[type=range]::-webkit-slider-thumb{-webkit-appearance:none; width:14px; height:14px;
  margin-top:-4px; border-radius:50%; background:#fff; border:none;
  box-shadow:0 1px 4px rgba(0,0,0,.5); transition:transform .12s ease}
input[type=range]:hover::-webkit-slider-thumb{transform:scale(1.2)}
input[type=range]:active::-webkit-slider-thumb{transform:scale(1.3)}
input[type=range]::-moz-range-track{height:6px; border-radius:3px;
  background:linear-gradient(90deg, var(--accent) var(--fill), var(--line) var(--fill))}
input[type=range]::-moz-range-thumb{width:14px; height:14px; border:none; border-radius:50%;
  background:#fff; box-shadow:0 1px 4px rgba(0,0,0,.5); transition:transform .12s ease}
input[type=range]:hover::-moz-range-thumb{transform:scale(1.2)}
.seekwrap{position:relative; flex:1 1 auto}
#seek{width:100%; display:block}
#seektip{position:absolute; bottom:21px; left:0; transform:translateX(-50%);
  background:#2a3040; border:1px solid var(--line); color:var(--text); padding:2px 8px;
  border-radius:6px; font-size:11px; font-variant-numeric:tabular-nums; white-space:nowrap;
  pointer-events:none; box-shadow:0 4px 14px rgba(0,0,0,.4); z-index:5}
.volwrap{position:relative; flex:0 0 auto}
#voltip{position:absolute; bottom:21px; left:50%; transform:translateX(-50%);
  background:#2a3040; border:1px solid var(--line); color:var(--text); padding:2px 8px;
  border-radius:6px; font-size:11px; font-variant-numeric:tabular-nums; white-space:nowrap;
  pointer-events:none; box-shadow:0 4px 14px rgba(0,0,0,.4); z-index:5}
.right{display:flex; align-items:center; gap:10px; justify-content:flex-end; order:3}
.vicon{color:var(--dim); display:flex}
.vicon svg{width:20px;height:20px;fill:currentColor}
#vol{width:110px}
/* video badge */
.vtag{display:inline-block; font-size:9px; letter-spacing:.6px; font-weight:700;
  color:var(--accent2); border:1px solid rgba(126,224,163,.45); border-radius:4px;
  padding:1px 5px; margin-right:7px; vertical-align:1px}
/* video stage */
#stage{position:fixed; inset:0; z-index:50; background:rgba(8,10,14,.85);
  backdrop-filter:blur(6px); display:flex; flex-direction:column;
  align-items:center; justify-content:center; gap:12px}
#stage.hidden{display:none}
.stage-head{width:min(92vw,880px); display:flex; justify-content:space-between;
  align-items:center; gap:12px}
#stagenm{font-size:14px; font-weight:600; overflow:hidden; text-overflow:ellipsis;
  white-space:nowrap}
#stageclose{width:34px; height:34px; border-radius:50%; padding:0; flex:0 0 auto;
  display:flex; align-items:center; justify-content:center; font-size:15px}
.stagebox{width:min(92vw,880px); aspect-ratio:16/9; background:#000; border-radius:12px;
  overflow:hidden; box-shadow:0 24px 70px rgba(0,0,0,.65)}
#media{width:100%; height:100%; display:block}
/* equalizer */
#eqpanel{position:fixed; right:16px; bottom:88px; z-index:40; background:var(--panel);
  border:1px solid var(--line); border-radius:14px; padding:14px 16px 12px;
  box-shadow:0 14px 44px rgba(0,0,0,.55)}
#eqpanel.hidden{display:none}
.eq-head{display:flex; align-items:center; gap:10px; margin-bottom:12px}
.eq-title{font-size:11px; text-transform:uppercase; letter-spacing:.8px; color:var(--dim)}
#eqpreset{background:var(--panel2); color:var(--text); border:1px solid var(--line);
  border-radius:6px; padding:4px 8px; font-size:13px}
.chk{font-size:12px; color:var(--dim); display:flex; gap:5px; align-items:center;
  margin-left:auto; cursor:pointer}
.eq-faders{display:flex; gap:4px}
.fader{display:flex; flex-direction:column; align-items:center; gap:5px}
.slot{height:120px; width:26px; display:flex; align-items:center; justify-content:center}
.fader input[type=range]{width:108px; transform:rotate(-90deg)}
.fl{font-size:9px; color:var(--dim); font-variant-numeric:tabular-nums}
/* toast */
#toast{position:fixed; bottom:90px; left:50%; transform:translateX(-50%);
  background:#2a3040; border:1px solid var(--line); padding:9px 18px; border-radius:10px;
  font-size:13px; opacity:0; pointer-events:none; transition:opacity .25s; z-index:60}
#toast.show{opacity:1}
@media (max-width:900px){ aside{width:220px} .ctr{min-width:300px}
  #thead,.trow{grid-template-columns:40px 2fr 60px} .col-album,.h-album{display:none} }
/* ---------- phones (≤700px) ---------- */
@media (max-width:700px){
  header{padding:8px 12px}
  header .sub{display:none}
  #menubtn{display:flex}
  aside{position:fixed; top:0; left:0; bottom:0; width:min(85vw,320px); z-index:70;
    transform:translateX(-105%); transition:transform .22s ease;
    box-shadow:0 0 60px rgba(0,0,0,.5)}
  aside.open{transform:none}
  #scrim{position:fixed; inset:0; background:rgba(8,10,14,.55); z-index:65}
  #thead,.trow{grid-template-columns:36px minmax(100px,2fr) minmax(80px,1.4fr) 52px;
    gap:6px; padding:9px 10px}
  .col-album,.h-album{display:none}
  footer{display:flex; flex-direction:column; align-items:stretch; gap:8px; padding:10px 12px}
  .frow-top{display:flex; align-items:center; justify-content:space-between; gap:10px}
  #npinfo{flex:1 1 auto}
  #nptitle{font-size:13px}
  #npartist{font-size:11px}
  .right{flex:0 0 auto}
  .ctr{min-width:0; width:100%; flex-direction:column-reverse}
  .btns{gap:12px}
  .pbtn{width:40px; height:40px}
  #vol{width:84px}
  #eqpanel{left:10px; right:10px; width:auto; bottom:170px}
  .eq-faders{overflow-x:auto; padding-bottom:2px}
  #toast{bottom:170px; z-index:80}
}
@media (max-width:360px){
  #thead,.trow{grid-template-columns:30px 1fr 48px}
  .col-artist,.h-artist{display:none}
}
</style>
</head>
<body>
<header>
  <button id="menubtn" class="pbtn" title="Playlists"><svg viewBox="0 0 24 24"><path d="M3 6h18v2H3zm0 5h18v2H3zm0 5h18v2H3z"/></svg></button>
  <span class="logo">🎵 MusicBee <b>Web</b></span>
  <span class="sub">your library &amp; playlists, in the browser</span>
</header>
<div id="main">
  <aside id="sidebar">
    <input id="search" placeholder="Filter playlists…" autocomplete="off">
    <div id="pllist"></div>
    <div id="status">loading…</div>
  </aside>
  <section>
    <div id="thead">
      <div>#</div><div>Title</div><div class="h-artist">Artist</div><div class="h-album">Album</div><div style="text-align:right">Time</div>
    </div>
    <div id="tlist"><div id="empty">Pick a playlist on the left — double-click a track to play</div></div>
  </section>
</div>
<div id="scrim" class="hidden"></div>
<footer>
  <div class="frow-top">
  <div id="npinfo">
      <div id="nptitle">Nothing playing</div>
      <div id="npartist">—</div>
    </div>
  
  <div class="right">
      <span class="vicon"><svg viewBox="0 0 24 24"><path d="M3 9v6h4l5 5V4L7 9H3zm13.5 3a4.5 4.5 0 0 0-2.5-4v8a4.5 4.5 0 0 0 2.5-4z"/></svg></span>
      <div class="volwrap">
        <input type="range" id="vol" min="0" max="100" value="80">
        <div id="voltip" class="hide">80%</div>
      </div>
      <button id="vbtn" class="pbtn" title="Video: on - show video stage"><svg viewBox="0 0 24 24"><path d="M17 10.5V7c0-.55-.45-1-1-1H4c-.55 0-1 .45-1 1v10c0 .55.45 1 1 1h12c.55 0 1-.45 1-1v-3.5l4 4v-11l-4 4z"/></svg></button>
      <button id="eqbtn" class="pbtn" title="Equalizer"><svg viewBox="0 0 24 24"><path d="M7 18h2V6H7v12zM11 6h2v12h-2V6zm4 4h2v8h-2v-8z"/></svg></button>
    </div>
  </div>
<div class="ctr">
    <div class="btns">
      <button id="shufbtn" class="pbtn" title="Shuffle"><svg viewBox="0 0 24 24"><path d="M10.59 9.17L5.41 4 4 5.41l5.17 5.17 1.42-1.41zM14.5 4l2.04 2.04L4 18.59 5.41 20 17.96 7.46 20 9.5V4h-5.5zm.33 9.41l-1.41 1.41 3.13 3.13L14.5 20H20v-5.5l-2.04 2.04-3.13-3.13z"/></svg></button>
      <button id="prevbtn" class="pbtn" title="Previous"><svg viewBox="0 0 24 24"><path d="M6 6h2v12H6zm12 0v12l-9.5-6z"/></svg></button>
      <button id="playbtn" class="pbtn" title="Play / Pause"><svg id="ic-play" viewBox="0 0 24 24"><path d="M8 5v14l11-7z"/></svg><svg id="ic-pause" class="hide" viewBox="0 0 24 24"><path d="M6 5h4v14H6zm8 0h4v14h-4z"/></svg></button>
      <button id="nextbtn" class="pbtn" title="Next"><svg viewBox="0 0 24 24"><path d="M16 6h2v12h-2zM6 6v12l9.5-6z"/></svg></button>
      <button id="repbtn" class="pbtn" title="Repeat: off"><svg viewBox="0 0 24 24"><path d="M7 7h10v3l4-4-4-4v3H5v6h2V7zm10 10H7v-3l-4 4 4 4v-3h12v-6h-2v4z"/></svg><span class="rbadge hide">1</span></button>
    </div>
    <div class="seekrow">
      <time id="tcur">0:00</time>
      <div class="seekwrap" id="seekwrap">
        <input type="range" id="seek" min="0" max="1000" value="0">
        <div id="seektip" class="hide">0:00</div>
      </div>
      <time id="tdur">0:00</time>
    </div>
  </div>
  
</footer>
<div id="eqpanel" class="hidden">
  <div class="eq-head">
    <span class="eq-title">Equalizer</span>
    <select id="eqpreset"></select>
    <label class="chk"><input type="checkbox" id="eqon" checked> on</label>
  </div>
  <div class="eq-faders" id="eqfaders"></div>
</div>
<div id="stage" class="hidden">
  <div class="stage-head">
    <span id="stagenm"></span>
    <button id="stageclose" title="Hide video (keeps playing)">✕</button>
  </div>
  <div class="stagebox"><video id="media" controls playsinline preload="auto"></video></div>
</div>
<div id="toast"></div>
<script>
const $=id=>document.getElementById(id);
const media=$('media');
let playlists=[], tracks=[], queue=[], pos=-1, shuffle=false, repeat=0, videoOn=true, seeking=false;
const IS_TOUCH = window.matchMedia && matchMedia('(pointer:coarse)').matches;

function fmt(s){ if(s==null||isNaN(s)) return '–:––'; s=Math.max(0,Math.round(s));
  const m=Math.floor(s/60), ss=s%60; return m+':'+String(ss).padStart(2,'0'); }
function toast(msg){ const t=$('toast'); t.textContent=msg; t.classList.add('show');
  clearTimeout(t._h); t._h=setTimeout(()=>t.classList.remove('show'),2600); }
function esc(s){ return String(s).replace(/[&<>"']/g, m=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[m])); }

/* ---------- equalizer (Web Audio) ---------- */
const EQBANDS=[31,62,125,250,500,1000,2000,4000,8000,16000];
const EQLABELS=['31','62','125','250','500','1K','2K','4K','8K','16K'];
const PRESETS={
  FLAT:[0,0,0,0,0,0,0,0,0,0],
  ROCK:[5,4,3,1,-1,-1,1,3,4,5],
  POP:[-2,2,4,5,4,2,0,-1,-2,-3],
  JAZZ:[4,3,1,0,-2,-2,0,1,2,3],
  CLASSICAL:[5,4,3,2,-2,-2,0,2,3,4],
  DANCE:[6,5,3,0,-2,-2,0,2,4,5],
  'BASS BOOST':[8,7,5,2,0,0,0,0,0,0],
  'LO-FI':[-1,0,1,2,3,2,0,-3,-6,-8],
  VOCAL:[-2,-1,0,2,4,5,4,2,0,-2]
};
let actx=null, eqFilters=[], mediaSrc=null;
let eqOn=true, curPreset='FLAT', pendingGains=PRESETS.FLAT.slice();

function ensureGraph(){
  if(actx) return;
  const AC=window.AudioContext||window.webkitAudioContext;
  if(!AC) return;
  actx=new AC();
  actx.addEventListener('statechange',wakeAudio);
  mediaSrc=actx.createMediaElementSource(media);
  let node=mediaSrc;
  for(const f of EQBANDS){
    const b=actx.createBiquadFilter();
    b.type=(f===31)?'lowshelf':(f===16000?'highshelf':'peaking');
    b.frequency.value=f; if(b.type==='peaking') b.Q.value=1;
    node.connect(b); eqFilters.push(b); node=b;
  }
  node.connect(actx.destination);
  applyGains();
}
function applyGains(){
  const g=eqOn?pendingGains:[0,0,0,0,0,0,0,0,0,0];
  eqFilters.forEach((f,i)=>{ f.gain.value=g[i]; });
}
function syncFaders(){
  document.querySelectorAll('.fader input').forEach((el,i)=>{ el.value=pendingGains[i]; sliderFill(el); });
  $('eqpreset').value=PRESETS[curPreset]?curPreset:'CUSTOM';
}
function setPreset(name){ curPreset=name; pendingGains=PRESETS[name].slice(); syncFaders(); applyGains(); saveEq(); }
function saveEq(){ try{ localStorage.setItem('mbweb.eq.v1',
  JSON.stringify({on:eqOn, preset:curPreset, gains:pendingGains})); }catch(e){} }

(function initEq(){
  const sel=$('eqpreset');
  for(const k of Object.keys(PRESETS)){
    const o=document.createElement('option'); o.value=k; o.textContent=k; sel.appendChild(o);
  }
  const oc=document.createElement('option'); oc.value='CUSTOM'; oc.textContent='Custom';
  sel.appendChild(oc);
  const fw=$('eqfaders');
  EQBANDS.forEach((f,i)=>{
    const d=document.createElement('div'); d.className='fader';
    d.innerHTML='<div class="slot"><input type="range" min="-12" max="12" step="0.5" value="0"></div><span class="fl">'+EQLABELS[i]+'</span>';
    d.querySelector('input').addEventListener('input',e=>{
      pendingGains[i]=parseFloat(e.target.value); curPreset='CUSTOM';
      syncFaders(); applyGains(); saveEq(); sliderFill(e.target);
    });
    fw.appendChild(d);
  });
  sel.addEventListener('change',()=>{ if(sel.value!=='CUSTOM') setPreset(sel.value); });
  $('eqon').addEventListener('change',e=>{ eqOn=e.target.checked; applyGains(); saveEq(); });
  try{
    const s=JSON.parse(localStorage.getItem('mbweb.eq.v1')||'null');
    if(s && Array.isArray(s.gains) && s.gains.length===10){
      eqOn=!!s.on; pendingGains=s.gains.map(Number);
      curPreset=(s.preset&&PRESETS[s.preset])?s.preset:'CUSTOM';
    }
  }catch(e){}
  $('eqon').checked=eqOn; syncFaders();
  $('eqbtn').onclick=()=>{ const p=$('eqpanel'); p.classList.toggle('hidden');
    $('eqbtn').classList.toggle('on', !p.classList.contains('hidden')); };
})();

/* ---------- video stage ---------- */
function showStage(){ $('stage').classList.remove('hidden'); }
function hideStage(){ $('stage').classList.add('hidden'); }
$('stageclose').onclick=hideStage;

/* ---------- mobile playlist drawer ---------- */
const drawer=$('sidebar');
function setDrawer(open){ drawer.classList.toggle('open',!!open);
  $('scrim').classList.toggle('hidden',!open); }
$('menubtn').onclick=()=>setDrawer(!drawer.classList.contains('open'));
$('scrim').onclick=()=>setDrawer(false);
if(IS_TOUCH) $('empty').textContent='Tap the menu button (top-left) to pick a playlist, then tap a track to play';
$('stage').addEventListener('click',e=>{ if(e.target===$('stage')) hideStage(); });

async function loadPlaylists(){
  const r=await fetch('/api/playlists'); playlists=await r.json(); renderPls();
}
function renderPls(){
  const q=$('search').value.toLowerCase();
  $('pllist').innerHTML='';
  for(const p of playlists){
    if(q && !p.name.toLowerCase().includes(q)) continue;
    const d=document.createElement('div');
    d.className='pl'+(curName===p.name?' active':'');
    let ct=p.count?p.count+' tracks':'';
    if(p.smart) ct='<span class="badge smart">smart</span>';
    else if(p.missing) ct+=` <span class="badge miss">${p.missing} missing</span>`;
    d.innerHTML=`<span class="nm"></span><span class="ct">${ct}</span>`;
    d.querySelector('.nm').textContent=p.name;
    if(!p.smart){ d.onclick=()=>openPlaylist(p.name); }
    else { d.title='Smart/auto playlist – only playable inside MusicBee';
           d.onclick=()=>toast('Auto-playlists can\u2019t be resolved outside MusicBee'); }
    $('pllist').appendChild(d);
  }
}
let curName=null;
async function openPlaylist(name){
  curName=name; renderPls();
  const r=await fetch('/api/tracks?playlist='+encodeURIComponent(name));
  if(!r.ok){ toast('Could not load playlist'); return; }
  tracks=await r.json(); queue=tracks.map((_,i)=>i); pos=-1;
  const list=$('tlist'); list.innerHTML='';
  tracks.forEach((t,i)=>{
    const row=document.createElement('div');
    row.className='trow'+' '+(t.exists?'':'missing');
    row.dataset.i=i;
    row.innerHTML=`<div class="cell dim" id="ic${i}">${i+1}</div>
      <div class="cell"></div><div class="cell dim col-artist"></div>
      <div class="cell dim col-album"></div>
      <div class="cell dim" style="text-align:right">${t.dur!=null?fmt(t.dur):''}${t.ok?'':' ⚠'}</div>`;
    const c=row.children;
    c[1].innerHTML=(t.kind==='video'?'<span class="vtag">VIDEO</span>':'')
      +(t.exists?'':'⚠ ')+esc(t.title)+(t.ok?'':`  [${t.ext} - not supported in browsers]`);
    c[2].textContent=t.artist; c[3].textContent=t.album;
    if(IS_TOUCH){ row.onclick=()=>playAt(i); }
    else { row.onclick=()=>{ document.querySelectorAll('.trow.sel').forEach(x=>x.classList.remove('sel'));
      row.classList.add('sel'); };
      row.ondblclick=()=>playAt(i); }
    list.appendChild(row);
  });
  setDrawer(false);
}
function playAt(i){
  const t=tracks[i]; if(!t) return;
  if(!t.exists){ toast('File not found on disk: '+t.title); return; }
  if(!t.ok){ toast(t.ext.toUpperCase()+' files can\u2019t be played in browsers (try MP3/M4A/FLAC/OGG/WAV/MP4)'); return; }
  ensureGraph(); if(actx && actx.state==='suspended') actx.resume();
  pos=i; media.src=t.url; media.play().catch(()=>{});
  $('stagenm').textContent=(t.artist?t.artist+' \u2014 ':'')+t.title;
  if(t.kind==='video' && videoOn) showStage(); else hideStage();
  markPlaying();
}
function markPlaying(){
  document.querySelectorAll('.trow').forEach(r=>{
    const i=+r.dataset.i;
    r.classList.toggle('playing', i===pos);
    r.classList.toggle('sel', i===pos);
    r.children[0].innerHTML=(i===pos)?'<span class="eq"><i></i><i></i><i></i></span>':(i+1);
  });
  if(pos>=0){ const t=tracks[pos];
    $('nptitle').textContent=t.title; $('npartist').textContent=t.artist||'—'; }
}
function next(){ if(!tracks.length) return;
  let n;
  if(shuffle && tracks.length>1){ do{ n=Math.floor(Math.random()*tracks.length);}while(n===pos); }
  else { n=(pos+1)%tracks.length; }
  playAt(n);
}
function prev(){ if(!tracks.length) return;
  if(media.currentTime>3){ media.currentTime=0; return; }
  playAt(shuffle?Math.floor(Math.random()*tracks.length):(pos-1+tracks.length)%tracks.length); }

function onEnded(){
  if(repeat===1){ media.currentTime=0; media.play().catch(()=>{}); return; }
  if(!shuffle && repeat!==2 && pos>=tracks.length-1){
    media.pause(); media.currentTime=0; $('seek').value=0; $('tcur').textContent='0:00';
    toast('End of playlist'); return; }
  next();
}
media.addEventListener('ended',onEnded);
function sliderFill(el){ const min=+el.min||0, max=+el.max||100;
  el.style.setProperty('--fill',((+el.value-min)/(max-min)*100)+'%'); }
media.addEventListener('loadedmetadata',()=>{ $('seek').max=Math.floor(media.duration||0);
  $('tdur').textContent=fmt(media.duration); sliderFill($('seek')); });
media.addEventListener('timeupdate',()=>{ if(!seeking){ $('seek').value=media.currentTime; sliderFill($('seek')); }
  $('tcur').textContent=fmt(media.currentTime); });
media.addEventListener('error',()=>{ if(media.src) toast('Playback error – file missing or unsupported format'); });

$('playbtn').onclick=()=>{ if(!media.src){ if(queue.length) playAt(queue[0]); return; }
  ensureGraph(); if(actx && actx.state==='suspended') actx.resume();
  media.paused?media.play():media.pause(); };
media.addEventListener('play',()=>{ $('ic-play').classList.add('hide');
  $('ic-pause').classList.remove('hide'); wakeAudio(); });
media.addEventListener('pause',()=>{ $('ic-play').classList.remove('hide');
  $('ic-pause').classList.add('hide'); });
$('nextbtn').onclick=next; $('prevbtn').onclick=prev;
$('shufbtn').onclick=function(){ shuffle=!shuffle; this.classList.toggle('on',shuffle);
  toast(shuffle?'Shuffle on':'Shuffle off'); };
const REP_LABELS=['off','one','all'];
function setRepeatUI(){ const b=$('repbtn'); b.classList.toggle('on',repeat>0);
  b.querySelector('.rbadge').classList.toggle('hide',repeat!==1);
  b.title='Repeat: '+REP_LABELS[repeat]; }
$('repbtn').onclick=function(){ repeat=(repeat+1)%3; setRepeatUI();
  toast('Repeat '+REP_LABELS[repeat]);
  try{ localStorage.setItem('mbweb.repeat',String(repeat)); }catch(e){} };
try{ const r=parseInt(localStorage.getItem('mbweb.repeat'),10); if(r===1||r===2) repeat=r; }catch(e){}
setRepeatUI();
function setVideoUI(){ $('vbtn').classList.toggle('on',videoOn);
  $('vbtn').title=videoOn?'Video: on - show video stage':'Video: off - audio only'; }
$('vbtn').onclick=function(){ videoOn=!videoOn; setVideoUI();
  if(!videoOn) hideStage();
  else if(pos>=0 && tracks[pos] && tracks[pos].kind==='video') showStage();
  toast(videoOn?'Video display on':'Audio-only - video hidden');
  try{ localStorage.setItem('mbweb.video',videoOn?'1':'0'); }catch(e){} };
try{ if(localStorage.getItem('mbweb.video')==='0') videoOn=false; }catch(e){}
setVideoUI();
/* iOS suspends the Web Audio graph when the app is backgrounded (screen
   locked), which mutes playback even though the <audio> element keeps
   running. wakeAudio() brings the EQ chain back to life whenever we can. */
function wakeAudio(){ if(actx && actx.state==='suspended' && !media.paused)
  actx.resume().catch(()=>{}); }
document.addEventListener('visibilitychange',wakeAudio);
document.addEventListener('resume',wakeAudio);   /* iOS unlock event */
window.addEventListener('pageshow',wakeAudio);

/* ---------- lock-screen / notification controls (phones) ---------- */
if('mediaSession' in navigator){
  try{
    const wake=()=>{ if(actx && actx.state!=='running') actx.resume().catch(()=>{}); };
    navigator.mediaSession.setActionHandler('play',()=>{ if(media.src){ wake(); media.play(); } });
    navigator.mediaSession.setActionHandler('pause',()=>media.pause());
    navigator.mediaSession.setActionHandler('previoustrack',prev);
    navigator.mediaSession.setActionHandler('nexttrack',next);
  }catch(e){}
  media.addEventListener('play',()=>{ const t=tracks[pos];
    if(t) try{ navigator.mediaSession.metadata=new MediaMetadata(
      {title:t.title, artist:t.artist||'', album:t.album||''}); }catch(e){} });
}
$('seek').addEventListener('input',e=>{ seeking=true; media.currentTime=+e.target.value; sliderFill(e.target); });
$('seek').addEventListener('change',()=>{ seeking=false; });
const seekwrap=$('seekwrap');
$('seek').addEventListener('pointermove',e=>{ if(!media.duration){ $('seektip').classList.add('hide'); return; }
  const r=seekwrap.getBoundingClientRect();
  let f=(e.clientX-r.left)/r.width; f=Math.max(0,Math.min(1,f));
  $('seektip').style.left=(f*100)+'%';
  $('seektip').textContent=fmt(f*(media.duration||0));
  $('seektip').classList.remove('hide'); });
$('seek').addEventListener('pointerleave',()=>$('seektip').classList.add('hide'));
let volTipT=null;
function showVolTip(v){ const t=$('voltip'); t.textContent=v+'%'; t.classList.remove('hide');
  clearTimeout(volTipT); volTipT=setTimeout(()=>t.classList.add('hide'),1000); }
$('vol').addEventListener('input',e=>{ media.volume=e.target.value/100; sliderFill(e.target);
  showVolTip(e.target.value);
  try{ localStorage.setItem('mbweb.vol.v1',String(e.target.value)); }catch(err){} });
(function initVol(){ let v=80;
  try{ const s=parseInt(localStorage.getItem('mbweb.vol.v1'),10);
    if(!isNaN(s)) v=Math.max(0,Math.min(100,s)); }catch(e){}
  media.volume=v/100; $('vol').value=v; sliderFill($('vol')); $('voltip').textContent=v+'%'; })();
$('search').addEventListener('input',renderPls);
document.addEventListener('keydown',e=>{
  if(e.key==='Escape'){ if(!$('stage').classList.contains('hidden')) hideStage(); else setDrawer(false); return; }
  if(e.target.tagName==='INPUT' && e.target.id!=='seek') return;
  if(e.code==='Space'){ e.preventDefault(); $('playbtn').click(); }
  else if(e.key==='ArrowRight'){ media.currentTime=Math.min((media.duration||0),media.currentTime+10); }
  else if(e.key==='ArrowLeft'){ media.currentTime=Math.max(0,media.currentTime-10); }
});

loadPlaylists().then(()=>{
  const s=$('status');
  const tot=playlists.reduce((a,p)=>a+(p.count||0),0);
  s.textContent=playlists.length+' playlists · '+tot+' entries';
});
</script>
</body>
</html>
"""

# --------------------------------------------------------------------------

def _home_dirs():
    """Best-effort list of user home directories (robust to odd shells)."""
    homes = []
    for var in ('USERPROFILE', 'HOME'):
        v = os.environ.get(var)
        if v:
            homes.append(v)
    hd, hp = os.environ.get('HOMEDRIVE'), os.environ.get('HOMEPATH')
    if hd and hp:
        homes.append(hd + hp)
    for base in ('C:\\Users', 'D:\\Users'):
        if os.path.isdir(base):
            for u in os.listdir(base):
                p = os.path.join(base, u)
                if os.path.isdir(p) and not u.startswith(('Public', 'Default')):
                    homes.append(p)
    seen, out = set(), []
    for h in homes:
        h = os.path.normpath(h)
        if h not in seen:
            seen.add(h)
            out.append(h)
    return out


def find_default_data_dir():
    cands = []
    appdata = os.environ.get('APPDATA')
    if appdata:
        cands.append(os.path.join(appdata, 'MusicBee'))
    for h in _home_dirs():
        cands.append(os.path.join(h, 'Music', 'MusicBee'))
    for c in cands:
        if os.path.isdir(os.path.join(c, 'Playlists')):
            return c
    # fall back to first candidate that at least exists
    for c in cands:
        if os.path.isdir(c):
            return c
    return cands[0] if cands else os.path.join(os.getcwd(), 'MusicBee')


def restricted_locations():
    """Absolute paths that must never be used as (or contain) a music root."""
    locs = [os.environ.get('SystemRoot') or r'C:\Windows',
            r'C:\Program Files', r'C:\Program Files (x86)', r'C:\ProgramData']
    for base in (r'C:\Users', r'D:\Users'):
        if os.path.isdir(base):
            for u in os.listdir(base):
                p = os.path.join(base, u)
                if os.path.isdir(p) and not u.startswith(('Public', 'Default')):
                    locs.append(p)
    return [os.path.normcase(os.path.normpath(p)) for p in locs if os.path.isdir(p)]


def is_sensitive_root(root):
    """True if root IS a sensitive location, or contains one inside it."""
    r = os.path.normcase(os.path.normpath(root)).rstrip(os.sep)
    return any(s == r or s.startswith(r + os.sep) for s in restricted_locations())


def main():
    ap = argparse.ArgumentParser(description='MusicBee Web player')
    ap.add_argument('--port', type=int, default=None, help='override CONFIG PORT')
    ap.add_argument('--host', default=None, help='override CONFIG HOST')
    ap.add_argument('--data-dir', default=None, help='override CONFIG DATA_DIR')
    ap.add_argument('--playlists-dir', default=None, help='override CONFIG PLAYLISTS_DIR')
    ap.add_argument('--music-root', nargs='+', default=None,
                    help='override CONFIG MUSIC_ROOTS (one or more folders)')
    ap.add_argument('--auth-password', default=None,
                    help='require this shared password (overrides CONFIG AUTH_PASSWORD)')
    args = ap.parse_args()

    # command-line arguments win over the CONFIG section; None -> auto-detect
    host = args.host or HOST
    port = args.port if args.port is not None else PORT
    data_dir = os.path.abspath(args.data_dir or DATA_DIR or find_default_data_dir())
    playlists_dir = os.path.abspath(args.playlists_dir or PLAYLISTS_DIR
                                    or os.path.join(data_dir, 'Playlists'))
    if args.music_root:
        music_roots = [os.path.abspath(r) for r in args.music_root]
    elif MUSIC_ROOTS:
        music_roots = [os.path.abspath(r) for r in MUSIC_ROOTS]
    else:
        # guess: parent of the data dir if it looks like <root>\MusicBee
        parent = os.path.dirname(data_dir)
        if os.path.basename(data_dir).lower() == 'musicbee' and \
                any(os.path.splitext(f)[1].lower() in AUDIO_EXTS
                    for f in (os.listdir(parent) if os.path.isdir(parent) else [])):
            music_roots = [parent]
        else:
            hs = _home_dirs()
            music_roots = ([os.path.join(hs[0], 'Music')] if hs
                           else [os.path.join(os.getcwd(), 'Music')])

    # safety check: refuse roots that would expose sensitive folders over HTTP
    if ENFORCE_SAFE_DIRS:
        safe, blocked = [], []
        for r in music_roots:
            nr = os.path.normpath(r)
            bare_drive = len(nr) == 3 and nr[1] == ':' and nr[2] == os.sep
            if is_sensitive_root(r):
                blocked.append((r, 'it would expose system or user folders'))
            elif bare_drive:
                blocked.append((r, 'a whole drive is too broad - point at a music '
                                    'folder inside it'))
            else:
                safe.append(r)
        for r, why in blocked:
            print(f"[security] cannot use '{r}' as a music root - for security "
                  f"reasons this directory is blocked ({why}).")
            print("           Please relocate your music to a dedicated folder and")
            print("           point MUSIC_ROOTS at it.")
            print("           (Set ENFORCE_SAFE_DIRS = False in the config section to allow.)")
        if not safe:
            print("[error] no usable music roots remain - aborting.")
            sys.exit(1)
        music_roots = safe

    auth_password = args.auth_password if args.auth_password is not None else AUTH_PASSWORD

    print("MusicBee Web")
    print(f"  data dir     : {data_dir}")
    print(f"  playlists    : {playlists_dir}")
    for r in music_roots:
        print(f"  music root   : {r}")
    if not os.path.isdir(playlists_dir):
        print("[warn] playlists dir not found - check PLAYLISTS_DIR in the config "
              "section or --playlists-dir")

    mbw = MusicBeeWeb(data_dir, playlists_dir, music_roots)
    Handler.mbw = mbw
    Handler.allowed_roots = mbw.allowed_roots()
    Handler.auth_password = (auth_password or '').strip()

    ThreadingHTTPServer.allow_reuse_address = True
    try:
        httpd = ThreadingHTTPServer((host, port), Handler)
    except OSError as e:
        print(f"[error] could not bind {host}:{port} - {e}")
        sys.exit(1)

    host_show = 'localhost' if host in ('127.0.0.1', '::1') else host
    print(f"\n  Now playing from:  http://{host_show}:{port}\n")
    if Handler.auth_password:
        print("  [auth] shared-password mode is ON - browsers will show a login prompt")
    if host not in ('127.0.0.1', '::1'):
        print("  [note] bound to all interfaces - anyone on your network can access it.")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")


if __name__ == '__main__':
    main()
