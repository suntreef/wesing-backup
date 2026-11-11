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
import io
import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
import types
import urllib.request
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import datetime as dt

dt_date = dt.date

from core import APP_NAME, APP_VERSION, Job, backup_one, render_guest_page

# 仅本机可访问，单一活动任务
_STATE = {'job': None, 'thread': None, 'server': None, 'lan': False, 'queue': []}


def _start_job(target, p):
    """启动一个备份任务线程（/api/start 与队列共用的入口）。"""
    args = build_args(p)
    job = Job()
    _STATE['job'] = job
    t = threading.Thread(target=_run_job, args=(target, args, job), daemon=True)
    _STATE['thread'] = t
    t.start()


def _queue_watch():
    """备份队列看护：空闲时自动弹出下一个任务（每 2.5 秒检查）。"""
    while True:
        time.sleep(2.5)
        try:
            q = _STATE.get('queue') or []
            if not q:
                continue
            alive = _STATE.get('thread') and _STATE['thread'].is_alive()
            if alive:
                continue
            item = q.pop(0)
            target = str(item.get('target') or '').strip()
            if not target:
                continue
            _start_job(target, item)
        except Exception:
            pass


def _shares_file():
    return os.path.join(data_root(), 'shares.json')


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


def share_entries(scope='pool'):
    """读取盲盒池的 index.json，返回 (entries, profile, share)。

    scope='pool'：盲盒池 = shares.pool_dirs（多作者目录，管理端勾选）若已设置，否则只用 shares.dir。
    scope='all' ：池 + 数据根目录下所有有效备份目录（历史掉落/加赠永久解锁的数据来源）。
    合并时每首作品打上 _pool（来源目录）标记；seq 全池重编，保证前端编号稳定；
    昵称优先用 shares.nick（机主自定义），否则取第一个有效作者的资料。"""
    sh = load_shares()
    dirs, seen = [], set()

    def _add(d):
        if not d:
            return
        ap = os.path.abspath(d)
        if ap not in seen and os.path.isdir(ap) and os.path.exists(os.path.join(ap, 'index.json')):
            seen.add(ap)
            dirs.append(ap)

    if scope == 'all':
        root = data_root()
        try:
            for name in sorted(os.listdir(root)):
                _add(os.path.join(root, name))
        except OSError:
            pass
    for d in (sh.get('pool_dirs') or []):
        _add(d)
    _add(sh.get('dir') or '')
    merged, profile = [], {}
    for d in dirs:
        if not d:
            continue
        try:
            with open(os.path.join(d, 'index.json'), encoding='utf-8') as f:
                idx = json.load(f)
        except Exception:
            continue
        if not profile:
            profile = idx.get('profile', {})
        # 老备份没有「收录时间」：用该目录音频文件的修改时间兜底（= 当初备份进盲盒的时间）
        for e in idx.get('works', {}).values():
            e = dict(e)
            e['_pool'] = d
            if not e.get('collected'):
                m = ''
                for f in (e.get('audio'), e.get('cover')):
                    if not f:
                        continue
                    p = os.path.join(d, f)
                    if os.path.isfile(p):
                        m = dt_date.fromtimestamp(os.path.getmtime(p)).isoformat()
                        break
                if m:
                    e['collected'] = m
            merged.append(e)
    merged.sort(key=lambda x: ((x.get('_pool') or ''), x.get('seq') or 0))
    for i, e in enumerate(merged, 1):
        e['seq'] = i
    if sh.get('nick'):
        profile = dict(profile or {})
        profile['nickname'] = str(sh['nick'])
    return merged, profile, sh


def _uk(e):
    return e.get('ugcid') or ('s' + str(e.get('seq')))


def _pick_for_date(entries, day, salt=''):
    """确定性盲盒：同一日期 + 同一身份永远抽中同一首。

    salt 是访客身份（账号名或浏览器 ID）：不同的人拿到的是完全不同的掉落序列，
    但每个人自己每天的记录依旧是稳定、可重算的。
    """
    if not entries:
        return None
    seed = hashlib.md5((day + '|wesing-blindbox|' + salt + '|' + str(len(entries))).encode()).hexdigest()
    return entries[int(seed, 16) % len(entries)]


# ------------------------------------------------ 访客身份（每人一套独立的盲盒进度）

_VISITOR_FILE = 'guest_visitors.json'
_VID_RE = re.compile(r'^[A-Za-z0-9_\-]{6,64}$')
# 内部身份是 'u:账号' / 'v:16位'，带冒号；仅在管理端内部使用（不来自 Cookie）。
# 账号名可以是中文（如 u:小猫），所以这里不能限 ASCII，只排除空白/引号/斜杠等危险字符
# （vid 只作访客数据 JSON 的键，不参与文件路径，放宽是安全的）
_VID_ANY_RE = re.compile(r'^[^\s<>"\'\\/]{3,120}$')


def _pick_distinct(pool, seed_base, salt, used, tries=80):
    """抽一首还没被解锁的歌（加赠开盒用，避免和别人/已解锁的撞车）。"""
    for i in range(tries):
        seed = seed_base if i == 0 else '%s|alt%d' % (seed_base, i)
        p = _pick_for_date(pool, seed, salt)
        if p is not None and _uk(p) not in used:
            return p
    for p in pool:
        if _uk(p) not in used:
            return p
    return None


def _visitor_file(share_dir):
    return _guest_data_file(share_dir, _VISITOR_FILE)


def visitors(share_dir):
    return _load_json_safe(_visitor_file(share_dir))


def save_visitors(share_dir, data):
    _save_json_safe(_visitor_file(share_dir), data)


def visitor_state(share_dir, vid):
    """取（必要时创建）某位访客自己的盲盒状态：开盒起点 + 播放次数。

    新访客一律从"今天"起算，所以看不到别人的掉落日历，也不会继承别人的随机序列。
    老版本只留了一份全局 guest_start：第一个到访的人继承它（老用户日历不被清空，
    且沿用旧随机种子 salt='' → 之前抽到的歌不变），之后来的人各自重新开始。
    """
    data = visitors(share_dir)
    st = data.get(vid)
    if isinstance(st, dict):
        return st, data
    today = os.environ.get('KG_FAKE_DATE') or dt_date.today().isoformat()
    st = {'start': today, 'plays': {}, 'bonus': 0, 'msgs': []}
    legacy_file = os.path.join(share_dir, 'guest_start')
    if not data and os.path.exists(legacy_file):
        try:
            with open(legacy_file, encoding='utf-8') as f:
                old = f.read().strip()
        except Exception:
            old = ''
        if old:
            st = {'start': old, 'plays': {}, 'legacy': True, 'bonus': 0, 'msgs': []}
    data[vid] = st
    save_visitors(share_dir, data)
    return st, data


def _today_str():
    """今天（测试时可用 KG_FAKE_DATE 替身）。"""
    return os.environ.get('KG_FAKE_DATE') or dt_date.today().isoformat()


def mark_opened(share_dir, vid, n=1):
    """记录"这位访客今天开盒进度"（n=已拆开的张数）：换浏览器/清 Cookie 也不会重开。"""
    try:
        n = max(1, int(n or 1))
    except Exception:
        n = 1
    st, data = visitor_state(share_dir, vid)
    opened = st.setdefault('opened', {})
    # 同一天取较大值（拆开只会越来越多）
    opened[_today_str()] = max(int(opened.get(_today_str()) or 0), n)
    # 只留最近 120 天，文件不无限膨胀
    st['opened'] = dict(sorted(opened.items())[-120:])
    data[vid] = st
    save_visitors(share_dir, data)


def opened_count(share_dir, vid):
    """今天已拆开的张数（0 = 今天还没开过）。"""
    if not share_dir or not vid:
        return 0
    try:
        st, _ = visitor_state(share_dir, vid)
        return int((st.get('opened') or {}).get(_today_str()) or 0)
    except Exception:
        return 0


def visitor_grants(share_dir, vid):
    """访客身上的「机主加赠」：还没拆的加赠歌数 + 未读消息。"""
    if not vid:
        return 0, []
    try:
        st, _ = visitor_state(share_dir, vid)
    except Exception:
        return 0, []
    gopened = set(st.get('grants_opened') or [])
    left = sum(1 for g in (st.get('grants') or []) if g.get('uk') not in gopened)
    msgs = [m for m in (st.get('msgs') or []) if not m.get('seen')]
    return left, msgs


def visitor_msg_history(share_dir, vid):
    """访客收到的全部机主消息（新→旧，含已读），供盲盒页消息中心展示。"""
    if not vid:
        return []
    try:
        st, _ = visitor_state(share_dir, vid)
    except Exception:
        return []
    return list(reversed(st.get('msgs') or []))


def _ensure_grants(share_dir, vid):
    """读取访客的加赠歌列表。

    老数据迁移：旧版的加赠是"每天按日期重摇 N 首"（每天换歌、昨天拆的今天变回锁定），
    这里把存量 bonus 一次性补定成固定的加赠歌，之后永久生效。"""
    st, data = visitor_state(share_dir, vid)
    gl = st.get('grants')
    if gl is None:
        gl = []
        st['grants'] = gl
        old = int(st.get('bonus') or 0)
        if old > 0:
            try:
                entries = [e for e in share_entries()[0] if e.get('audio')]
                used = set()
                for gi in range(min(old, 30)):
                    p = _pick_distinct(entries, 'migrate|%s|%d' % (vid, gi), vid, used)
                    if p is None:
                        break
                    uk = _uk(p)
                    used.add(uk)
                    gl.append({'uk': uk, 'at': _today_str()})
            except Exception:
                pass
        data[vid] = st
        save_visitors(share_dir, data)
    return st, gl


def grant_to_visitor(share_dir, vid, extra=0, msg=''):
    """给某位访客加开盒次数 / 留言。extra 可为负（回收），结果不小于 0。

    加赠的歌在发送那一刻就挑定并存进访客档案（一次性生效）：
    立即可拆、拆开永久解锁，不再每天重摇新歌。"""
    unlocked = set()
    today = _today_str()
    if extra > 0:
        # 先让 blindbox_state 把这位访客的加赠档案补齐/迁移，同时拿到已解锁集合
        try:
            entries, _pf, unlocked, _h, _tk, today, _fe = blindbox_state(vid)
        except Exception:
            unlocked = set()
    st, data = visitor_state(share_dir, vid)   # 此时磁盘上已是迁移后的最新档案
    st['bonus'] = max(0, int(st.get('bonus') or 0) + int(extra or 0))
    st.setdefault('msgs', [])
    if msg:
        st['msgs'].append({'text': str(msg)[:300], 'gift': int(extra or 0),
                           'at': dt.datetime.now().strftime('%Y-%m-%d %H:%M:%S'), 'seen': False})
        st['msgs'] = st['msgs'][-20:]
    if extra > 0:
        # 现在就把加赠歌挑定：只从这位访客自己的可抽范围里挑
        # （entries 已按其分配的作者池过滤），避开已解锁/已加赠过的
        grants = st.setdefault('grants', [])
        used = set(unlocked) | {g.get('uk') for g in grants}
        for gi in range(min(extra, 30)):
            p = _pick_distinct(entries, '%s|grant|%d|%d' % (today, int(time.time()), gi), vid, used)
            if p is None:
                break
            uk = _uk(p)
            used.add(uk)
            grants.append({'uk': uk, 'at': today})
        st['grants'] = grants[-120:]
    data[vid] = st
    save_visitors(share_dir, data)
    return st['bonus']


