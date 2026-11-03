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

from core import APP_NAME, APP_VERSION, Job, backup_one

# 仅本机可访问，单一活动任务
_STATE = {'job': None, 'thread': None, 'server': None, 'lan': False}


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
                     'icon-192.png': 'image/png', 'icon-512.png': 'image/png'}
            if name in allow:
                fp = os.path.join(static_dir(), name)
                try:
                    with open(fp, 'rb') as f:
                        return self._send(200, f.read(), allow[name])
                except OSError:
                    pass
            return self._send(404, {'error': 'not found'})
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
        if self.path == '/api/quit':
            self._send(200, {'ok': True})
            threading.Timer(0.4, _quit).start()
            return
        return self._send(404, {'error': 'not found'})


def _run_job(target, args, job):
    try:
        backup_one(target, args, job)
    except BaseException as e:  # 状态已记入 job，线程不抛栈
        job.log(f'任务结束（{job.state}）: {e}' if job.state == 'error' else f'任务结束（{job.state}）')


def _quit():
    srv = _STATE.get('server')
    if srv:
        threading.Thread(target=srv.shutdown, daemon=True).start()
    remove_instance_file()
    os._exit(0)


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
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass


if __name__ == '__main__':
    main()
