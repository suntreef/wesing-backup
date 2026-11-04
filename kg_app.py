#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""
kg_app.py — 全民K歌备份工具 · 图形界面入口
==========================================

启动一个仅监听本机的轻量服务器，并自动打开浏览器操作页面。
双击运行（或 PyInstaller 打包成 exe 后双击）即可使用，无需命令行。

用法：
    python kg_app.py            # 启动并打开浏览器
"""

import hashlib
import json
import os
import socket
import subprocess
import sys
import threading
import time
import types
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import datetime as dt

dt_date = dt.date

from core import APP_NAME, APP_VERSION, Job, backup_one, render_guest_page

# 仅本机可访问，单一活动任务
_STATE = {'job': None, 'thread': None, 'server': None, 'lan': False}


def _shares_file():
    return os.path.join(os.getcwd(), 'shares.json')


def load_shares():
    try:
        with open(_shares_file(), encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return {'dir': '', 'since': ''}


def save_shares(sh):
    try:
        with open(_shares_file(), 'w', encoding='utf-8') as f:
            json.dump(sh, f, ensure_ascii=False, indent=1)
    except Exception:
        pass


def share_entries():
    """读取当前分享目录的 index.json，返回 (entries_sorted, profile, share)。"""
    sh = load_shares()
    d = sh.get('dir') or ''
    if not d or not os.path.isdir(d):
        return [], {}, sh
    try:
        with open(os.path.join(d, 'index.json'), encoding='utf-8') as f:
            idx = json.load(f)
    except Exception:
        return [], {}, sh
    entries = sorted(idx.get('works', {}).values(), key=lambda x: x.get('seq') or 0)
    return entries, idx.get('profile', {}), sh


def _uk(e):
    return e.get('ugcid') or ('s' + str(e.get('seq')))


def _pick_for_date(entries, day):
    """确定性盲盒：同一日期永远抽中同一首。"""
    if not entries:
        return None
    seed = hashlib.md5((day + '|wesing-blindbox|' + str(len(entries))).encode()).hexdigest()
    return entries[int(seed, 16) % len(entries)]


def blindbox_state():
    """返回 (entries, profile, unlocked_keys, history, today_keys, today_date, festival)。"""
    entries, profile, sh = share_entries()
    if not entries:
        return entries, profile, set(), [], None, ''
    start_file = os.path.join(sh.get('dir') or '.', 'guest_start')
    today = os.environ.get('KG_FAKE_DATE') or dt_date.today().isoformat()
    if not os.path.exists(start_file):
        try:
            with open(start_file, 'w') as f:
                f.write(today)
        except Exception:
            pass
    try:
        with open(start_file, encoding='utf-8') as f:
            start = f.read().strip() or today
    except Exception:
        start = today
    history, unlocked = [], set()
    d = dt_date.fromisoformat(start)
    end = dt_date.fromisoformat(today)
    guard = 0
    while d <= end and guard < 3650:
        day = d.isoformat()
        fest = __import__('core').get_festival(day)
        picks = [_pick_for_date(entries, day)]
        if fest and fest.get('double'):
            picks.append(_pick_for_date(entries, day + '|bonus'))
        for pi, pick in enumerate(picks):
            if pick is None:
                continue
            unlocked.add(_uk(pick))
            history.append({'date': day, 'title': pick.get('title') or '',
                            'singer': pick.get('singer_name') or '', 'uk': _uk(pick),
                            'festival': (fest['name'] + (pi > 0 and '加更' or '')) if fest and fest.get('double') else (fest['name'] if fest else '')})
        d += dt.timedelta(days=1)
        guard += 1
    today_pick = _pick_for_date(entries, today)
    today_keys = {_uk(today_pick)} if today_pick else set()
    festival = __import__('core').get_festival(today)
    # 节日双倍：加更歌也标记为 today 并加入解锁集
    if festival and festival.get('double') and today_pick is not None:
        bonus = _pick_for_date(entries, today + '|bonus')
        if bonus is not None:
            unlocked.add(_uk(bonus))
            today_keys.add(_uk(bonus))
    return entries, profile, unlocked, history, today_key, today, festival


def lan_ips():
    """枚举本机局域网 IP（优先默认路由出口）。"""
    ips = []
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(('8.8.8.8', 80))
        ips.append(s.getsockname()[0])
        s.close()
    except Exception:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if ip not in ips and not ip.startswith('127.') and ':' not in ip:
                ips.append(ip)
    except Exception:
        pass
    return ips


def rebind(lan_on):
    """切换监听范围：仅本机 ↔ 局域网。返回 (port, lan_ips)。"""
    old = _STATE['server']
    port = old.server_address[1]
    host = '0.0.0.0' if lan_on else '127.0.0.1'
    old.shutdown()
    old.server_close()  # 不关监听 socket 的话，Windows 上新连接会被旧 socket 拦下（SYN 被拒）
    srv = ThreadingHTTPServer((host, port), Handler)
    _STATE['server'] = srv
    _STATE['lan'] = lan_on
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv.server_address[1], lan_ips()


def _instance_file():
    base = os.environ.get('LOCALAPPDATA') or os.path.expanduser('~')
    return os.path.join(base, 'KG-Backup', 'instance.json')


def find_running_instance():
    """检测已有实例（防止重复双击 exe 叠开一堆窗口）。返回已运行实例的端口或 None。"""
    try:
        with open(_instance_file(), encoding='utf-8') as f:
            info = json.load(f)
        port = int(info['port'])
        with urllib.request.urlopen(f'http://127.0.0.1:{port}/api/status', timeout=2) as r:
            j = json.load(r)
        if isinstance(j, dict) and j.get('app'):
            return port
    except Exception:
        pass
    return None


def write_instance_file(port):
    try:
        os.makedirs(os.path.dirname(_instance_file()), exist_ok=True)
        with open(_instance_file(), 'w', encoding='utf-8') as f:
            json.dump({'port': port, 'pid': os.getpid(), 'version': APP_VERSION}, f)
    except Exception:
        pass


def remove_instance_file():
    try:
        os.remove(_instance_file())
    except Exception:
        pass


def static_dir():
    if getattr(sys, 'frozen', False):  # PyInstaller 打包
        return os.path.join(sys._MEIPASS, 'static')
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), 'static')


def build_args(p):
    """把前端 JSON 参数映射为 backup_one 需要的 args 命名空间。"""
    return types.SimpleNamespace(
        out=str(p.get('out') or '').strip(),
        cookie=str(p.get('cookie') or '').strip(),
        cookie_file='',
        proxy=str(p.get('proxy') or '').strip(),
        workers=max(1, min(8, int(p.get('workers') or 3))),
        page_sleep=0.8, retries=3, timeout=30,
        max=max(0, int(p.get('max') or 0)),
        since=str(p.get('since') or '').strip(),
        until=str(p.get('until') or '').strip(),
        no_audio=False, no_cover=False, no_comments=False, no_meta=False, no_html=False,
        no_tags=not bool(p.get('tags', True)),
        no_lyric=not bool(p.get('lyric', True)),
        video=bool(p.get('video')), mp3=bool(p.get('mp3')),
        zip=bool(p.get('zip')), list_only=bool(p.get('list_only')),
        flat=bool(p.get('flat')),
        force=bool(p.get('force')),
    )


def open_with_system(path):
    try:
        if os.name == 'nt':
            os.startfile(path)  # noqa
        elif sys.platform == 'darwin':
            subprocess.Popen(['open', path])
        else:
            subprocess.Popen(['xdg-open', path])
        return True
    except Exception:
        return False


class Handler(BaseHTTPRequestHandler):
    server_version = f'KGBackup/{APP_VERSION}'  # HTTP 头仅允许 latin-1，中文放不进去

    def log_message(self, fmt, *a):  # 静默访问日志
        pass

    # ---------- 工具 ----------
    def _send(self, code, body, ctype='application/json; charset=utf-8'):
        data = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(data)

    def _body(self):
        n = int(self.headers.get('Content-Length') or 0)
        if n <= 0:
            return {}
        return json.loads(self.rfile.read(n).decode('utf-8'))

    # ---------- GET ----------

    _MIME = {'.m4a': 'audio/mp4', '.mp4': 'video/mp4', '.jpg': 'image/jpeg',
             '.jpeg': 'image/jpeg', '.png': 'image/png', '.lrc': 'text/plain; charset=utf-8',
             '.html': 'text/html; charset=utf-8'}

    def _serve_guest_file(self, rel):
        """服务分享目录里的文件（含 Range 断点/拖动支持 + 盲盒锁定拦截）。"""
        try:
            rel = urllib.request.url2pathname(rel).replace('\\', '/').lstrip('/')
        except Exception:
            return self._send(400, b'bad path', 'text/plain; charset=utf-8')
        entries, profile, unlocked, history, today_keys, today_date, festival = blindbox_state()
        if not entries:
            return self._send(404, b'no share', 'text/plain; charset=utf-8')
        sh = load_shares()
        root = os.path.abspath(sh.get('dir') or '')
        full = os.path.abspath(os.path.join(root, rel))
        if not full.startswith(root + os.sep) and full != root:
            return self._send(403, b'forbidden', 'text/plain; charset=utf-8')
        ext = os.path.splitext(full)[1].lower()
        mime = self._MIME.get(ext)
        if not mime:
            return self._send(404, b'not found', 'text/plain; charset=utf-8')
        if ext in ('.m4a', '.mp4', '.jpg', '.jpeg'):
            base = os.path.basename(full)
            ok = False
            for e in entries:
                if _uk(e) not in unlocked:
                    continue
                if any(f and os.path.basename(f) == base for f in (e.get('audio'), e.get('cover'))):
                    ok = True
                    break
            if not ok:
                return self._send(403, b'locked', 'text/plain; charset=utf-8')
        if not os.path.exists(full):
            return self._send(404, b'not found', 'text/plain; charset=utf-8')
        size = os.path.getsize(full)
        rng = self.headers.get('Range')
        start, end = 0, size - 1
        partial = False
        if rng and rng.startswith('bytes='):
            try:
                spec = rng[6:].split(',')[0]
                a, _, b = spec.partition('-')
                if a:
                    start = int(a)
                    if b:
                        end = min(int(b), size - 1)
                elif b:
                    start = max(0, size - int(b))
                partial = True
            except ValueError:
                pass
        if start >= size or end < start:
            return self._send(416, b'range error', 'text/plain; charset=utf-8')
        self.send_response(206 if partial else 200)
        self.send_header('Content-Type', mime)
        self.send_header('Accept-Ranges', 'bytes')
        length = end - start + 1
        if partial:
            self.send_header('Content-Range', f'bytes {start}-{end}/{size}')
        self.send_header('Content-Length', str(length))
        self.end_headers()
        try:
            with open(full, 'rb') as f:
                f.seek(start)
                remain = length
                while remain > 0:
                    chunk = f.read(min(1 << 16, remain))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remain -= len(chunk)
        except (BrokenPipeError, ConnectionAbortedError):
            pass

    def do_GET(self):
        if self.path in ('/', '/index.html'):
            fp = os.path.join(static_dir(), 'index.html')
            try:
                with open(fp, 'rb') as f:
                    return self._send(200, f.read(), 'text/html; charset=utf-8')
            except OSError:
                return self._send(500, {'error': '找不到页面文件'})
        if self.path.startswith('/static/'):
            name = os.path.basename(self.path[len('/static/'):])
            allow = {'qrcode.min.js': 'application/javascript; charset=utf-8',
                     'manifest.json': 'application/manifest+json',
                     'guest-manifest.json': 'application/manifest+json',
                     'icon-192.png': 'image/png', 'icon-512.png': 'image/png'}
            if name in allow:
                fp = os.path.join(static_dir(), name)
                try:
                    with open(fp, 'rb') as f:
                        return self._send(200, f.read(), allow[name])
                except OSError:
                    pass
            return self._send(404, {'error': 'not found'})
        if self.path == '/api/authors':
            try:
                with open(os.path.join(os.getcwd(), 'authors.json'), encoding='utf-8') as f:
                    data = json.load(f)
            except Exception:
                data = {}
            authors = sorted(data.values(), key=lambda x: x.get('last') or '', reverse=True)
            return self._send(200, {'authors': authors})
        if self.path == '/api/share':
            entries, profile, unlocked, history, today_keys, today_date, festival = blindbox_state()
            ips = lan_ips() if _STATE.get('lan') else []
            port = _STATE['server'].server_address[1]
            gport = _STATE.get('guest_port') or 0
            gurl = f'http://{ips[0]}:{gport}/guest' if (ips and gport) else (f'http://{ips[0]}:{port}/guest' if ips else '')
            return self._send(200, {
                'dir': (load_shares().get('dir') or ''),
                'guestUrl': gurl, 'unlocked': len(unlocked), 'total': len(entries),
                'today': next((e.get('title') for e in entries if _uk(e) in today_keys), ''),
                'ips': ips, 'guestPort': gport,
                'authMode': (load_shares().get('auth') or {}).get('mode') or 'none',
                'festival': (festival or {}).get('name') or ''})
        if self.path == '/guest':
            entries, profile, unlocked, history, today_keys, today_date, festival = blindbox_state()
            if not entries:
                return self._send(200, '还没有可分享的备份目录。先在工具页跑一次备份，或通过 /api/share 设置目录。'
                                  .encode(), 'text/html; charset=utf-8')
            html = render_guest_page(entries, profile, unlocked, history, today_keys, today_date, festival)
            return self._send(200, html.encode(), 'text/html; charset=utf-8')
        if self.path.startswith('/g/'):
            return self._serve_guest_file(self.path[3:])
        if self.path.startswith('/api/status'):
            job = _STATE['job']
            snap = job.snapshot() if job else {'state': 'idle'}
            snap['running'] = bool(_STATE['thread'] and _STATE['thread'].is_alive())
            snap['app'] = {'name': APP_NAME, 'version': APP_VERSION,
                           'ffmpeg': __import__('core').ffmpeg_available()}
            snap['lan'] = _STATE.get('lan', False)
            snap['port'] = _STATE['server'].server_address[1]
            snap['lan_ips'] = lan_ips() if snap['lan'] else []
            return self._send(200, snap)
        if self.path.startswith('/api/open'):
            job = _STATE['job']
            what = (self.path.split('what=')[-1].split('&')[0] if 'what=' in self.path else 'folder')
            if not job or not job.out_dir:
                return self._send(400, {'error': '还没有备份任务'})
            base = os.path.abspath(job.out_dir)
            targets = {'folder': base, 'html': os.path.join(base, 'index.html'),
                       'csv': os.path.join(base, 'index.csv')}
            p = targets.get(what, base)
            if not os.path.exists(p):
                return self._send(400, {'error': f'文件不存在: {p}'})
            return self._send(200, {'ok': open_with_system(p)})
        return self._send(404, {'error': 'not found'})

    # ---------- POST ----------
    def do_POST(self):
        if self.path == '/api/start':
            if _STATE['thread'] and _STATE['thread'].is_alive():
                return self._send(409, {'error': '已有任务在进行中，请等待完成或先停止'})
            try:
                p = self._body()
                target = str(p.get('target') or '').strip()
                if not target:
                    return self._send(400, {'error': '请先填写个人主页链接或 uid'})
                args = build_args(p)
                job = Job()
                _STATE['job'] = job
                t = threading.Thread(target=_run_job, args=(target, args, job), daemon=True)
                _STATE['thread'] = t
                t.start()
                return self._send(200, {'ok': True})
            except Exception as e:
                return self._send(400, {'error': str(e)})
        if self.path == '/api/lan':
            p = self._body()
            port, ips = rebind(bool(p.get('on')))
            msg = '已开放局域网访问' if p.get('on') else '已恢复仅本机访问'
            return self._send(200, {'ok': True, 'lan': bool(p.get('on')), 'port': port,
                                    'lan_ips': ips, 'msg': msg})
        if self.path == '/api/stop':
            job = _STATE['job']
            if job:
                job.cancelled = True
            return self._send(200, {'ok': True})
        if self.path == '/api/check-cookie':
            try:
                p = self._body()
                from core import KGClient, parse_uid
                uid = parse_uid(str(p.get('target') or ''))
                client = KGClient(str(p.get('cookie') or '').strip(),
                                  str(p.get('proxy') or '').strip(), 20, 2)
                client.share_uid = uid
                d = client.get_work_page(uid, 1)
                return self._send(200, {
                    'ok': True, 'nickname': d.get('nickname', ''),
                    'count': d.get('ugc_total_count')})
            except Exception as e:
                return self._send(200, {'ok': False, 'error': str(e)})
        if self.path == '/api/pause':
            job = _STATE['job']
            if not job:
                return self._send(400, {'error': '没有进行中的任务'})
            p = self._body()
            job.paused = bool(p.get('paused'))
            job.log('⏸ 已暂停（进度保留，点“继续”接着跑）' if job.paused else '▶ 继续备份')
            return self._send(200, {'ok': True, 'paused': job.paused})
        if self.path == '/api/share':
            p = self._body()
            if 'dir' in p:
                d = str(p.get('dir') or '').strip()
                if d and os.path.isdir(d) and os.path.exists(os.path.join(d, 'index.json')):
                    sh = load_shares()
                    if not sh.get('since'):
                        import datetime as _dt
                        sh['since'] = _dt.date.today().isoformat()
                    sh['dir'] = d
                    save_shares(sh)
                    return self._send(200, {'ok': True, 'dir': d})
                return self._send(400, {'error': '目录不存在或还没有 index.json（先完成一次备份）'})
            if 'auth_mode' in p:
                sh = load_shares()
                sh['auth'] = {'mode': str(p.get('auth_mode') or 'none'),
                              'user': str(p.get('auth_user') or ''),
                              'pass': str(p.get('auth_pass') or '')}
                save_shares(sh)
                return self._send(200, {'ok': True, 'auth': sh['auth']['mode']})
            entries, profile, unlocked, history, today_keys, today_date, festival = blindbox_state()
            ips = lan_ips() if _STATE.get('lan') else []
            port = _STATE['server'].server_address[1]
            url = f'http://{ips[0]}:{port}/guest' if ips else ''
            return self._send(200, {
                'ok': True, 'dir': (load_shares().get('dir') or ''),
                'guestUrl': url, 'unlocked': len(unlocked), 'total': len(entries),
                'today': next((e.get('title') for e in entries if _uk(e) in today_keys), ''),
                'ips': ips})
        if self.path == '/api/upgrade':
            if _STATE['thread'] and _STATE['thread'].is_alive():
                return self._send(409, {'error': '已有任务在进行中'})
            p = self._body()
            d = str(p.get('dir') or '').strip()
            if not d or not os.path.exists(os.path.join(d, 'index.json')):
                return self._send(400, {'error': '目录不存在或没有 index.json'})
            job = Job()
            _STATE['job'] = job
            t = threading.Thread(target=_run_upgrade, args=(d, job), daemon=True)
            _STATE['thread'] = t
            t.start()
            return self._send(200, {'ok': True})
        if self.path == '/api/quit':
            self._send(200, {'ok': True})
            threading.Timer(0.4, _quit).start()
            return
        return self._send(404, {'error': 'not found'})


def _run_upgrade(d, job):
    try:
        from core import KGClient, upgrade_index_dir
        client = KGClient('', '', 30, 3)
        job.out_dir = d
        upgrade_index_dir(d, client, job)
        job.state = 'done'
        job.finished_at = __import__('core').ts2str(time.time())
        job.log('🆙 升级完成')
    except BaseException as e:
        job.state = 'error'
        job.error = str(e)


def _run_job(target, args, job):
    try:
        backup_one(target, args, job)
        if job.out_dir:
            try:
                save_author_preset(job.profile.get('uid') or '', job.profile.get('nickname') or '', job.out_dir)
            except Exception:
                pass
            sh = load_shares()
            sh['dir'] = os.path.abspath(job.out_dir)
            if not sh.get('since'):
                import datetime as _dt
                sh['since'] = _dt.date.today().isoformat()
            save_shares(sh)
    except BaseException as e:  # 状态已记入 job，线程不抛栈
        job.log(f'任务结束（{job.state}）: {e}' if job.state == 'error' else f'任务结束（{job.state}）')


def _quit():
    srv = _STATE.get('server')
    if srv:
        threading.Thread(target=srv.shutdown, daemon=True).start()
    remove_instance_file()
    os._exit(0)


class GuestHandler(Handler):
    """独立盲盒端口：只提供 /guest 与 /g/*，不暴露任何管理功能。"""

    def _auth_ok(self):
        cfg = load_shares().get('auth') or {}
        mode = cfg.get('mode') or 'none'
        if mode == 'none':
            return True
        import base64
        auth = self.headers.get('Authorization') or ''
        if not auth.startswith('Basic '):
            return False
        try:
            user, _, pwd = base64.b64decode(auth[6:]).decode('utf-8').partition(':')
        except Exception:
            return False
        if mode == 'user':
            return user == (cfg.get('user') or '')
        if mode == 'userpass':
            return user == (cfg.get('user') or '') and pwd == (cfg.get('pass') or '')
        return True

    def do_GET(self):
        if not self._auth_ok():
            self.send_response(401)
            self.send_header('WWW-Authenticate', 'Basic realm="KG Blindbox"')
            self.send_header('Content-Length', '0')
            self.end_headers()
            return
        if (self.path == '/guest' or self.path.startswith('/g/') or
                self.path in ('/static/guest-manifest.json', '/static/icon-192.png',
                              '/static/icon-512.png')):
            return Handler.do_GET(self)
        return self._send(404, b'not found', 'text/plain; charset=utf-8')

    def do_POST(self):
        return self._send(404, b'not found', 'text/plain; charset=utf-8')


def main():
    # 单实例守卫：已有实例在跑就直接打开它的页面，避免重复双击 exe 叠开一堆窗口
    port = 0
    lan = False
    host = '127.0.0.1'
    argv = sys.argv[1:]
    if '--port' in argv:
        port = int(argv[argv.index('--port') + 1])
    if '--lan' in argv:
        lan = True
    guest_port = int(os.environ.get('GUEST_PORT') or 0)
    if '--guest-port' in argv:
        guest_port = int(argv[argv.index('--guest-port') + 1])
    if '--host' in argv:
        host = argv[argv.index('--host') + 1]
        lan = host != '127.0.0.1'
    if port == 0:
        existing = find_running_instance()
        if existing:
            url = f'http://127.0.0.1:{existing}'
            if sys.stdout:
                print(f'{APP_NAME} 已在运行，直接打开页面: {url}', flush=True)
            if not os.environ.get('KG_NO_BROWSER'):
                webbrowser.open(url)
            return
    srv = ThreadingHTTPServer((host, port), Handler)
    _STATE['server'] = srv
    _STATE['lan'] = lan
    port = srv.server_address[1]
    write_instance_file(port)
    import atexit
    atexit.register(remove_instance_file)
    url = f'http://127.0.0.1:{port}'
    if sys.stdout:
        print(f'{APP_NAME} v{APP_VERSION} 已启动: {url} （关闭此窗口即退出）', flush=True)
        if lan:
            for ip in lan_ips():
                print(f'  📱 手机（同一Wi-Fi）访问: http://{ip}:{port}', flush=True)
    if not os.environ.get('KG_NO_BROWSER'):
        threading.Timer(0.8, webbrowser.open, args=(url,)).start()
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    if guest_port > 0:
        try:
            gsrv = ThreadingHTTPServer(('0.0.0.0', guest_port), GuestHandler)
            threading.Thread(target=gsrv.serve_forever, daemon=True).start()
            _STATE['guest_port'] = guest_port
            if sys.stdout:
                print(f'🎁 盲盒访客端口已开放: http://0.0.0.0:{guest_port}/guest', flush=True)
        except OSError as e:
            if sys.stdout:
                print(f'盲盒访客端口 {guest_port} 启动失败: {e}', flush=True)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass


if __name__ == '__main__':
    main()