def visitor_label(vid):
    """把内部 vid 还原成人能看的名字（账号 → 用户名，浏览器 → 短 ID）。"""
    if not vid:
        return '未登录访客'
    if vid.startswith('u:'):
        return vid[2:]
    return '匿名·' + vid[2:8] if vid.startswith('v:') else vid[:8]


def blindbox_state(vid=None):
    """返回 (entries, profile, unlocked_keys, history, today_keys, today_date, festival)。

    vid 为空 → 全局视角（管理后台统计"从分享第一天到今天"应有的进度）；
    给了 vid → 这位访客自己的起点与掉落序列（互不相通）。
    """
    all_entries, profile, sh = share_entries('all')
    if not all_entries:
        return all_entries, profile, set(), [], set(), '', None
    share_dir = sh.get('dir') or '.'
    today = os.environ.get('KG_FAKE_DATE') or dt_date.today().isoformat()
    salt = ''
    grants = []
    st = {}
    if vid:
        try:
            st, _ = _ensure_grants(share_dir, vid)
            start = st.get('start') or today
            salt = '' if st.get('legacy') else vid
            grants = st.get('grants') or []
        except Exception:
            start, salt = today, vid
    else:
        # 全局视角（管理端）：沿用 guest_start 记录的分享起点
        start_file = os.path.join(share_dir, 'guest_start')
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
    # 视图层：访客的掉落范围 = 管理端分配（未分配 = 盲盒池），全局视角 = 盲盒池。
    # all_entries 是全库（历史掉落/加赠的歌即使被移出当前池/分配范围，
    # 也凭记录保持已解锁可听——已解锁的永久可听）
    pool_names = {os.path.abspath(p) for p in (sh.get('pool_dirs') or [])}
    if not pool_names and sh.get('dir'):
        pool_names = {os.path.abspath(sh['dir'])}
    if vid:
        allowed = {os.path.abspath(a) for a in ((st or {}).get('pools') or [])}
        base = allowed if allowed else pool_names
    else:
        base = pool_names
    entries = [e for e in all_entries if (not base or e.get('_pool') in base)]
    # 盲盒池只收有音频文件的作品：纯 MV / 音频缺失的歌抽中了也没法听
    pool = [e for e in entries if e.get('audio')]
    if not pool:
        return entries, profile, set(), [], set(), '', None
    history, unlocked = [], set()
    # 掉落按访客持久化：换池/扩池/修池后，历史解锁的歌永不消失、日历永不重排
    drops = st.setdefault('drops', {}) if isinstance(st, dict) and st else None
    drops_changed = False
    by_all_uk = {_uk(e): e for e in all_entries}
    today_picks = []
    try:
        d = dt_date.fromisoformat(start)
    except ValueError:
        d = dt_date.fromisoformat(today)
    end = dt_date.fromisoformat(today)
    guard = 0
    while d <= end and guard < 3650:
        day = d.isoformat()
        fest = __import__('core').get_festival(day)
        rec = drops.get(day) if drops is not None else None
        picks = []
        if rec:
            # 已记录的掉落优先按记录取（记录的歌被移出备份时跳过）
            for uk in rec:
                e = by_all_uk.get(uk)
                if e is not None:
                    picks.append(e)
        else:
            picks = [_pick_for_date(pool, day, salt)]
            if fest and fest.get('double'):
                picks.append(_pick_for_date(pool, day + '|bonus', salt))
            if drops is not None:
                drops[day] = [_uk(p) for p in picks if p is not None]
                drops_changed = True
        if day == today:
            today_picks = [p for p in picks if p is not None]
        for pi, pick in enumerate(picks):
            if pick is None:
                continue
            unlocked.add(_uk(pick))
            history.append({'date': day, 'title': pick.get('title') or '',
                            'singer': pick.get('singer_name') or '', 'uk': _uk(pick),
                            'festival': (fest['name'] + (pi > 0 and '加更' or '')) if fest and fest.get('double') else (fest['name'] if fest else '')})
        d += dt.timedelta(days=1)
        guard += 1
    if drops_changed and drops is not None:
        try:
            st2, data2 = visitor_state(share_dir, vid)   # 重新读盘合并，避免覆盖并发的加赠/分配
            st2['drops'] = drops
            data2[vid] = st2
            save_visitors(share_dir, data2)
        except Exception:
            pass
    # 今日堆叠 = 有序列表：日常在前、节日双倍其次（加赠在其后追加）
    today_keys = []
    for p in today_picks:
        k = _uk(p)
        if k not in today_keys:
            today_keys.append(k)
    festival = __import__('core').get_festival(today)
    # 机主加赠：发送时就挑定的歌，一次性立即生效——立即可拆、拆开永久解锁，
    # 不再按天重摇（旧版每天换一批新歌、昨天拆的今天变回锁定）
    if grants:
        by_uk = by_all_uk
        gopened = set(st.get('grants_opened') or [])
        for g in grants:
            e = by_uk.get(g.get('uk'))
            if e is None:
                continue
            k = _uk(e)
            unlocked.add(k)
            e['_fest'] = '加更'   # 卡片/揭晓卡上的角标
            history.append({'date': g.get('at') or today, 'title': e.get('title') or '',
                            'singer': e.get('singer_name') or '', 'uk': k,
                            'festival': '加更'})
            if k not in gopened and k not in today_keys:
                today_keys.append(k)   # 还没拆的进今天的堆叠（追加在日常之后，顺序稳定）
    # 历史掉落/加赠里的歌若已不在当前视图（缩池/改分配），凭记录补回——已解锁的永久可听
    if vid and st:
        have = {_uk(e) for e in entries}
        keep = set()
        for _day, uks in (st.get('drops') or {}).items():
            keep.update(uks or [])
        for g in (st.get('grants') or []):
            keep.add(g.get('uk'))
        missing = [by_all_uk[uk] for uk in keep
                   if uk and uk in by_all_uk and uk not in have and uk in unlocked]
        if missing:
            entries = entries + missing
            entries.sort(key=lambda x: ((x.get('_pool') or ''), x.get('seq') or 0))
            for i, e in enumerate(entries, 1):
                e['seq'] = i
    return entries, profile, unlocked, history, today_keys, today, festival


def docker_mode():
    """是否运行在 Docker 容器内（无 GUI，无法 os.startfile）。"""
    return bool(os.environ.get('KG_DATA_DIR') or os.environ.get('KG_NO_BROWSER'))


def data_root():
    """数据根目录：Docker 为挂载卷 /data，桌面版为 exe 所在目录。

    以前用 os.getcwd()：从不同目录启动 exe（或换了个快捷方式）就会看到"数据全没了"，
    实际只是读了另一个空目录。exe 版改为固定跟着 exe 走，升级换版本也不会丢。
    """
    d = os.environ.get('KG_DATA_DIR')
    if d:
        return os.path.abspath(d)
    if getattr(sys, 'frozen', False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.abspath(os.getcwd())


# 这些文件是"用户数据"，升级版本一条都不能丢
_DATA_KEEP = ('shares.json', 'auto_update.json', 'authors.json',
              'guest_visitors.json', 'guest_comments.json', 'guest_start')


def migrate_legacy_data():
    """老版本把数据放在启动目录（cwd）：exe 版首次运行新版本时搬到 exe 旁边。

    只搬"新位置还没有"的文件，绝不覆盖已有数据。
    """
    try:
        import shutil as _sh
        new = data_root()
        old = os.path.abspath(os.getcwd())
        if os.path.normcase(new) == os.path.normcase(old):
            return []
        moved = []
        for n in _DATA_KEEP:
            src, dst = os.path.join(old, n), os.path.join(new, n)
            if os.path.exists(src) and not os.path.exists(dst):
                try:
                    _sh.copy2(src, dst)
                    moved.append(n)
                except Exception:
                    pass
        return moved
    except Exception:
        return []


def snapshot_data():
    """每个版本第一次运行时，把用户数据留一份快照（.kgbak/<版本号>/），出问题能回滚。"""
    try:
        import shutil as _sh
        root = data_root()
        dst = os.path.join(root, '.kgbak', APP_VERSION.replace('.', '_'))
        made = []
        for n in _DATA_KEEP:
            src = os.path.join(root, n)
            if not os.path.exists(src):
                continue
            tgt = os.path.join(dst, n)
            if os.path.exists(tgt):
                continue
            try:
                os.makedirs(dst, exist_ok=True)
                _sh.copy2(src, tgt)
                made.append(n)
            except Exception:
                pass
        return made
    except Exception:
        return []


def guest_url_from_host(host_header, gport):
    """用请求的 Host 头构造盲盒页地址。

    Docker 里 lan_ips() 拿到的是容器内网 IP（浏览器不可达），
    而用户能用 Host 头里的地址访问工具页，那个地址必然可达。
    """
    if not host_header:
        return ''
    h = host_header.split(',')[0].strip().split(':')[0]
    if not h or h.startswith('127.') or h == 'localhost':
        return ''
    return f'http://{h}:{gport}/guest' if gport else f'http://{h}/guest'


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
    out_val = str(p.get('out') or '').strip()
    if out_val and not os.path.isabs(out_val) and os.environ.get('KG_DATA_DIR'):
        out_val = os.path.join(os.environ['KG_DATA_DIR'], out_val)
    return types.SimpleNamespace(
        out=out_val,
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




# ---------------------------------------------------------------- 访客互动数据

def _guest_data_file(share_dir, name):
    p = os.path.join(share_dir, name)
    # 换了备份目录 / 首次把访客数据挪出旧位置时，从数据根目录带过来，别让人以为"数据没了"
    if share_dir and not os.path.exists(p):
        legacy = os.path.join(data_root(), name)
        if os.path.exists(legacy):
            try:
                import shutil as _sh
                _sh.copy2(legacy, p)
            except Exception:
                pass
    return p


def _load_json_safe(path):
    try:
        with open(path, encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return {}


def _save_json_safe(path, data):
    try:
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
    except Exception:
        pass


def guest_comments(share_dir):
    return _load_json_safe(_guest_data_file(share_dir, 'guest_comments.json'))


def comments_of(comments, vid):
    """只挑出某人自己的留言（旧版留言没有 vid，视为仅管理员可见）。"""
    if not vid:
        return {}
    out = {}
    for uk, lst in (comments or {}).items():
        mine = [c for c in lst if (c.get('vid') or '') == vid]
        if mine:
            out[uk] = mine
    return out


def add_guest_comment(share_dir, uk, name, text, vid=''):
    path = _guest_data_file(share_dir, 'guest_comments.json')
    data = _load_json_safe(path)
    lst = data.setdefault(uk, [])
    lst.append({'name': name[:20], 'text': text[:200], 'time': time.strftime('%Y-%m-%d %H:%M'),
                'vid': vid})
    _save_json_safe(path, data)


def reply_guest_comment(share_dir, uk, ctime, text):
    """管理员回复某条留言：按 uk+时间定位，回复随原留言一起展示给这位访客。"""
    path = _guest_data_file(share_dir, 'guest_comments.json')
    data = _load_json_safe(path)
    for c in (data.get(uk) or []):
        if (c.get('time') or '') == ctime:
            c['reply'] = {'text': str(text)[:300],
                          'time': time.strftime('%Y-%m-%d %H:%M')}
            _save_json_safe(path, data)
            return True
    return False


def guest_plays(share_dir):
    """旧版全局播放次数（迁移前的数据，仍参与总量统计）。"""
    return _load_json_safe(_guest_data_file(share_dir, 'guest_plays.json'))


def visitor_plays(share_dir, vid):
    """某位访客自己的播放次数 —— 各人各算，不互通。"""
    if not vid:
        return {}
    try:
        st, _ = visitor_state(share_dir, vid)
        plays = st.get('plays')
        return plays if isinstance(plays, dict) else {}
    except Exception:
        return {}


def all_plays(share_dir):
    """全部访客播放数合计（管理后台 / 统计用）。"""
    total = dict(guest_plays(share_dir))
    for st in (visitors(share_dir) or {}).values():
        plays = (st or {}).get('plays') if isinstance(st, dict) else None
        if not isinstance(plays, dict):
            continue
        for k, v in plays.items():
            total[k] = total.get(k, 0) + int(v or 0)
    return total


def add_guest_play(share_dir, uk, vid=''):
    """记一次收听。有身份时只加自己的计数，返回本人该曲播放次数。"""
    if not vid:
        path = _guest_data_file(share_dir, 'guest_plays.json')
        data = _load_json_safe(path)
        data[uk] = data.get(uk, 0) + 1
        _save_json_safe(path, data)
        return data[uk]
    st, data = visitor_state(share_dir, vid)
    plays = st.setdefault('plays', {})
    plays[uk] = int(plays.get(uk) or 0) + 1
    st['last'] = time.strftime('%Y-%m-%d %H:%M')
    data[vid] = st
    save_visitors(share_dir, data)
    return plays[uk]

class Handler(BaseHTTPRequestHandler):
    server_version = f'KGBackup/{APP_VERSION}'  # HTTP 头仅允许 latin-1，中文放不进去

    def log_message(self, fmt, *a):  # 静默访问日志
        pass

    # 管理页密码：guest 盲盒公共资源放行，其余都要 kg_adm 会话/Basic
    def _is_public(self, path):
        if path in ('/guest', '/sw.js', '/manifest.webmanifest',
                    '/api/admin-login', '/api/admin-logout', '/favicon.ico'):
            return True
        return path.startswith('/g/') or path.startswith('/api/g-') or path.startswith('/static/')

    def _admin_ok(self):
        sh = load_shares()
        if not sh.get('admin_auth'):
            return True
        for part in (self.headers.get('Cookie') or '').split(';'):
            k, _, v = part.strip().partition('=')
            if k == 'kg_adm' and v in _adm_sessions:
                if _adm_sessions[v].get('exp', 0) > time.time():
                    return True
                _adm_sessions.pop(v, None)
        import base64
        auth = self.headers.get('Authorization') or ''
        if auth.startswith('Basic '):
            try:
                user, _, pwd = base64.b64decode(auth[6:]).decode('utf-8').partition(':')
                gmode = (sh.get('auth') or {}).get('mode') or 'none'
                for acc in (sh.get('accounts') or []):
                    if acc.get('disabled') or acc.get('user') != user:
                        continue
                    if gmode == 'user' or acc.get('pass') == pwd:
                        return True
            except Exception:
                pass
        return False

    # ---------- 工具 ----------
    def _vid(self):
        """访客身份。管理端口一律无身份（全局视角），盲盒端口在 GuestHandler 里重写。"""
        return None

    def _send(self, code, body, ctype='application/json; charset=utf-8', cache='no-store'):
        data = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', cache)
        for k, v in (getattr(self, '_extra_hdrs', None) or ()):
            self.send_header(k, v)
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
        entries, profile, unlocked, history, today_keys, today_date, festival = blindbox_state(self._vid())
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
            # 歌可能来自其他作者的目录（历史掉落/加赠的跨池解锁）：在已解锁条目的来源目录里找
            for e in entries:
                if _uk(e) not in unlocked:
                    continue
                for f in (e.get('audio'), e.get('cover'), e.get('lrc')):
                    if f and os.path.basename(f) == os.path.basename(full):
                        cand = os.path.abspath(os.path.join(e.get('_pool') or root, f))
                        if os.path.isfile(cand):
                            full = cand
                            break
                if os.path.exists(full):
                    break
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

    def _serve_manifest(self):
        fp = os.path.join(static_dir(), 'guest-manifest.json')
        try:
            with open(fp, 'rb') as f:
                return self._send(200, f.read(),
                                  'application/manifest+json; charset=utf-8', 'no-cache')
        except OSError:
            return self._send(404, {'error': 'manifest missing'})

    def _send_file_with_range(self, full, mime):
        """按 Range 请求发送本地文件（音频拖动/断点续传都靠它）。"""
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

    def _resolve_under_root(self, root, rel):
        """把 URL 相对路径安全解析到 root 之下，防目录穿越。返回绝对路径或 None。"""
        try:
            rel = urllib.request.url2pathname(rel).replace('\\', '/').lstrip('/')
        except Exception:
            return None
        root = os.path.abspath(root)
        full = os.path.abspath(os.path.join(root, rel))
        if not full.startswith(root + os.sep) and full != root:
            return None
        return full

    _PREVIEW_MIME = {'.html': 'text/html; charset=utf-8', '.htm': 'text/html; charset=utf-8',
                     '.m4a': 'audio/mp4', '.mp3': 'audio/mpeg', '.mp4': 'video/mp4',
                     '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.png': 'image/png',
                     '.gif': 'image/gif', '.webp': 'image/webp', '.lrc': 'text/plain; charset=utf-8',
                     '.json': 'application/json; charset=utf-8', '.csv': 'text/plain; charset=utf-8',
                     '.css': 'text/css; charset=utf-8', '.js': 'application/javascript; charset=utf-8',
                     '.txt': 'text/plain; charset=utf-8'}

    def _preview_root(self):
        """当前可预览的目录：本会话任务输出目录 > 当前分享目录。"""
        job = _STATE.get('job')
        if job and job.out_dir and os.path.isdir(job.out_dir):
            return os.path.abspath(job.out_dir)
        return os.path.abspath(load_shares().get('dir') or '')

    def _render_fresh(self, root):
        """按目录 index.json 实时渲染新版歌单/CD页（旧备份不用升级也能看到新界面）。按 mtime 缓存。"""
        idx_path = os.path.join(root, 'index.json')
        if not os.path.exists(idx_path):
            return None
        try:
            mtime = os.path.getmtime(idx_path)
            cache = _STATE.setdefault('preview_cache', {})
            hit = cache.get(root)
            if not hit or hit[0] != mtime:
                with open(idx_path, encoding='utf-8') as f:
                    idx = json.load(f)
                entries = sorted(idx.get('works', {}).values(), key=lambda x: x.get('seq') or 0)
                profile = idx.get('profile', {})
                core_mod = __import__('core')
                gen = core_mod.ts2str(mtime)
                docs = {'index.html': core_mod.render_html_doc(entries, profile, gen),
                        'cd.html': core_mod.render_cd_doc(entries, profile, gen)}
                cache[root] = (mtime, docs)
            return cache[root][1]
        except Exception:
            return None

    def _serve_preview(self, rel):
        rel = rel.split('?', 1)[0].split('#', 1)[0]  # 带查询串的链接（如 ?v=1）也能打开
        root = self._preview_root()
        if not root or not os.path.isdir(root):
            return self._send(404, b'no backup dir', 'text/plain; charset=utf-8')
        full = self._resolve_under_root(root, rel)
        if not full:
            return self._send(403, b'forbidden', 'text/plain; charset=utf-8')
        # 歌单/CD 页永远实时渲染当前模板（避免旧目录打开还是老界面）
        norm = rel.replace('\\', '/').lstrip('/')
        if norm in ('index.html', 'cd.html') or os.path.isdir(full or ''):
            docs = self._render_fresh(root)
            if docs and docs.get(norm):
                return self._send(200, docs[norm].encode(), 'text/html; charset=utf-8')
        if os.path.isdir(full):
            full = os.path.join(full, 'index.html')
        if not os.path.exists(full):
            return self._send(404, b'not found', 'text/plain; charset=utf-8')
        ext = os.path.splitext(full)[1].lower()
        mime = self._PREVIEW_MIME.get(ext)
        if not mime:
            return self._send(404, b'not found', 'text/plain; charset=utf-8')
        return self._send_file_with_range(full, mime)

    def _send_download(self, fname):
        """以附件形式发送分享目录里的歌单/目录文件（index.html / index.csv / cd.html）。"""
        sh_dir = load_shares().get('dir') or ''
        if not sh_dir:
            return self._send(404, {'error': '没有分享目录'})
        allowed = {'index.html': 'text/html; charset=utf-8',
                   'index.csv': 'text/csv; charset=utf-8',
                   'cd.html': 'text/html; charset=utf-8'}
        if fname not in allowed:
            return self._send(404, {'error': '文件不存在'})
        full = self._resolve_under_root(sh_dir, fname)
        if not full or not os.path.exists(full):
            return self._send(404, {'error': '文件不存在'})
        with open(full, 'rb') as f:
            data = f.read()
        self.send_response(200)
        self.send_header('Content-Type', allowed[fname])
        self.send_header('Content-Disposition', f'attachment; filename="{fname}"')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if not self._is_public(self.path) and not self._admin_ok():
            if self.path.startswith('/api/'):
                return self._send(401, {'error': '管理页需要登录', 'login': True})
            return self._send(200, _ADMIN_LOGIN_HTML.replace('__ERR__', '').encode(),
                              'text/html; charset=utf-8')
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
                     'manifest.json': 'application/manifest+json; charset=utf-8',
                     'guest-manifest.json': 'application/manifest+json; charset=utf-8',
                     'manifest.webmanifest': 'application/manifest+json; charset=utf-8',
                     'icon-192.png': 'image/png', 'icon-512.png': 'image/png'}
            nocache = name in ('manifest.json', 'guest-manifest.json', 'manifest.webmanifest',
                               'icon-192.png', 'icon-512.png')
            if name in allow:
                fp = os.path.join(static_dir(), name)
                try:
                    with open(fp, 'rb') as f:
                        return self._send(200, f.read(), allow[name], 'no-cache' if nocache else 'no-store')
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
            authors = [{k: v for k, v in a.items() if k != 'cookie'} for a in authors]
            return self._send(200, {'authors': authors})
        if self.path == '/api/comments':
            """管理端查看全部访客留言（留言已按访客隔离，这里负责汇总）。"""
            sh_dir = load_shares().get('dir') or ''
            comments = guest_comments(sh_dir) if sh_dir else {}
            rows = []
            for uk, lst in comments.items():
                for c in lst:
                    rows.append({'uk': uk, 'name': c.get('name') or '匿名',
                                 'text': c.get('text') or '', 'time': c.get('time') or '',
                                 'from': visitor_label(c.get('vid') or '')})
            rows.sort(key=lambda x: x['time'], reverse=True)
            return self._send(200, {'comments': rows, 'total': len(rows)})
        if self.path == '/api/visitors':
            """管理端：访客名单（含各自的开盒次数、停留起点、听过多少首）。"""
            sh_dir = load_shares().get('dir') or ''
            data = visitors(sh_dir) if sh_dir else {}
            rows = []
            for vid, st in data.items():
                if not isinstance(st, dict):
                    continue
                rows.append({'vid': vid, 'name': visitor_label(vid),
                             'start': st.get('start') or '', 'bonus': int(st.get('bonus') or 0),
                             'plays': sum(int(v or 0) for v in (st.get('plays') or {}).values()),
                             'last': st.get('last') or '',
                             'pools': st.get('pools') or [],
                             'msgs': len(st.get('msgs') or [])})
            rows.sort(key=lambda x: (-x['bonus'], x['name']))
            return self._send(200, {'visitors': rows, 'total': len(rows)})
        if self.path == '/api/grants':
            """管理端：最近发出去的通知记录（便于确认送达）。"""
            sh_dir = load_shares().get('dir') or ''
            data = visitors(sh_dir) if sh_dir else {}
            rows = []
            for vid, st in data.items():
                if not isinstance(st, dict):
                    continue
                for m in (st.get('msgs') or []):
                    rows.append({'name': visitor_label(vid), 'text': m.get('text') or '',
                                 'gift': m.get('gift') or 0, 'at': m.get('at') or '',
                                 'seen': bool(m.get('seen'))})
            rows.sort(key=lambda x: x['at'], reverse=True)
            return self._send(200, {'grants': rows[:50]})
        if self.path.startswith('/api/visitor-detail'):
            """管理端：某位访客的完整档案 —— 历史解锁/今日解锁/播放数据/最后播放时间。"""
            from urllib.parse import urlparse, parse_qs
            q = parse_qs(urlparse(self.path).query)
            vid = (q.get('vid') or [''])[0]
            sh_dir = load_shares().get('dir') or ''
            if not vid or not sh_dir:
                return self._send(400, {'error': '缺少访客或分享目录'})
            st = (visitors(sh_dir) or {}).get(vid)
            if not isinstance(st, dict):
                return self._send(404, {'error': '找不到这位访客'})
            try:
                entries, _pf, unlocked, history, _tk, today, _fe = blindbox_state(vid)
            except Exception:
                entries, unlocked, history, today = [], set(), [], ''
            # 每个日期内的第几张（节日双倍会同日多张）：opened[date] ≥ 序号+1 才算真拆开；
            # 加更（机主加赠）以 grants_opened 为准；老访客（没开盒进度记录）按服务端解锁集兜底
            gopened = set(st.get('grants_opened') or [])
            per_day = {}
            rows = []
            for h in history:
                k = per_day.get(h['date'], 0)
                per_day[h['date']] = k + 1
                if h.get('festival') == '加更':
                    rev = h['uk'] in gopened
                elif st.get('legacy'):
                    rev = h['uk'] in unlocked
                else:
                    rev = int((st.get('opened') or {}).get(h['date']) or 0) >= k + 1
                rows.append({'date': h['date'], 'title': h.get('title') or '',
                             'singer': h.get('singer') or '', 'festival': h.get('festival') or '',
                             'uk': h.get('uk') or '', 'revealed': rev})
            rows.sort(key=lambda x: x['date'], reverse=True)
            plays = st.get('plays') if isinstance(st.get('plays'), dict) else {}
            tmap = {_uk(e): (e.get('title') or '未命名') for e in entries}
            msgs = st.get('msgs') or []
            grant_left = sum(1 for g in (st.get('grants') or []) if g.get('uk') not in gopened)
            return self._send(200, {
                'vid': vid, 'name': visitor_label(vid),
                'start': st.get('start') or '', 'legacy': bool(st.get('legacy')),
                'bonus': int(st.get('bonus') or 0), 'grant_left': grant_left, 'msgs': len(msgs),
                'pools': st.get('pools') or [],
                'last_msg': (msgs[-1].get('text') or '') if msgs else '',
                'last_msg_at': (msgs[-1].get('at') or '') if msgs else '',
                'last_play': st.get('last') or '',
                'plays_total': sum(int(v or 0) for v in plays.values()),
                'plays_top': sorted(((tmap.get(uk, uk), int(n or 0)) for uk, n in plays.items()),
                                    key=lambda x: -x[1])[:8],
                'today': {'date': today,
                          'opened_n': int((st.get('opened') or {}).get(today) or 0),
                          'songs': [{'title': r['title'], 'singer': r['singer'],
                                     'festival': r['festival']} for r in rows
                                    if r['date'] == today and r['revealed']]},
                'history': rows})
        if self.path == '/api/bundle':
            """管理端：打包导出盲盒全部用户数据（访客/掉落/留言/播放/配置）。"""
            import zipfile as _zip
            sh = load_shares()
            sh_dir = sh.get('dir') or ''
            buf = io.BytesIO()
            with _zip.ZipFile(buf, 'w', _zip.ZIP_DEFLATED) as z:
                if sh_dir and os.path.isdir(sh_dir):
                    for name in sorted(os.listdir(sh_dir)):
                        if name.startswith('guest_') or name == 'guest_start':
                            fp = os.path.join(sh_dir, name)
                            if os.path.isfile(fp):
                                z.write(fp, 'sharedir/' + name)
                for name in ('shares.json', 'authors.json', 'auto_update.json'):
                    fp = os.path.join(os.getcwd(), name)
                    if os.path.isfile(fp):
                        z.write(fp, name)
                z.writestr('meta.txt', 'kg-backup bundle · ' + time.strftime('%Y-%m-%d %H:%M:%S'))
            data = buf.getvalue()
            self.send_response(200)
            self.send_header('Content-Type', 'application/zip')
            self.send_header('Content-Disposition', 'attachment; filename="kg-backup-bundle.zip"')
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        if self.path == '/api/auto-update':
            cfg = _load_json_safe(os.path.join(os.getcwd(), 'auto_update.json'))
            cfg['running'] = bool(_auto_thread and _auto_thread.is_alive())
            return self._send(200, cfg)
        if self.path == '/api/dirs':
            """列出数据根目录下所有可备份/已备份的子目录（含 index.json 的标记为有效）。"""
            root = data_root()
            dirs = []
            try:
                for name in sorted(os.listdir(root)):
                    full = os.path.join(root, name)
                    if not os.path.isdir(full):
                        continue
                    has_idx = os.path.exists(os.path.join(full, 'index.json'))
                    n_songs = 0
                    if has_idx:
                        try:
                            with open(os.path.join(full, 'index.json'), encoding='utf-8') as f:
                                n_songs = len(json.load(f).get('works') or {})
                        except Exception:
                            pass
                    dirs.append({'name': name, 'path': full, 'valid': has_idx, 'songs': n_songs})
            except OSError:
                pass
            return self._send(200, {'root': root, 'docker': docker_mode(),
                                    'dirs': dirs, 'current': (load_shares().get('dir') or '')})
        if self.path == '/api/share':
            entries, profile, unlocked, history, today_keys, today_date, festival = blindbox_state()
            ips = lan_ips() if _STATE.get('lan') else []
            port = _STATE['server'].server_address[1]
            gport = _STATE.get('guest_port') or 0
            # 自定义前缀（反代/域名）优先，其次按请求 Host 头构造（Docker/局域网都可达），最后退回本机 IP
            custom = (load_shares().get('guest_base') or '').rstrip('/')
            gurl = custom + '/guest' if custom else guest_url_from_host(self.headers.get('Host'), gport or port)
            if not gurl:
                gurl = f'http://{ips[0]}:{gport}/guest' if (ips and gport) else (f'http://{ips[0]}:{port}/guest' if ips else '')
            _sh = load_shares()
            _all = []
            _root = data_root()
            try:
                for name in sorted(os.listdir(_root)):
                    full = os.path.join(_root, name)
                    if os.path.isdir(full) and os.path.exists(os.path.join(full, 'index.json')):
                        _all.append(full)
            except OSError:
                pass
            return self._send(200, {
                'dir': (_sh.get('dir') or ''),
                'poolDirs': (_sh.get('pool_dirs') or []),
                'allDirs': _all,
                'nick': (_sh.get('nick') or ''),
                'adminAuth': bool(_sh.get('admin_auth')),
                'guestUrl': gurl, 'guestBase': custom, 'unlocked': len(unlocked), 'total': len(entries),
                'pool': sum(1 for e in entries if e.get('audio')),
                'today': next((e.get('title') for e in entries if _uk(e) in today_keys), ''),
                'ips': ips, 'guestPort': gport,
                'authMode': (load_shares().get('auth') or {}).get('mode') or 'none',
                'accounts': (load_shares().get('accounts') or []),
                'festival': (festival or {}).get('name') or '',
                'docker': docker_mode(),
                'visitors': len(visitors(load_shares().get('dir') or '.')),
                'dataDir': data_root()})
        if self.path == '/sw.js':
            # 盲盒页也能从管理端口进（8766 被占用时），SW 必须两个端口都在，
            # 否则 /guest 在 8765 打开时注册 404，浏览器永远不弹一键安装
            return self._send(200, _SW_JS.encode(), 'application/javascript; charset=utf-8')
        if self.path == '/manifest.webmanifest':
            return self._serve_manifest()
        if self.path == '/guest':
            try:
                entries, profile, unlocked, history, today_keys, today_date, festival = blindbox_state(self._vid())
            except Exception as e:
                return self._send(200, f'<html><body style="font-family:sans-serif;padding:40px"><h2>🎁 盲盒页加载失败</h2><p>{e}</p><p>请确认已完成至少一次备份。</p></body></html>'.encode(), 'text/html; charset=utf-8')
            if not entries:
                return self._send(200, '<html><body style="font-family:sans-serif;padding:40px;text-align:center"><h2>🎁 盲盒歌单</h2><p>还没有可分享的备份目录。</p><p>请先在工具页完成一次备份。</p></body></html>'.encode(), 'text/html; charset=utf-8')
            html = render_guest_page(entries, profile, unlocked, history, today_keys, today_date, festival)
            # 分享卡片 og:image 需要绝对地址：反代域名优先，其次按 Host 头
            _base = (load_shares().get('guest_base') or '').rstrip('/')
            if not _base:
                _host = self.headers.get('Host') or ''
                if _host:
                    _base = 'http://' + _host
            if _base:
                html = html.replace('__OGIMAGE__', _base + '/static/icon-512.png')
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
            snap['queue'] = [str(x.get('target') or '')[:46] for x in (_STATE.get('queue') or [])]
            return self._send(200, snap)
        if self.path.startswith('/api/open'):
            job = _STATE['job']
            what = (self.path.split('what=')[-1].split('&')[0] if 'what=' in self.path else 'folder')
            # 没有本会话任务时，退回当前分享目录（重开浏览器后也能打开）
            base = os.path.abspath(job.out_dir) if (job and job.out_dir) else (load_shares().get('dir') or '')
            if not base:
                return self._send(400, {'error': '还没有备份任务'})
            targets = {'folder': base, 'html': os.path.join(base, 'index.html'),
                       'csv': os.path.join(base, 'index.csv')}
            p = targets.get(what, base)
            if not os.path.exists(p):
                return self._send(400, {'error': f'文件不存在: {p}'})
            if what == 'html':
                # HTML 歌单一律走 /preview 实时渲染（旧目录也是新界面），不再打开磁盘文件
                return self._send(200, {'ok': True, 'url': '/preview/index.html'})
            if docker_mode():
                # 容器内没有桌面环境，无法弹出窗口：文件夹 → 返回路径让前端提示
                return self._send(200, {'ok': True, 'docker': True, 'path': base, 'what': what,
                                        'msg': 'Docker 容器内无法弹出文件夹，请在宿主机映射目录中查看（docker-compose 里 ./backups:/data 即输出根目录）。'})
            return self._send(200, {'ok': open_with_system(p)})
        if self.path.startswith('/preview/'):
            # 以 HTTP 服务当前备份目录（Docker 里"打开HTML歌单"用）
            return self._serve_preview(self.path[len('/preview/'):])
        if self.path.startswith('/api/download/'):
            return self._send_download(self.path[len('/api/download/'):])
        return self._send(404, {'error': 'not found'})

    # ---------- POST ----------
    def do_POST(self):
        if self.path == '/api/admin-login':
            try:
                raw = self.rfile.read(int(self.headers.get('Content-Length') or 0)).decode('utf-8')
                from urllib.parse import parse_qs
                qs = parse_qs(raw)
                user = (qs.get('user') or [''])[0].strip()
                pwd = (qs.get('pass') or [''])[0]
            except Exception:
                return self._send(400, {'error': '请求格式错误'})
            sh = load_shares()
            gmode = (sh.get('auth') or {}).get('mode') or 'none'
            ok = False
            for acc in (sh.get('accounts') or []):
                if acc.get('disabled') or acc.get('user') != user:
                    continue
                if gmode == 'user' or acc.get('pass') == pwd:
                    ok = True
                    break
            if not ok:
                return self._send(200, _ADMIN_LOGIN_HTML.replace('__ERR__', '账号或密码不对').encode(),
                                  'text/html; charset=utf-8')
            token = _create_adm_session(user)
            self.send_response(303)
            self.send_header('Location', '/')
            self.send_header('Set-Cookie', f'kg_adm={token}; Path=/; Max-Age=604800; SameSite=Lax')
            self.send_header('Content-Length', '0')
            self.end_headers()
            return
        if self.path == '/api/admin-logout':
            for part in (self.headers.get('Cookie') or '').split(';'):
                k, _, v = part.strip().partition('=')
                if k == 'kg_adm':
                    _adm_sessions.pop(v, None)
            self.send_response(303)
            self.send_header('Location', '/')
            self.send_header('Set-Cookie', 'kg_adm=; Path=/; Max-Age=0')
            self.send_header('Content-Length', '0')
            self.end_headers()
            return
        if not self._is_public(self.path) and not self._admin_ok():
            return self._send(401, {'error': '管理页需要登录', 'login': True})
        if self.path == '/api/bundle-import':
            """管理端：导入打包的用户数据（只恢复白名单文件，避免任意写盘）。"""
            p = self._body()
            b64 = str(p.get('data') or '').strip()
            if not b64:
                return self._send(400, {'error': '缺少数据'})
            import base64 as _b64
            import zipfile as _zip
            try:
                zf = _zip.ZipFile(io.BytesIO(_b64.b64decode(b64)))
            except Exception as e:
                return self._send(400, {'error': '压缩包解析失败：' + str(e)})
            sh = load_shares()
            sh_dir = sh.get('dir') or ''
            restored = []
            for info in zf.infolist():
                arc = info.filename.replace('\\', '/')
                name = os.path.basename(arc)
                if not name:
                    continue
                if arc.startswith('sharedir/') and (name.startswith('guest_') or name == 'guest_start') and sh_dir:
                    with open(os.path.join(sh_dir, name), 'wb') as f:
                        f.write(zf.read(info))
                    restored.append(name)
                elif name in ('shares.json', 'authors.json', 'auto_update.json'):
                    with open(os.path.join(os.getcwd(), name), 'wb') as f:
                        f.write(zf.read(info))
                    restored.append(name)
            return self._send(200, {'ok': True, 'restored': restored})
        if self.path == '/api/del-dir':
            """管理端：删除数据根目录下的某个备份目录。
            前端必须二次确认；这里再加三道围栏——只许删数据根下的单个目录、
            不许删数据根本身、当前分享目录必须先切换走。"""
            p = self._body()
            name = str(p.get('name') or '').strip()
            confirm = str(p.get('confirm') or '').strip()
            root = os.path.abspath(data_root())
            sh = load_shares()
            if not name or name in ('.', '..') or '/' in name or '\\' in name:
                return self._send(400, {'error': '目录名不合法'})
            if confirm != name:
                return self._send(400, {'error': '确认文字与目录名不一致'})
            full = os.path.abspath(os.path.join(root, name))
            if not full.startswith(root + os.sep) or full == root:
                return self._send(400, {'error': '只能删除数据根目录下的备份目录'})
            if os.path.abspath(sh.get('dir') or '') == full:
                return self._send(400, {'error': '这是当前分享目录：请先把其他目录「设为分享」后再删除'})
            if os.path.abspath(sh.get('nick') or '') == full:
                sh.pop('nick', None)
            if (sh.get('pool_dirs') or []):
                keep = [d for d in sh['pool_dirs'] if os.path.abspath(d) != full]
                if len(keep) != len(sh['pool_dirs']):
                    sh['pool_dirs'] = keep
                    save_shares(sh)
            if not os.path.isdir(full):
                return self._send(400, {'error': '目录不存在（可能已删除）'})
            import shutil as _shutil
            try:
                _shutil.rmtree(full)
            except Exception as e:
                return self._send(400, {'error': '删除失败：' + str(e)})
            return self._send(200, {'ok': True})
        if self.path == '/api/clean-dirs':
            """管理端：清理无 index.json 的残留目录（如 .kgbak），有效目录与当前分享目录不动。"""
            root = os.path.abspath(data_root())
            sh = load_shares()
            cur = os.path.abspath(sh.get('dir') or '')
            removed, failed = [], []
            import shutil as _shutil
            try:
                for name in sorted(os.listdir(root)):
                    full = os.path.abspath(os.path.join(root, name))
                    if not os.path.isdir(full) or full == root or full == cur:
                        continue
                    if os.path.exists(os.path.join(full, 'index.json')):
                        continue
                    try:
                        _shutil.rmtree(full)
                        removed.append(name)
                    except Exception:
                        failed.append(name)
            except OSError as e:
                return self._send(400, {'error': str(e)})
            return self._send(200, {'ok': True, 'removed': removed, 'failed': failed})

        if self.path == '/api/start':
            try:
                p = self._body()
                target = str(p.get('target') or '').strip()
                if not target:
                    return self._send(400, {'error': '请先填写个人主页链接或 uid'})
                if _STATE['thread'] and _STATE['thread'].is_alive():
                    q = _STATE.setdefault('queue', [])
                    if len(q) >= 10:
                        return self._send(429, {'error': '队列已满（最多 10 个）'})
                    q.append(p)
                    return self._send(200, {'ok': True, 'queued': True, 'pos': len(q),
                                            'msg': f'已有任务进行中，已加入队列（第 {len(q)} 位）'})
                _STATE['queue'] = []
                _start_job(target, p)
                ck = str(p.get('cookie') or '').strip()
                if ck:
                    try:
                        from core import parse_uid as _puid
                        au = str(_puid(target) or '').strip()
                        if au:
                            fp = os.path.join(os.getcwd(), 'authors.json')
                            ad = {}
                            try:
                                with open(fp, encoding='utf-8') as f:
                                    ad = json.load(f)
                            except Exception:
                                pass
                            ent = ad.get(au) or {'uid': au, 'nickname': au}
                            ent['cookie'] = ck
                            ent['cookie_ok'] = True
                            ad[au] = ent
                            with open(fp, 'w', encoding='utf-8') as f:
                                json.dump(ad, f, ensure_ascii=False, indent=1)
                    except Exception:
                        pass
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
        if self.path == '/api/cookie-check':
            """管理端：校验某位已备份作者的存储 cookie 是否仍有效（私密歌备份的前提）。"""
            p = self._body()
            uid = str(p.get('uid') or '').strip()
            try:
                with open(os.path.join(os.getcwd(), 'authors.json'), encoding='utf-8') as f:
                    data = json.load(f)
            except Exception:
                data = {}
            a = data.get(uid)
            if not a:
                return self._send(404, {'error': '没有这位作者的档案'})
            cookie = str(a.get('cookie') or '').strip()
            if not cookie:
                return self._send(200, {'ok': False, 'noCookie': True,
                                        'error': '该作者没有存过 cookie（备份时没填）'})
            try:
                from core import KGClient
                client = KGClient(cookie, '', 20, 2)
                client.share_uid = uid
                d = client.get_work_page(uid, 1)
                a['cookie_ok'] = True
                a['checked'] = ts2str(time.time())
                data[uid] = a
                with open(os.path.join(os.getcwd(), 'authors.json'), 'w', encoding='utf-8') as f:
                    json.dump(data, f, ensure_ascii=False, indent=1)
                return self._send(200, {'ok': True, 'nickname': d.get('nickname', ''),
                                        'count': d.get('ugc_total_count')})
            except Exception as e:
                a['cookie_ok'] = False
                a['checked'] = ts2str(time.time())
                data[uid] = a
                with open(os.path.join(os.getcwd(), 'authors.json'), 'w', encoding='utf-8') as f:
                    json.dump(data, f, ensure_ascii=False, indent=1)
                return self._send(200, {'ok': False, 'error': str(e)})
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
        if self.path == '/api/comment-reply':
            """管理端：回复朋友的一条留言（对方在自己的留言列表里能看到机主回复）。"""
            p = self._body()
            sh_dir = load_shares().get('dir') or ''
            if not sh_dir:
                return self._send(400, {'error': '还没设置分享目录'})
            uk = str(p.get('uk') or '')
            ctime = str(p.get('time') or '')
            text = str(p.get('text') or '').strip()
            if not uk or not ctime or not text:
                return self._send(400, {'error': '缺少留言定位信息或回复内容'})
            ok = reply_guest_comment(sh_dir, uk, ctime, text)
            if not ok:
                return self._send(404, {'error': '找不到这条留言（可能已被删除）'})
            return self._send(200, {'ok': True})
        if self.path == '/api/grant':
            """管理端：给盲盒用户加开盒次数 + 发送一条消息（对方打开就弹猫提醒）。"""
            p = self._body()
            sh_dir = load_shares().get('dir') or ''
            if not sh_dir:
                return self._send(400, {'error': '还没设置分享目录'})
            try:
                extra = int(p.get('n') or 0)
            except Exception:
                extra = 0
            msg = str(p.get('msg') or '').strip()[:300]
            who = p.get('vid') or p.get('to') or ''
            pools = p.get('pools') if isinstance(p.get('pools'), list) else None
            one_uk = str(p.get('uk') or '').strip()
            if who and who != '*':
                targets = [str(who)]
            else:
                targets = [v for v, st in (visitors(sh_dir) or {}).items() if isinstance(st, dict)]
            if not targets:
                return self._send(400, {'error': '还没有访客来过，先让他们打开一次盲盒页'})
            done = []
            for vid in targets:
                if not _VID_ANY_RE.match(vid):
                    continue
                bonus = grant_to_visitor(sh_dir, vid, extra, msg)
                if one_uk:
                    # 定向补偿：把指定歌作为该访客的未拆加更送出（进堆叠，拆开永久解锁）
                    st3, data3 = visitor_state(sh_dir, vid)
                    gl = st3.setdefault('grants', [])
                    if one_uk not in [g.get('uk') for g in gl]:
                        gl.append({'uk': one_uk, 'at': _today_str()})
                        st3['grants'] = gl[-120:]
                        data3[vid] = st3
                        save_visitors(sh_dir, data3)
                if pools is not None:
                    st2, data2 = visitor_state(sh_dir, vid)
                    st2['pools'] = [str(x) for x in pools if str(x).strip()]
                    data2[vid] = st2
                    save_visitors(sh_dir, data2)
                done.append({'vid': vid, 'name': visitor_label(vid), 'bonus': bonus,
                             'pools': (st2 or {}).get('pools') if pools is not None else None})
            return self._send(200, {'ok': True, 'sent': len(done), 'rows': done})
        if self.path == '/api/share':
            p = self._body()
            if 'dir' in p:
                d = str(p.get('dir') or '').strip()
                dr_base = os.environ.get('KG_DATA_DIR') or ''
                if d and not os.path.isabs(d) and dr_base:
                    d = os.path.join(dr_base, d)
                if d and os.path.isdir(d) and os.path.exists(os.path.join(d, 'index.json')):
                    sh = load_shares()
                    if not sh.get('since'):
                        import datetime as _dt
                        sh['since'] = _dt.date.today().isoformat()
                    sh['dir'] = d
                    save_shares(sh)
                    return self._send(200, {'ok': True, 'dir': d})
                return self._send(400, {'error': '目录不存在或还没有 index.json（先完成一次备份）'})
            if 'pool_dirs' in p:
                # 盲盒池作者集合：勾选哪些备份目录参与盲盒（可混合可单独）；空 = 只用主目录
                sh = load_shares()
                root = os.environ.get('KG_DATA_DIR') or ''
                dirs = []
                for d in (p.get('pool_dirs') or []):
                    d = str(d or '').strip()
                    if not d:
                        continue
                    if not os.path.isabs(d) and root:
                        d = os.path.join(root, d)
                    if os.path.isdir(d) and os.path.exists(os.path.join(d, 'index.json')):
                        dirs.append(d)
                sh['pool_dirs'] = dirs
                save_shares(sh)
                return self._send(200, {'ok': True, 'pool_dirs': dirs})
            if 'admin_auth' in p:
                sh = load_shares()
                sh['admin_auth'] = bool(p.get('admin_auth'))
                save_shares(sh)
                return self._send(200, {'ok': True, 'admin_auth': sh['admin_auth']})
            if 'nick' in p:
                # 盲盒页昵称覆盖（多作者混合时不再只显示第一个作者的名字）
                sh = load_shares()
                nick = str(p.get('nick') or '').strip()[:30]
                if nick:
                    sh['nick'] = nick
                else:
                    sh.pop('nick', None)
                save_shares(sh)
                return self._send(200, {'ok': True, 'nick': nick})
            if 'guest_base' in p:
                # 自定义盲盒链接前缀（反代/域名/端口与直连不一致时用），空 = 自动生成
                base = str(p.get('guest_base') or '').strip().rstrip('/')
                sh = load_shares()
                if base:
                    if not base.startswith('http://') and not base.startswith('https://'):
                        base = 'http://' + base
                    sh['guest_base'] = base
                else:
                    sh.pop('guest_base', None)
                save_shares(sh)
                return self._send(200, {'ok': True, 'guest_base': base})
            if 'auth_mode' in p or 'auth_delete_user' in p or 'auth_account' in p:
                sh = load_shares()
                accounts = sh.get('accounts') or []
                if 'auth_mode' in p:
                    sh['auth'] = {'mode': str(p.get('auth_mode') or 'none')}
                # 删除账号
                if p.get('auth_delete_user'):
                    duser = str(p.get('auth_delete_user'))
                    accounts = [a for a in accounts if a.get('user') != duser]
                # 新增或修改账号（auth_account = 原用户名；为空表示新增）
                acc = p.get('auth_account')
                if isinstance(acc, dict) and str(acc.get('user') or '').strip():
                    new_user = str(acc.get('user')).strip()
                    orig_user = str(acc.get('orig_user') or '').strip() or new_user
                    fields = {'user': new_user, 'note': str(acc.get('note') or '')}
                    # 密码随填随存：留空 = 清除密码（这个账号变成免密账号）
                    fields['pass'] = str(acc.get('pass') or '')
                    if 'disabled' in acc:
                        fields['disabled'] = bool(acc.get('disabled'))
                    existing = [a for a in accounts if a.get('user') == orig_user]
                    if existing:
                        existing[0].update(fields)
                    else:
                        existing_new = [a for a in accounts if a.get('user') == new_user]
                        if existing_new:
                            existing_new[0].update(fields)
                        else:
                            fields.setdefault('pass', '')
                            accounts.append(fields)
                    accounts = [a for a in accounts if a.get('user')]  # 防空用户名
                sh['accounts'] = accounts
                save_shares(sh)
                return self._send(200, {'ok': True, 'auth': (sh.get('auth') or {}).get('mode') or 'none',
                                        'accounts': accounts})
            entries, profile, unlocked, history, today_keys, today_date, festival = blindbox_state()
            ips = lan_ips() if _STATE.get('lan') else []
            port = _STATE['server'].server_address[1]
            url = f'http://{ips[0]}:{port}/guest' if ips else ''
            return self._send(200, {
                'ok': True, 'dir': (load_shares().get('dir') or ''),
                'guestUrl': url, 'unlocked': len(unlocked), 'total': len(entries),
                'today': next((e.get('title') for e in entries if _uk(e) in today_keys), ''),
                'ips': ips})
        if self.path == '/api/auto-update':
            cfg_path = os.path.join(os.getcwd(), 'auto_update.json')
            p = self._body()
            cfg = _load_json_safe(cfg_path)
            if 'enabled' in p:
                cfg['enabled'] = bool(p['enabled'])
                if cfg['enabled']:
                    start_auto_update()
                else:
                    _auto_stop.set()
            if 'interval_hours' in p:
                cfg['interval_hours'] = max(1, int(p['interval_hours']))
            if 'cookie' in p:
                cfg['cookie'] = str(p['cookie'])
            if 'authors' in p:
                cfg['authors'] = p['authors']
            _save_json_safe(cfg_path, cfg)
            if p.get('run_now'):
                if _STATE['thread'] and _STATE['thread'].is_alive():
                    return self._send(409, {'error': '备份任务进行中，稍后再试'})
                threading.Thread(target=_auto_check_once, daemon=True).start()
                return self._send(200, dict(cfg, run_now=True))
            return self._send(200, cfg)
        if self.path == '/api/health-check':
            p = self._body()
            d = str(p.get('dir') or '').strip()
            if not d or not os.path.isdir(d):
                return self._send(400, {'error': '目录不存在'})
            audio_files = []
            for sub_d in ('audio', ''):
                scan = os.path.join(d, sub_d) if sub_d else d
                if not os.path.isdir(scan):
                    continue
                for f in os.listdir(scan):
                    if f.endswith(('.m4a', '.mp4')):
                        audio_files.append(os.path.join(scan, f))
            ok, bad = 0, []
            for af in audio_files:
                try:
                    r = subprocess.run(['ffprobe', '-v', 'error', '-show_entries', 'format=duration',
                                        '-of', 'json', af], capture_output=True, timeout=15)
                    j = json.loads(r.stdout)
                    if float(j['format']['duration']) > 1:
                        ok += 1
                    else:
                        bad.append(os.path.basename(af))
                except Exception:
                    bad.append(os.path.basename(af))
            return self._send(200, {'ok': ok, 'bad': bad, 'total': len(audio_files)})
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


# 盲盒端口登录会话：token -> {'user': 账号名, 'exp': 过期时间}
_g_sessions = {}
_adm_sessions = {}


def _create_adm_session(user):
    import secrets as _sec
    token = _sec.token_hex(32)
    for k in [k for k, v in _adm_sessions.items() if v.get('exp', 0) < time.time()]:
        _adm_sessions.pop(k, None)
    _adm_sessions[token] = {'user': user, 'exp': time.time() + 7 * 86400}
    return token


_ADMIN_LOGIN_HTML = '''<!DOCTYPE html>
<html lang="zh-cn"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>登录 · 管理页</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{min-height:100vh;display:flex;align-items:center;justify-content:center;padding:20px;
 background:radial-gradient(700px 480px at 50% -6%,#1c2536,#0b0d13 60%);
 font-family:-apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei",sans-serif}
.card{width:min(360px,100%);border-radius:20px;padding:30px 26px;text-align:center;
 background:linear-gradient(180deg,#1c2231,#131722);border:1px solid #2a3140;
 box-shadow:0 24px 60px rgba(0,0,0,.5)}
.ic{font-size:40px}
h1{font-size:17px;color:#eef2f8;margin:8px 0 4px}
p{font-size:12px;color:#8b94a6;margin-bottom:18px}
input{width:100%;padding:12px 14px;margin-bottom:10px;border-radius:12px;border:1px solid #2a3140;
 background:#0d1119;color:#e8ecf3;font-size:14px;outline:none;text-align:center;font-family:inherit}
input:focus{border-color:#4c9aff;box-shadow:0 0 0 3px rgba(76,154,255,.2)}
.btn{width:100%;padding:12px;border:none;border-radius:12px;cursor:pointer;font-family:inherit;
 background:linear-gradient(135deg,#0071e3,#4c9aff);color:#fff;font-size:15px;font-weight:700}
.err{color:#ff6b6b;font-size:12px;margin-top:10px;min-height:16px}
</style></head><body>
<div class="card">
 <div class="ic">🛡️</div>
 <h1>管理页登录</h1>
 <p>已开启管理页密码保护，输入朋友盲盒的账号即可</p>
 <form method="POST" action="/api/admin-login">
  <input name="user" placeholder="账号" required>
  <input name="pass" type="password" placeholder="密码（免密账号留空）">
  <button class="btn" type="submit">进 入</button>
 </form>
 <div class="err">__ERR__</div>
</div>
</body></html>'''
_G_SESS_TTL = 7 * 86400


def _create_g_session(user):
    import secrets as _sec
    token = _sec.token_hex(32)
    # 顺手清掉过期会话，避免长期运行内存膨胀
    now = time.time()
    for k in [k for k, v in _g_sessions.items() if v.get('exp', 0) < now]:
        _g_sessions.pop(k, None)
    _g_sessions[token] = {'user': user, 'exp': now + _G_SESS_TTL}
    return token


# Service Worker：只要有一个能响应 fetch 的 SW，浏览器就把本站当成"可安装的 App"。
# 全部走网络，避免音频/封面被缓存成旧版本。
_SW_JS = r'''/* 盲盒歌单 Service Worker：外壳离线缓存（可安装的关键 = SW 能离线响应 start_url）
  配方对齐已验证可安装的任务魔盒：install 预缓存外壳 + stale-while-revalidate */
const CACHE = 'kg-guest-v2';
const SHELL = ['/', '/guest', '/manifest.webmanifest', '/static/icon-192.png', '/static/icon-512.png'];

self.addEventListener('install', e => {
  e.waitUntil(caches.open(CACHE).then(c => c.addAll(SHELL)).then(() => self.skipWaiting()));
});
self.addEventListener('activate', e => {
  e.waitUntil(caches.keys().then(ks =>
    Promise.all(ks.filter(k => k !== CACHE).map(k => caches.delete(k))).then(() => self.clients.claim())));
});
self.addEventListener('fetch', e => {
  const req = e.request;
  if (req.method !== 'GET') return;
  const url = new URL(req.url);
  if (url.origin !== location.origin) return;      /* 第三方直接放过 */
  if (url.pathname.startsWith('/api/')) return;    /* 接口永远走网络 */
  if (req.headers.get('range')) return;            /* 音频分段/拖动不插手 */
  if (/\.(m4a|mp3|mp4|flac)$/i.test(url.pathname)) return;  /* 大音频不缓存 */
  e.respondWith(caches.open(CACHE).then(async c => {
    const hit = await c.match(req, {ignoreSearch: true});
    const net = fetch(req).then(r => {
      if (r && r.ok && (req.mode === 'navigate' || url.pathname.startsWith('/static/') ||
                        /\.(png|jpe?g|webp|css|js|json|lrc|webmanifest)$/i.test(url.pathname))) {
        c.put(req, r.clone()).catch(() => {});
      }
      return r;
    }).catch(() => hit);
    return hit || net;   /* 外壳/封面离线可开（盲盒页装成应用后断网也不白屏） */
  }));
});
'''


class GuestHandler(Handler):
    """独立盲盒端口：只提供 /guest 与 /g/*，不暴露任何管理功能。"""

    def _auth_ok(self):
        sh = load_shares()
        cfg = sh.get('auth') or {}
        mode = cfg.get('mode') or 'none'
        if mode == 'none':
            return True
        # 先看登录会话 Cookie（自定义登录页签发的，浏览器弹窗体验差已被弃用）
        for part in (self.headers.get('Cookie') or '').split(';'):
            k, _, v = part.strip().partition('=')
            if k == 'kg_sess' and v in _g_sessions:
                sess = _g_sessions[v]
                if sess.get('exp', 0) > time.time():
                    self._auth_user = sess.get('user') or ''
                    return True
                _g_sessions.pop(v, None)  # 过期即清
        # 兼容旧的 HTTP Basic Auth（收藏链接里可能带着）
        accounts = sh.get('accounts') or []
        import base64
        auth = self.headers.get('Authorization') or ''
        if not auth.startswith('Basic '):
            return False
        try:
            user, _, pwd = base64.b64decode(auth[6:]).decode('utf-8').partition(':')
        except Exception:
            return False
        for acc in accounts:
            if acc.get('disabled'):
                continue  # 已停用的账号不允许登录
            if acc.get('user') != user:
                continue
            # 仅账号模式：只校验用户名；账号+密码模式：两者都校验
            if mode == 'user' or acc.get('pass') == pwd:
                self._auth_user = user
                return True
        return False

    def _vid(self):
        """访客身份 = 盲盒进度与播放次数的主人。

        开了访问控制时直接用账号名（同一个人换设备/换浏览器还是同一份进度）；
        无需验证模式则用 kg_vid Cookie（每个浏览器一份，互不相通）。
        """
        if getattr(self, '_vid_cache', None):
            return self._vid_cache
        user = getattr(self, '_auth_user', '')
        if user:
            vid = 'u:' + user
        else:
            vid = ''
            for part in (self.headers.get('Cookie') or '').split(';'):
                k, _, v = part.strip().partition('=')
                if k == 'kg_vid' and _VID_RE.match(v or ''):
                    vid = 'v:' + v
                    break
            if not vid:
                vid = 'v:' + uuid.uuid4().hex[:16]
                # 首次到访种下身份，之后每天的开盒记录都算在这个人头上
                self._extra_hdrs = [('Set-Cookie',
                                     f'kg_vid={vid[2:]}; Path=/; Max-Age=31536000; SameSite=Lax')]
        self._vid_cache = vid
        return vid

    _login_html = '''<!DOCTYPE html>
<html lang="zh-cn"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="theme-color" content="#20160f">
<title>登录 · 盲盒歌单</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{min-height:100vh;min-height:100dvh;display:flex;align-items:center;justify-content:center;padding:20px 14px;
 background:radial-gradient(900px 620px at 50% 0%,#2a1d15,#120c09 62%,#0a0706);
 font-family:-apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei",sans-serif;color:#f0e8dd;
 -webkit-tap-highlight-color:transparent;-webkit-font-smoothing:antialiased}
.wrap{width:min(400px,100%)}
/* ---------- 卡座机身 ---------- */
.deck{position:relative;border-radius:20px;padding:16px 16px 14px;overflow:hidden;
 background:linear-gradient(180deg,#3c2a1e,#2a1c13 44%,#1d130d);
 box-shadow:0 0 0 1px rgba(255,225,180,.12) inset,0 0 0 6px #150e09,0 0 0 7px #6b5433,
  0 30px 70px rgba(0,0,0,.75),0 0 60px rgba(255,190,90,.06)}
.deck::before{content:'';position:absolute;left:0;right:0;top:0;height:44px;
 background:linear-gradient(180deg,rgba(255,220,170,.1),transparent);pointer-events:none}
.brand{display:flex;align-items:center;gap:8px;font-size:10px;letter-spacing:4px;color:#c9a86a;
 font-weight:700;margin-bottom:12px;position:relative}
.brand .dot{width:7px;height:7px;border-radius:50%;background:#ff5b5b;box-shadow:0 0 9px #ff5b5b;animation:blink 1.8s infinite}
.brand .sp{flex:1;height:1px;background:linear-gradient(90deg,rgba(201,168,106,.35),transparent)}
@keyframes blink{50%{opacity:.25}}
/* ---------- 磁带窗 + 纸标签（账号密码写在这里） ---------- */
.cassette{position:relative;border-radius:14px;padding:14px;
 background:linear-gradient(180deg,#241a12,#170f0a);
 box-shadow:0 10px 26px rgba(0,0,0,.6) inset,0 0 0 2px #6b5433,0 0 0 4px #1a120c}
.reels{position:relative;height:52px;margin-bottom:10px}
.reel{position:absolute;top:0;width:52px;height:52px;border-radius:50%;
 background:radial-gradient(circle,#f4e7cf 0 26%,#d8c3a1 27% 30%,#2a2018 31% 100%);
 box-shadow:0 0 0 2px #0f0a07,0 4px 10px rgba(0,0,0,.5);animation:spin 5s linear infinite}
.reel.l{left:8px} .reel.r{right:8px}
.reel::after{content:'';position:absolute;inset:16px;border-radius:50%;
 background:conic-gradient(#8a7460 0 12%,transparent 12% 25%,#8a7460 25% 37%,transparent 37% 50%,
 #8a7460 50% 62%,transparent 62% 75%,#8a7460 75% 87%,transparent 87% 100%)}
.reels .tape{position:absolute;left:70px;right:70px;top:24px;height:3px;border-radius:2px;
 background:linear-gradient(90deg,rgba(120,90,60,.2),#5a4636,rgba(120,90,60,.2))}
@keyframes spin{to{transform:rotate(360deg)}}
.label{background:linear-gradient(180deg,#fbf4e2,#efe2c8);border-radius:9px;padding:12px 12px 14px;
 box-shadow:0 2px 0 #b9a680,0 8px 20px rgba(0,0,0,.45)}
.label h1{font-size:15px;color:#3a2a18;text-align:center;letter-spacing:1px}
.label .sub{font-size:10.5px;color:#8a7455;text-align:center;margin:3px 0 10px}
.label input{width:100%;padding:11px 12px;margin-bottom:8px;border-radius:9px;border:1px solid #cdbb96;
 background:#fffdf6;color:#2c2114;font-size:14px;outline:none;text-align:center;font-family:inherit;
 transition:.16s;-webkit-appearance:none;appearance:none}
.label input::placeholder{color:#b3a184}
.label input:focus{border-color:#b8860b;box-shadow:0 0 0 3px rgba(184,134,11,.16)}
/* ---------- 走带按钮（播放=进入） ---------- */
.keys{display:flex;align-items:center;justify-content:center;gap:10px;margin-top:14px}
.key{width:40px;height:34px;border-radius:7px;border:none;cursor:pointer;font-size:13px;color:#e8dcc8;
 background:linear-gradient(180deg,#4a3826,#241a11);box-shadow:0 3px 0 #120c08,0 0 0 1px rgba(255,220,170,.14) inset;
 font-family:inherit;display:inline-flex;align-items:center;justify-content:center;-webkit-appearance:none;appearance:none}
.key:active{transform:translateY(2px);box-shadow:0 1px 0 #120c08}
.play{flex:1;max-width:190px;height:44px;border-radius:10px;border:none;cursor:pointer;font-family:inherit;
 font-size:15px;font-weight:800;letter-spacing:1px;color:#2a1c0c;
 background:linear-gradient(180deg,#ffd870,#dda93c);box-shadow:0 4px 0 #8a6620,0 0 22px rgba(255,200,90,.35);
 display:inline-flex;align-items:center;justify-content:center;gap:8px;-webkit-appearance:none;appearance:none}
.play:active{transform:translateY(3px);box-shadow:0 1px 0 #8a6620}
/* ---------- 底部格栅 + 错误灯 ---------- */
.grille{height:16px;border-radius:6px;margin-top:14px;
 background:repeating-linear-gradient(90deg,#120c08 0 5px,#3a2a1c 5px 7px)}
.err{min-height:16px;font-size:11.5px;color:#ff8f8f;text-align:center;padding-top:9px;letter-spacing:.3px}
body.loading .reel{animation-duration:.7s}
body.loading .play{opacity:.75}
.foot{text-align:center;font-size:10.5px;color:#7a6a55;margin-top:12px}
@media (max-width:360px){.reel{width:44px;height:44px}.reels{height:44px}.label h1{font-size:14px}}
</style></head><body>
<div class="wrap">
 <form class="deck" method="POST" action="/api/g-login" onsubmit="return checkLogin(this)">
  <div class="brand"><span class="dot"></span>K·SONG TAPE DECK<span class="sp"></span>█ █ █</div>
  <div class="cassette">
   <div class="reels"><i class="reel l"></i><span class="tape"></span><i class="reel r"></i></div>
   <div class="label">
    <h1>📼 盲盒歌单</h1>
    <div class="sub">塞进一盘磁带 · 有密码的输密码，免密账号密码留空</div>
    <input id="u" name="user" placeholder="账号" autocomplete="username" autocapitalize="off" spellcheck="false">
    <input id="p" name="pass" type="password" placeholder="密码（免密账号留空）" autocomplete="current-password">
   </div>
  </div>
  <div class="keys">
   <button class="key" type="button" onclick="document.getElementById('u').focus()" title="账号">⏮</button>
   <button class="play" type="submit">▶&nbsp;&nbsp;播 放</button>
   <button class="key" type="button" onclick="document.getElementById('p').focus()" title="密码">⏭</button>
  </div>
  <div class="grille"></div>
  <div class="err" id="err">__ERR__</div>
 </form>
 <div class="foot">按「播放」进入今日盲盒</div>
</div>
<script>
function checkLogin(f){
  var u = f.user.value.trim(), p = f.pass.value;
  var e = document.getElementById('err');
  if (!u) { e.textContent = '请先填写账号'; f.user.focus(); return false; }
  /* 密码可以留空：免密账号只输账号就能进；设了密码的账号留空会被拦下并提示 */
  e.textContent = '';
  document.body.classList.add('loading');
  return true;
}
if (location.search.indexOf('err=1') > -1) document.getElementById('err').textContent = '账号或密码不对，再试一次';
</script>
</body></html>'''

    def do_GET(self):
        # SW / manifest 不需要鉴权（浏览器取这些资源时未必带 Cookie），先放行；
        # manifest 挂根路径（对齐任务魔盒：/manifest.webmanifest + charset），浏览器才认"可安装的 PWA"
        if self.path in ('/sw.js', '/g/sw.js'):
            # /g/sw.js 保留兼容旧页面
            return self._send(200, _SW_JS.encode(), 'application/javascript; charset=utf-8')
        if self.path == '/manifest.webmanifest':
            return self._serve_manifest()
        # 访客认证：页面请求返回自定义登录页（不用浏览器原生弹窗），接口返回 JSON
        if not self._auth_ok():
            if self.path.startswith('/api/') or self.path.startswith('/g/'):
                return self._send(403, {'error': '需要登录', 'login': True})
            html = self._login_html.replace('__ERR__', '')
            return self._send(200, html.encode(), 'text/html; charset=utf-8')
        if self.path.startswith('/api/download/'):
            # Docker 环境：通过 HTTP 下载文件（Content-Disposition: attachment）
            return self._send_download(self.path[len('/api/download/'):])
        if self.path == '/api/g-data':
            vid = self._vid()
            entries, profile, unlocked, history, today_keys, today_date, festival = blindbox_state(vid)
            sh_dir = load_shares().get('dir') or ''
            comments = comments_of(guest_comments(sh_dir) if sh_dir else {}, vid)
            plays = visitor_plays(sh_dir, vid) if sh_dir else {}
            bonus, msgs = visitor_grants(sh_dir, vid) if sh_dir else (0, [])
            was_opened_n = opened_count(sh_dir, vid)
            return self._send(200, {'comments': comments, 'plays': plays,
                                    'me': visitor_label(vid), 'bonus': bonus, 'msgs': msgs,
                                    'msg_history': visitor_msg_history(sh_dir, vid) if sh_dir else [],
                                    'opened_today': was_opened_n > 0, 'opened_n': was_opened_n,
                                    'unlocked': len(unlocked), 'total': len(entries)})
        if self.path in ('/', '/index.html'):
            # 盲盒端口根路径直接出盲盒页（PWA start_url='/'，与任务魔盒同构；
            # 之前 302 到 /guest——部分浏览器对跳转 start_url 的安装校验更苛刻）
            self._vid()  # 先落身份 Cookie（_extra_hdrs 由最终 _send 统一带上）
            self.path = '/guest'
        if (self.path == '/guest' or self.path.startswith('/g/') or
                self.path in ('/static/guest-manifest.json', '/static/icon-192.png',
                              '/static/icon-512.png')):
            return Handler.do_GET(self)
        return self._send(404, b'not found', 'text/plain; charset=utf-8')

    def do_POST(self):
        if self.path == '/api/g-login':
            # 自定义登录页提交：校验账号 → 种会话 Cookie → 回盲盒页
            try:
                raw = self.rfile.read(int(self.headers.get('Content-Length') or 0)).decode('utf-8')
                from urllib.parse import parse_qs
                qs = parse_qs(raw)
                user = (qs.get('user') or [''])[0].strip()
                pwd = (qs.get('pass') or [''])[0]
            except Exception:
                return self._send(400, {'error': '请求格式错误'})
            sh = load_shares()
            cfg = sh.get('auth') or {}
            mode = cfg.get('mode') or 'none'
            accounts = sh.get('accounts') or []
            ok = False
            for acc in accounts:
                if acc.get('disabled'):
                    continue
                if acc.get('user') != user:
                    continue
                # 密码按账号各管各的：没设密码的账号只输账号就能进；设了密码必须对上
                acc_pass = str(acc.get('pass') or '')
                if mode == 'user' or not acc_pass or acc_pass == pwd:
                    ok = True
                    break
            if ok and mode != 'none':
                token = _create_g_session(user)
                self.send_response(303)
                self.send_header('Location', '/guest')
                self.send_header('Set-Cookie',
                                 f'kg_sess={token}; Path=/; Max-Age={_G_SESS_TTL}; SameSite=Lax')
                self.send_header('Content-Length', '0')
                self.end_headers()
                return
            # 登录失败：回到登录页并提示
            html = self._login_html.replace('__ERR__', '账号或密码错误，请重试')
            return self._send(200, html.encode(), 'text/html; charset=utf-8')
        if not self._auth_ok():
            return self._send(401, {'error': '需要登录'})
        vid = self._vid()
        if self.path == '/api/g-open':
            # 开盒进度落到服务端（n=已拆开的张数）：换浏览器 / 清 Cookie 也不能重开；
            # uks=本轮已拆开的歌：若是机主加赠歌，记成"已拆"（一次性生效后永久解锁，不再回堆叠）
            sh_dir = load_shares().get('dir') or ''
            try:
                if sh_dir and vid:
                    p = {}
                    try:
                        p = json.loads(self.rfile.read(int(self.headers.get('Content-Length') or 0)) or b'{}')
                    except Exception:
                        p = {}
                    mark_opened(sh_dir, vid, p.get('n') or 1)
                    uks = p.get('uks') or []
                    if uks:
                        st, data = visitor_state(sh_dir, vid)
                        gset = {g.get('uk') for g in (st.get('grants') or [])}
                        go = st.setdefault('grants_opened', [])
                        changed = False
                        for uk in uks:
                            if uk in gset and uk not in go:
                                go.append(uk)
                                changed = True
                        if changed:
                            st['grants_opened'] = go[-120:]
                            data[vid] = st
                            save_visitors(sh_dir, data)
            except Exception:
                pass
            return self._send(200, {'ok': True})
        if self.path == '/api/g-seen-reply':
            # 访客看过机主的留言回复 → 本地记下即可（服务端只回 ok，避免重复弹）
            return self._send(200, {'ok': True})
        if self.path == '/api/g-seen':
            # 访客看过了机主消息 → 标记已读，下次不再重复弹
            try:
                p = json.loads(self.rfile.read(int(self.headers.get('Content-Length') or 0)))
                keys = set(p.get('keys') or [])
                sh_dir = load_shares().get('dir') or ''
                if sh_dir and keys:
                    st, data = visitor_state(sh_dir, vid)
                    for m in (st.get('msgs') or []):
                        if (m.get('at', '') + '|' + (m.get('text') or '')) in keys:
                            m['seen'] = True
                    data[vid] = st
                    save_visitors(sh_dir, data)
            except Exception:
                pass
            return self._send(200, {'ok': True})
        if self.path == '/api/g-comment':
            try:
                p = json.loads(self.rfile.read(int(self.headers.get('Content-Length') or 0)))
                entries, profile, unlocked, history, today_keys, today_date, festival = blindbox_state(vid)
                uk = str(p.get('uk') or '')
                if uk not in unlocked:
                    return self._send(403, {'error': '该歌曲尚未解锁'})
                add_guest_comment(load_shares().get('dir') or os.getcwd(), uk,
                                  str(p.get('name') or '匿名')[:20], str(p.get('text') or '')[:200],
                                  vid)
                return self._send(200, {'ok': True})
            except Exception as e:
                return self._send(400, {'error': str(e)})
        if self.path == '/api/g-play':
            try:
                p = json.loads(self.rfile.read(int(self.headers.get('Content-Length') or 0)))
                entries, profile, unlocked, history, today_keys, today_date, festival = blindbox_state(vid)
                uk = str(p.get('uk') or '')
                if uk not in unlocked:
                    return self._send(403, {'error': '该歌曲尚未解锁'})
                count = add_guest_play(load_shares().get('dir') or os.getcwd(), uk, vid)
                return self._send(200, {'ok': True, 'plays': count})
            except Exception as e:
                return self._send(400, {'error': str(e)})
        return self._send(404, b'not found', 'text/plain; charset=utf-8')




# ---------------------------------------------------------------- 定时自动更新

_auto_thread = None
_auto_stop = threading.Event()


def _auto_check_once():
    """执行一轮作者新歌检查（定时循环与「立即检查」共用）。完成后记录 last_check/last_result。"""
    cfg_path = os.path.join(os.getcwd(), 'auto_update.json')
    cfg = _load_json_safe(cfg_path)
    authors = cfg.get('authors') or []
    ok_n, err_msgs = 0, []
    for a in authors:
        if _auto_stop.is_set():
            break
        uid, out_dir = a.get('uid'), a.get('dir')
        if not uid or not out_dir or not os.path.isdir(out_dir):
            continue
        try:
            job = Job()
            from core import KGClient
            client = KGClient(cfg.get('cookie') or '', cfg.get('proxy') or '', 30, 3)
            client.share_uid = uid
            args = types.SimpleNamespace(
                out=out_dir, cookie=cfg.get('cookie') or '', cookie_file='',
                proxy=cfg.get('proxy') or '', workers=2, page_sleep=1.0,
                retries=3, timeout=30, max=0, since='', until='',
                no_audio=False, no_cover=False, no_comments=False, no_meta=False,
                no_html=False, no_tags=False, video=False, mp3=False, zip=False,
                list_only=False, force=False, flat=bool(cfg.get('flat')))
            from core import _backup_one
            _backup_one(uid, args, job)
            ok_n += 1
        except Exception as e:
            err_msgs.append((a.get('nickname') or uid) + '：' + str(e)[:40])
    cfg = _load_json_safe(cfg_path)
    cfg['last_check'] = ts2str(time.time())
    if err_msgs:
        cfg['last_result'] = f'{ok_n}/{len(authors)} 成功 · ' + '；'.join(err_msgs[:2])
    else:
        cfg['last_result'] = f'{ok_n}/{len(authors)} 位作者检查完成'
    _save_json_safe(cfg_path, cfg)


def _auto_update_loop():
    """定时检查订阅作者的新歌并自动下载。"""
    while not _auto_stop.is_set():
        try:
            cfg = _load_json_safe(os.path.join(os.getcwd(), 'auto_update.json'))
            if not cfg.get('enabled'):
                _auto_stop.wait(300)
                continue
            interval_h = max(1, cfg.get('interval_hours', 6))
            _auto_check_once()
            _auto_stop.wait(3600 * interval_h)
        except Exception:
            _auto_stop.wait(60)


def start_auto_update():
    global _auto_thread
    if _auto_thread and _auto_thread.is_alive():
        return
    _auto_stop.clear()
    _auto_thread = threading.Thread(target=_auto_update_loop, daemon=True)
    _auto_thread.start()

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
    # 盲盒独立端口：Docker 显式指定；桌面版默认也开 8766（被占用则自动跳过，/guest 仍可从工具页进）
    guest_port = int(os.environ.get('GUEST_PORT') or 8766)
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
    # 升级安全：先把老版本的数据搬过来，再给这个版本留一份快照
    moved = migrate_legacy_data()
    snap = snapshot_data()
    if sys.stdout and (moved or snap):
        print(f'💾 用户数据已保留（数据目录 {data_root()}）'
              + (f' · 迁移 {len(moved)} 个文件' if moved else '')
              + (f' · 已备份 {len(snap)} 个' if snap else ''), flush=True)
    import atexit
    atexit.register(remove_instance_file)
    threading.Thread(target=_queue_watch, daemon=True).start()
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
