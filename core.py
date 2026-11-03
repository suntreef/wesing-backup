#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""
core.py — 全民K歌个人作品批量备份：核心逻辑（CLI 与 GUI 共用）
==============================================================

功能：输入个人主页链接（或 uid），批量备份该账号的全部可见作品：
音频（原始码率不转码）、封面、MV 视频、上传时间、介绍文案、评论、
评分/播放/点赞/鲜花等全部详情，并生成 CSV 总目录 + 本地 HTML 歌单相册。

- 命令行入口：kg_backup.py（薄壳，调 cli_main）
- 图形界面入口：kg_app.py（本地服务器 + 浏览器页面，调 backup_one + Job）

歌词说明：歌词来自全民K歌公开歌词接口（LRC 格式），随备份保存为 .lrc 边车文件
并写入音频歌词标签，仅用于个人欣赏自己唱过的作品，请勿用于商业用途。

说明：
- 仅用于备份本人或经授权账号的作品，请勿用于批量抓取他人内容或商用。
- 音频保存服务端原始 AAC 流（不转码、不二次压缩），即公开渠道的最高音质。
- 私密作品（仅自己可见）需要登录 Cookie；未登录只能取公开作品。

依赖：requests；可选 ffmpeg（自动嵌入标签 / 生成 MP3 副本）
"""

import argparse
import concurrent.futures as cf
import csv
import datetime as dt
import html as html_mod
import io
import json
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from collections import deque
from urllib.parse import urlparse

try:
    import requests
except ImportError:
    print('缺少 requests 库，请先执行: pip install requests')
    sys.exit(1)

UA_MOBILE = ('Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) '
             'AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148 '
             'QQ/8.9.13.410 V1_IPH_SQ_8.9.13_1_APP_A Pixel/1170 Core/UIWebView '
             'Device/Apple(iPhone 14)')
REFERER = 'https://static-play.kg.qq.com/'
LIST_API = 'https://node.kg.qq.com/fcgi-bin/kg_ugc_get_homepage'
PLAY_URL = 'https://node.kg.qq.com/play?s={shareid}'
PAGE_SIZE = 15  # 服务端上限，更大的 num 会被拒绝

APP_NAME = '全民K歌备份工具'
APP_VERSION = '1.3.0'
DETAIL_API = 'https://node.kg.qq.com/fcgi-bin/kg_ugc_getdetail'


# ---------------------------------------------------------------- 基础工具

def log(msg):
    line = f'[{dt.datetime.now().strftime("%H:%M:%S")}] {msg}'
    if sys.stdout:
        try:
            print(line, flush=True)
        except Exception:
            pass


def _jlog(job, msg):
    if job is not None:
        job.log(msg)
    else:
        log(msg)


class Job:
    """一次备份任务的状态与日志（CLI 与 GUI 共用）。"""

    def __init__(self):
        self.state = 'idle'      # idle | listing | running | done | stopped | error
        self.error = ''
        self.cancelled = False
        self.paused = False
        self.out_dir = ''
        self.profile = {}
        self.stats = {'total_list': 0, 'new': 0, 'repair': 0,
                      'done': 0, 'ok': 0, 'fail': 0, 'index': 0, 'bytes': 0}
        self.logs = deque(maxlen=800)
        self.started_at = ''
        self.finished_at = ''

    def log(self, msg):
        self.logs.append((dt.datetime.now().strftime('%H:%M:%S'), str(msg)))
        log(msg)

    def snapshot(self):
        return {'state': self.state, 'error': self.error, 'cancelled': self.cancelled,
                'paused': self.paused,
                'out_dir': self.out_dir, 'profile': self.profile, 'stats': self.stats,
                'started_at': self.started_at, 'finished_at': self.finished_at,
                'logs': list(self.logs)}


def safe_name(text, maxlen=60):
    text = re.sub(r'[\\/:*?"<>|\'\r\n\t]', '_', str(text)).strip(' .')
    return re.sub(r'\s+', ' ', text)[:maxlen] or 'untitled'


def ts2str(ts):
    try:
        return dt.datetime.fromtimestamp(int(ts)).strftime('%Y-%m-%d %H:%M:%S')
    except (TypeError, ValueError, OSError):
        return ''


def calc_gtk(skey):
    """QQ 经典 g_tk 算法；无 skey 时返回 5381（未登录默认值）。"""
    if not skey:
        return 5381
    h = 5381
    for ch in skey:
        h += (h << 5) + ord(ch)
    return h & 0x7FFFFFFF


def parse_uid(text):
    text = text.strip()
    # uid 实测 15~18 位十六进制（如 639d9986202534 / 639f9b862028378b37），放宽到 10~24
    m = re.search(r'[?&]uid=([0-9A-Fa-f]{10,24})', text)
    if m:
        return m.group(1)
    m = re.search(r'\b([0-9A-Fa-f]{10,24})\b', text)
    if m:
        return m.group(1)
    raise ValueError(f'无法从输入中解析 uid：{text!r}\n'
                     '请粘贴个人主页链接（…personal?uid=xxx）或直接给 uid。')


def extract_json_after(text, marker):
    """提取 marker= 之后第一个大括号平衡的 JSON 串。"""
    idx = text.find(marker)
    if idx < 0:
        return None
    start = text.find('{', idx)
    if start < 0:
        return None
    depth, in_str, esc = 0, False, False
    for i in range(start, len(text)):
        c = text[i]
        if in_str:
            if esc:
                esc = False
            elif c == '\\':
                esc = True
            elif c == '"':
                in_str = False
        else:
            if c == '"':
                in_str = True
            elif c == '{':
                depth += 1
            elif c == '}':
                depth -= 1
                if depth == 0:
                    return text[start:i + 1]
    return None


def https(url):
    return 'https://' + url[7:] if url.startswith('http://') else url


# ---------------------------------------------------------------- 网络客户端

class KGClient:
    def __init__(self, cookie='', proxy='', timeout=30, retries=3):
        self.s = requests.Session()
        self.s.headers.update({'User-Agent': UA_MOBILE, 'Referer': REFERER})
        if cookie:
            self.s.headers['Cookie'] = cookie.strip().removeprefix('Cookie:').strip()
        if proxy:
            self.s.proxies = {'http': proxy, 'https': proxy}
        self.timeout = timeout
        self.retries = retries
        self.bytes_downloaded = 0
        self.share_uid = ''
        self.login_uid = ''
        skey = ''
        if cookie:
            m = re.search(r'(?:^|;\s*)skey=([^;]+)', cookie)
            if m:
                skey = m.group(1).strip('"')
            m2 = re.search(r'(?:^|;\s*)uid=(\d+)', cookie)
            if m2:
                self.login_uid = m2.group(1)
        self.gtk = calc_gtk(skey)

    def _get(self, url, **kw):
        last = None
        for attempt in range(1, self.retries + 1):
            try:
                r = self.s.get(url, timeout=self.timeout, **kw)
                if r.status_code == 200:
                    return r
                last = RuntimeError(f'HTTP {r.status_code}')
            except requests.RequestException as e:
                last = e
            if attempt < self.retries:
                time.sleep(1.5 * attempt)
        raise last

    def get_work_page(self, uid, page):
        params = {
            'outCharset': 'utf-8', 'from': '1',
            'nocache': str(int(time.time() * 1000)),
            'format': 'json', 'type': 'get_uinfo',
            'start': str(page), 'num': str(PAGE_SIZE),
            'share_uid': uid, 'g_tk': str(self.gtk), 'g_tk_openkey': str(self.gtk),
        }
        r = self._get(LIST_API, params=params)
        j = r.json()
        if j.get('code') != 0:
            raise RuntimeError(f'列表接口异常: code={j.get("code")} {j.get("message", "")}')
        return j['data']

    def fetch_detail(self, shareid):
        """优先走 JSON 详情接口（结构稳定）；失败回退到播放页 HTML 解析。"""
        try:
            r = self._get(DETAIL_API, params={
                'v': 4, 'shareid': shareid, 'shareuid': self.share_uid,
                'login_uid': self.login_uid, 'format': 'json'})
            t = r.text.strip()
            if t.startswith('jsonpcallback('):
                t = t[len('jsonpcallback('):].rstrip(')').rstrip(';')
            j = json.loads(t)
            if j.get('code') == 0:
                d = j.get('data') or {}
                det = d.get('detail') or d
                if isinstance(det, dict) and (det.get('playurl') is not None or det.get('ctime')):
                    return det, PLAY_URL.format(shareid=shareid)
        except (requests.RequestException, ValueError, json.JSONDecodeError):
            pass
        return self._fetch_detail_html(shareid)

    def _fetch_detail_html(self, shareid):
        url = PLAY_URL.format(shareid=shareid)
        r = self._get(url)
        raw = extract_json_after(r.text, 'window.__FETCH_RES__')
        if raw:
            try:
                j = json.loads(raw)
                res = j.get('ugcDetailRes') or j.get('data') or j
                if isinstance(res, dict):
                    if res.get('code') == 0 and isinstance(res.get('data'), dict):
                        return res['data'], url
                    if res.get('playurl') is not None:
                        return res, url
            except json.JSONDecodeError:
                pass
        raw2 = extract_json_after(r.text, 'window.__DATA__')
        if raw2:
            try:
                j = json.loads(raw2)
                det = (j.get('data') or {}).get('detail') or j.get('data') or {}
                if det.get('playurl') is not None or det.get('ctime'):
                    return det, url
            except json.JSONDecodeError:
                pass
        m = re.search(r'"playurl"\s*:\s*"([^"]+)"', r.text)
        if m:
            return {'playurl': m.group(1)}, url
        raise RuntimeError('播放页未找到详情数据（可能已删除/仅APP可见/结构变更）')

    def fetch_lyric(self, ksong_mid):
        """取原唱歌词（标准 LRC 文本）。取不到返回 ''。"""
        if not ksong_mid:
            return ''
        try:
            r = self._get('https://node.kg.qq.com/fcgi-bin/fcg_lyric',
                          params={'ksongmid': ksong_mid, 'format': 'json'})
            j = r.json()
            if j.get('code') == 0:
                return (j.get('data') or {}).get('lyric') or ''
        except (requests.RequestException, ValueError, json.JSONDecodeError):
            pass
        return ''

    def download(self, url, path, min_size=1000):
        last = None
        for attempt in range(1, self.retries + 1):
            try:
                with self.s.get(url, stream=True, timeout=(15, 180)) as r:
                    if r.status_code != 200:
                        raise RuntimeError(f'HTTP {r.status_code}')
                    ct = (r.headers.get('Content-Type') or '').lower()
                    if 'text/html' in ct:
                        raise RuntimeError('返回 HTML 而非媒体文件（链接已过期或被拒）')
                    tmp = path + '.part'
                    n = 0
                    with open(tmp, 'wb') as f:
                        for chunk in r.iter_content(chunk_size=1 << 16):
                            f.write(chunk)
                            n += len(chunk)
                    if n < min_size:
                        raise RuntimeError(f'文件过小({n}B)')
                    os.replace(tmp, path)
                    self.bytes_downloaded += n
                    return n
            except (requests.RequestException, RuntimeError) as e:
                last = e
                if os.path.exists(path + '.part'):
                    try:
                        os.remove(path + '.part')
                    except OSError:
                        pass
                if attempt < self.retries:
                    time.sleep(2 * attempt)
        raise last


# ---------------------------------------------------------------- 元数据嵌入

def ffmpeg_available():
    return shutil.which('ffmpeg') is not None


def embed_tags(audio_path, cover_path, title, artist, date_str, comment, lyric='', desc='', job=None):
    """ffmpeg 无损重封装写入标签（-c copy 不重编码，不损失音质）。"""
    tmp_fd, tmp_path = tempfile.mkstemp(suffix='.m4a', dir=os.path.dirname(audio_path) or '.')
    os.close(tmp_fd)
    cmd = ['ffmpeg', '-y', '-v', 'error', '-i', audio_path]
    if cover_path and os.path.exists(cover_path):
        cmd += ['-i', cover_path, '-map', '0:a', '-map', '1:v',
                '-c:v', 'copy', '-disposition:v:0', 'attached_pic']
    else:
        cmd += ['-map', '0:a']
    cmd += ['-c:a', 'copy',
            '-metadata', f'title={title}',
            '-metadata', f'artist={artist}',
            '-metadata', 'album=全民K歌',
            '-metadata', f'date={date_str}']
    if comment:
        cmd += ['-metadata', f'comment={comment}']
    if lyric:
        cmd += ['-metadata', f'lyrics={lyric}']
    # description 覆盖服务端残留字段，写入原唱/评分等关键信息
    if desc:
        cmd += ['-metadata', f'description={desc}']
    else:
        cmd += ['-map_metadata', '-1']
    cmd += ['-f', 'ipod', tmp_path]
    try:
        p = subprocess.run(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                           stderr=subprocess.PIPE, timeout=180)
        if p.returncode != 0:
            raise RuntimeError(p.stderr.decode('utf-8', 'replace')[-300:])
        os.replace(tmp_path, audio_path)
        return True
    except Exception as e:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        _jlog(job, f'    标签嵌入失败（保留原始音频）: {e}')
        return False


# ---------------------------------------------------------------- MP3 副本

def convert_mp3(out, entries, job=None):
    """为兼容车机/老设备生成 MP3 副本（192kbps CBR）；m4a 原件仍是母版。"""
    if not ffmpeg_available():
        _jlog(job, '未检测到 ffmpeg，跳过 MP3 副本（安装 ffmpeg 后重跑即可）')
        return
    os.makedirs(os.path.join(out, 'mp3'), exist_ok=True)
    conv = skip = 0
    for e in entries:
        src = e.get('audio') or ''
        if not src.endswith('.m4a'):
            continue
        src_abs = os.path.join(out, src)
        if not os.path.exists(src_abs):
            continue
        rel = 'mp3/' + os.path.basename(src)[:-4] + '.mp3'
        dst_abs = os.path.join(out, rel)
        e['mp3'] = rel
        if os.path.exists(dst_abs):
            skip += 1
            continue
        r = subprocess.run(['ffmpeg', '-y', '-v', 'error', '-i', src_abs, '-b:a', '192k', dst_abs],
                           stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL)
        if r.returncode == 0:
            conv += 1
        else:
            e.pop('mp3', None)
            try:
                os.remove(dst_abs)
            except OSError:
                pass
    _jlog(job, f'MP3 副本：新转换 {conv}，已有 {skip}（192kbps，原 m4a 保留为母版）')


# ---------------------------------------------------------------- 业务处理

def collect_photos(detail):
    urls = []
    for key in ('photos', 'vec_rich_pic'):
        for item in detail.get(key) or []:
            if isinstance(item, str) and item.startswith('http'):
                urls.append(item)
            elif isinstance(item, dict):
                urls.extend(v for v in item.values()
                            if isinstance(v, str) and v.startswith('http'))
    seen, out = set(), []
    for u in urls:
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out


def process_work(client, out_dir, seq, item, opts, force=False):
    """单首作品：详情 → 音频/封面/MV/配图 → 详情JSON → 标签。失败抛异常。"""
    job = opts.get('job')
    flat = opts.get('flat')

    def place(kind, name):
        """flat 模式：全部文件落在根目录（同前缀成组）；否则按子目录归档。"""
        return name if flat else f'{kind}/{name}'

    shareid = item.get('shareid') or ''
    detail, web_url = client.fetch_detail(shareid)
    time.sleep(random.uniform(0.2, 0.5))

    # shareid 是每次请求动态生成的令牌；ugc_id 才是作品稳定 ID
    # force=True（补下任务）跳过去重保护，否则会被"已备份过"挡住
    ugcid = detail.get('ugc_id') or ''
    if not force and ugcid and ugcid in opts.get('seen_ugcids', set()):
        return None

    ctime = detail.get('ctime') or item.get('time') or 0
    date_str = ts2str(ctime)
    date_tag = date_str[:10].replace('-', '') or 'nodate'
    title = detail.get('title') or item.get('title') or '未命名'
    base = f'{seq:04d}_{date_tag}_{safe_name(title)}'

    audio_url = https((detail.get('playurl') or '').strip())
    video_url = https((detail.get('playurl_video') or '').strip())
    cover_url = detail.get('cover') or detail.get('fb_cover') or ''
    audio_path = video_path = cover_path = ''

    def need(rel):
        return opts.get('force') or not os.path.exists(os.path.join(out_dir, rel))

    if audio_url and not opts['no_audio']:
        ext = '.mp4' if '.mp4' in os.path.basename(urlparse(audio_url).path) else '.m4a'
        audio_path = place('audio', f'{base}{ext}')
        if need(audio_path):
            client.download(audio_url, os.path.join(out_dir, audio_path))
    elif video_url and not opts['no_audio']:
        video_path = place('audio', f'{base}_MV.mp4')
        if need(video_path):
            client.download(video_url, os.path.join(out_dir, video_path))
    if video_url and opts['want_video'] and audio_url:
        video_path = place('audio', f'{base}_MV.mp4')
        if need(video_path):
            client.download(video_url, os.path.join(out_dir, video_path))

    if cover_url and not opts['no_cover']:
        cover_path = place('covers', f'{base}.jpg')
        if need(cover_path):
            try:
                client.download(cover_url, os.path.join(out_dir, cover_path))
            except Exception as e:
                _jlog(job, f'    封面下载失败（跳过）: {e}')
                cover_path = ''

    photos = collect_photos(detail)
    photo_files = []
    if photos and not opts['no_cover']:
        for i, pu in enumerate(photos[:10]):
            try:
                pf = place('covers', f'{base}_p{i + 1}.jpg')
                if not need(pf):
                    photo_files.append(pf)
                    continue
                client.download(pu, os.path.join(out_dir, pf))
                photo_files.append(pf)
            except Exception:
                pass

    # 歌词：LRC 边车文件 + 嵌入音频标签
    lyric_text = ''
    lrc_path = ''
    if not opts['no_lyric'] and audio_path.endswith('.m4a'):
        ksong_mid = detail.get('ksong_mid') or item.get('ksong_mid') or ''
        lyric_text = client.fetch_lyric(ksong_mid)
        if lyric_text:
            lrc_path = place('audio', f'{base}.lrc')
            if need(lrc_path):
                try:
                    with open(os.path.join(out_dir, lrc_path), 'w', encoding='utf-8') as f:
                        f.write(lyric_text)
                except OSError:
                    lrc_path = ''

    meta_rel = ''
    if not opts['no_meta']:
        meta_rel = place('meta', f'{base}.json')
        full = {'shareid': shareid, 'web_url': web_url, 'list_info': item,
                'detail': detail,
                'backup': {'seq': seq, 'lyric': lyric_text,
                           'files': {'audio': audio_path, 'video': video_path,
                                     'cover': cover_path, 'lrc': lrc_path,
                                     'photos': photo_files}}}
        with open(os.path.join(out_dir, meta_rel), 'w', encoding='utf-8') as f:
            json.dump(full, f, ensure_ascii=False, indent=1)

    tagged = False
    if opts['tags'] and audio_path.endswith('.m4a'):
        artist = detail.get('nick') or detail.get('kg_nick') or ''
        bits = []
        if detail.get('song_name'):
            singer = detail.get('singer_name') or ''
            bits.append(f'原唱：{singer}《{detail["song_name"]}》' if singer
                        else f'原唱：《{detail["song_name"]}》')
        if detail.get('score'):
            bits.append(f'评分 {detail["score"]}')
        if detail.get('play_num') is not None:
            bits.append(f'播放 {detail["play_num"]}')
        if detail.get('device'):
            bits.append(f'录制设备 {detail["device"]}')
        tagged = embed_tags(os.path.join(out_dir, audio_path),
                            os.path.join(out_dir, cover_path) if cover_path else '',
                            title, artist, date_str[:10], detail.get('content') or '',
                            lyric=lyric_text, desc=' · '.join(bits), job=job)

    entry = {
        'shareid': shareid,
        'ugcid': ugcid,
        'tkey': f"{item.get('time') or 0}_{item.get('ksong_mid') or ''}",
        'seq': seq,
        'title': title,
        'song_name': detail.get('song_name', ''),
        'singer_name': detail.get('singer_name', ''),
        'time': date_str,
        'duration_sec': round((detail.get('segment_end') or 0) / 1000) or None,
        'score': detail.get('score'),
        'score_rank': detail.get('scoreRank'),
        'play_num': detail.get('play_num', item.get('play_count')),
        'like': detail.get('uLikeCount'),
        'comment_num': detail.get('comment_num', item.get('coment_count')),
        'flower_num': detail.get('flower_num'),
        'gift_num': detail.get('gift_num', item.get('gift_count')),
        'is_duet': bool(detail.get('hc_uid')),
        'partner': detail.get('hc_nick', ''),
        'device': detail.get('tail_name', ''),
        'is_segment': bool(detail.get('is_segment')),
        'content': detail.get('content', ''),
        'web_url': web_url,
        'audio': audio_path or video_path,
        'video': video_path if (video_url and audio_url) else '',
        'cover': cover_path,
        'lrc': lrc_path,
        'photos': photo_files,
        'meta': meta_rel,
        'tagged': tagged,
    }
    if not opts['no_comments'] and detail.get('comments'):
        cpath = place('meta', f'{base}_comments.json')
        with open(os.path.join(out_dir, cpath), 'w', encoding='utf-8') as f:
            json.dump({'total': detail.get('total') or detail.get('comment_num'),
                       'count_in_backup': len(detail['comments']),
                       'comments': detail['comments']}, f, ensure_ascii=False, indent=1)
        entry['comments_file'] = cpath
    return entry


# ---------------------------------------------------------------- 索引导出

CSV_COLS = [
    ('seq', '序号'), ('title', '歌曲标题'), ('song_name', '原唱歌曲'),
    ('singer_name', '原唱歌手'), ('time', '上传时间'), ('duration_sec', '时长(秒)'),
    ('score', '评分(满分约5000)'), ('score_rank', '评分排名'), ('play_num', '播放量'),
    ('like', '点赞数'), ('comment_num', '评论数'), ('flower_num', '鲜花数'),
    ('gift_num', '礼物数'), ('is_duet', '是否合唱'), ('partner', '合唱搭档'),
    ('device', '录制设备'), ('is_segment', '是否片段'), ('content', '我的介绍'),
    ('audio', '音频/MV文件'), ('video', 'MV文件(另下)'), ('cover', '封面文件'),
    ('meta', '详情JSON'), ('web_url', '网页链接'), ('shareid', 'shareid'),
]


def write_csv(out, entries):
    path = os.path.join(out, 'index.csv')
    with open(path, 'w', encoding='utf-8-sig', newline='') as f:
        w = csv.writer(f)
        w.writerow([c[1] for c in CSV_COLS])
        for e in sorted(entries, key=lambda x: x['seq']):
            row = []
            for key, _ in CSV_COLS:
                v = e.get(key)
                if isinstance(v, bool):
                    v = '是' if v else ''
                row.append('' if v is None else v)
            w.writerow(row)
    return path


def write_list_csv(out, works):
    path = os.path.join(out, 'works_list.csv')
    with open(path, 'w', encoding='utf-8-sig', newline='') as f:
        w = csv.writer(f)
        w.writerow(['上传时间', '标题', '播放量', '评论数', '礼物数', '是否片段', 'shareid', '网页链接'])
        for it in sorted(works, key=lambda x: x.get('time') or 0):
            w.writerow([ts2str(it.get('time')), it.get('title', ''), it.get('play_count', ''),
                        it.get('coment_count', ''), it.get('gift_count', ''),
                        '是' if it.get('is_segment') else '',
                        it.get('shareid', ''), PLAY_URL.format(shareid=it.get('shareid', ''))])
    return path


def write_html(out, entries, profile, generated):
    path = os.path.join(out, 'index.html')

    def esc(s):
        return html_mod.escape(str(s if s is not None else ''))

    cards = []
    for e in sorted(entries, key=lambda x: x['seq'], reverse=True):
        cover_html = (f'<img loading="lazy" src="{esc(e["cover"])}" alt="">'
                      if e.get('cover') else '<div class="noimg">♪</div>')
        stats = []
        if e.get('play_num') is not None:
            stats.append(f'▶ {e["play_num"]}')
        if e.get('like'):
            stats.append(f'♥ {e["like"]}')
        if e.get('comment_num'):
            stats.append(f'💬 {e["comment_num"]}')
        if e.get('flower_num'):
            stats.append(f'🌹 {e["flower_num"]}')
        if e.get('score'):
            stats.append(f'★ {e["score"]}')
        content = esc(e.get('content'))
        duet = ('　👤 合唱：' + esc(e['partner'])) if e.get('is_duet') and e.get('partner') else ''
        player = (f'<audio controls preload="none" src="{esc(e["audio"])}"></audio>'
                  if e.get('audio') and e['audio'].endswith(('.m4a', '.mp4')) else '')
        cards.append(f'''<div class="card" data-title="{esc(e['title'])} {esc(e['song_name'])} {esc(e['singer_name'])} {content}">
  {cover_html}
  <div class="body">
    <div class="title"><a href="{esc(e['web_url'])}" target="_blank">{e['seq']:04d} {esc(e['title'])}</a></div>
    <div class="sub">{esc(e['song_name'])} · {esc(e['singer_name'])}</div>
    <div class="time">🕘 {esc(e['time'])}{duet}</div>
    {f'<div class="content">{content}</div>' if content else ''}
    <div class="stats">{'　'.join(stats)}</div>
    {player}
  </div>
</div>''')

    nick = esc(profile.get('nickname', ''))
    total = len(entries)
    plays = sum(int(e.get('play_num') or 0) for e in entries)
    durs = sum((e.get('duration_sec') or 0) for e in entries)
    years = sorted({e['time'][:4] for e in entries if e.get('time')})
    stat = f'共 {total} 首 · 总时长约 {durs // 60} 分钟'
    if years:
        stat += f' · {years[0]}–{years[-1]}'
    best = max(entries, key=lambda x: x.get('score') or 0, default=None)
    hot = max(entries, key=lambda x: x.get('play_num') or 0, default=None)
    if best and best.get('score'):
        stat += f" · 评分最高《{esc(best['title'])}》{best['score']}分"
    if hot and hot.get('play_num'):
        stat += f" · 最热《{esc(hot['title'])}》{hot['play_num']}次"
    doc = f'''<!DOCTYPE html>
<html lang="zh-cn"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{nick} 的全民K歌备份</title>
<style>
 body{{font-family:"Microsoft YaHei",system-ui,sans-serif;margin:0;background:#111418;color:#eee}}
 header{{padding:22px 24px 12px;position:sticky;top:0;background:rgba(17,20,24,.96);backdrop-filter:blur(6px);z-index:9;border-bottom:1px solid #2a2f36}}
 h1{{margin:0 0 6px;font-size:22px}}
 .meta{{color:#9aa3ad;font-size:13px}}
 #q{{margin-left:16px;padding:7px 12px;border-radius:8px;border:1px solid #3a4149;background:#1b2026;color:#eee;width:200px}}
 select{{padding:7px;border-radius:8px;border:1px solid #3a4149;background:#1b2026;color:#eee}}
 .grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:16px;padding:20px}}
 .card{{background:#1a1f26;border-radius:12px;overflow:hidden;display:flex;flex-direction:column}}
 .card img,.noimg{{width:100%;height:190px;object-fit:cover;display:flex;align-items:center;justify-content:center;font-size:48px;color:#555;background:#232932}}
 .body{{padding:12px 14px 14px;display:flex;flex-direction:column;gap:5px}}
 .title a{{color:#fff;font-weight:600;text-decoration:none;font-size:15px}}
 .title a:hover{{color:#7cb8ff}}
 .sub{{color:#aab4bf;font-size:13px}}
 .time,.stats{{color:#8b95a1;font-size:12px}}
 .content{{color:#cfd6dd;font-size:13px;background:#222932;border-left:3px solid #3f6ea5;padding:6px 9px;border-radius:0 6px 6px 0;white-space:pre-wrap}}
 audio{{width:100%;height:34px;margin-top:4px}}
 .hidden{{display:none}}
</style></head><body>
<header>
 <h1>{nick} 的全民K歌备份 <span class="meta">{stat} · 总播放 {plays}</span></h1>
 <div class="meta">备份时间 {esc(generated)} · 本地文件仅供个人收藏 ·
   <a href="cd.html" style="color:#ffd76a;text-decoration:none">✨ 复古CD播放器</a> ·
   <input id="q" placeholder="搜索歌名/介绍…" oninput="flt()">
   <select onchange="flt()"><option value="new">最新在前</option><option value="old">最早在前</option><option value="hot">播放最多</option><option value="score">评分最高</option></select>
 </div>
</header>
<div class="grid" id="g">{''.join(cards)}</div>
<script>
function num(c){{var t=c.querySelector('.stats').textContent.match(/▶ (\\d+)/);return t?+t[1]:0;}}
function scoreNum(c){{var t=c.querySelector('.stats').textContent.match(/★ (\\d+)/);return t?+t[1]:0;}}
function seqNum(c){{var t=c.querySelector('.title a').textContent.match(/^(\\d+)/);return t?+t[1]:0;}}
function flt(){{
 var q=document.getElementById('q').value.toLowerCase();
 var m=document.querySelector('select').value;
 var cards=[...document.querySelectorAll('.card')];
 cards.forEach(c=>c.classList.toggle('hidden', !!q && !c.dataset.title.toLowerCase().includes(q)));
 if(m==='old'){{cards.sort((a,b)=>seqNum(a)-seqNum(b));}}
 else if(m==='hot'){{cards.sort((a,b)=>num(b)-num(a));}}
 else if(m==='score'){{cards.sort((a,b)=>scoreNum(b)-scoreNum(a));}}
 else{{cards.sort((a,b)=>seqNum(b)-seqNum(a));}}
 cards.forEach(c=>document.getElementById('g').appendChild(c));
}}
</script></body></html>'''
    with open(path, 'w', encoding='utf-8') as f:
        f.write(doc)
    return path


# ---------------------------------------------------------------- 备份主流程

def backup_one(target, args, job=None):
    """备份一个账号。job 用于进度回调与取消（GUI）；CLI 传 None 自动创建。"""
    job = job or Job()
    job.started_at = ts2str(time.time())
    try:
        _backup_one(target, args, job)
        job.state = 'stopped' if job.cancelled else 'done'
        job.finished_at = ts2str(time.time())
    except Exception as e:
        job.state = 'error'
        job.error = str(e)
        job.finished_at = ts2str(time.time())
        raise
    return job


def _backup_one(target, args, job):
    uid = parse_uid(target)
    cookie = args.cookie
    if getattr(args, 'cookie_file', ''):
        with open(args.cookie_file, encoding='utf-8-sig') as f:
            cookie = f.read().strip()

    client = KGClient(cookie, args.proxy, args.timeout, args.retries)
    client.share_uid = uid
    job.log(f'目标 uid: {uid}（{"已带 Cookie，可含私密作品" if cookie else "未登录，仅公开作品"}）')

    job.state = 'listing'
    d1 = client.get_work_page(uid, 1)
    profile = {'uid': uid, 'nickname': d1.get('nickname', ''),
               'head_img_url': d1.get('head_img_url', ''),
               'ugc_total_count': d1.get('ugc_total_count'), 'level': d1.get('level'),
               'levelname': d1.get('levelname'), 'follower': d1.get('follower'),
               'following': d1.get('following'), 'fetched_at': ts2str(time.time())}
    job.profile = profile
    job.log(f"账号: {profile['nickname'] or uid} · 可见作品总数: {profile['ugc_total_count']}")

    out = args.out or f'kg_backup_{safe_name(profile["nickname"] or uid, 30)}'
    os.makedirs(out, exist_ok=True)
    if not args.list_only and not getattr(args, 'flat', False):
        for sub in ('audio', 'covers', 'meta'):
            os.makedirs(os.path.join(out, sub), exist_ok=True)
    if profile.get('head_img_url') and not args.list_only:
        try:
            client.download(profile['head_img_url'], os.path.join(out, 'avatar.jpg'), min_size=100)
        except Exception:
            pass
    job.out_dir = out

    idx_path = os.path.join(out, 'index.json')
    index = {}
    if os.path.exists(idx_path) and not args.force:
        try:
            with open(idx_path, encoding='utf-8') as f:
                index = json.load(f).get('works', {})
            if index:
                # 统一键名：tkey(稳定键) > ugcid > 原键，避免同一作品多个旧条目
                index = {(e.get('tkey') or e.get('ugcid') or k): e
                         for k, e in index.items()}
                job.log(f'增量模式：已有 {len(index)} 首备份（--force 强制重来）')
        except Exception:
            index = {}

    # 翻页抓全列表
    works = list(d1.get('ugclist') or [])
    page, has_more, misses = 2, d1.get('has_more'), 0
    while has_more and page <= 3000:
        if job.cancelled:
            job.log('已取消，停止翻页')
            break
        time.sleep(args.page_sleep)
        try:
            d = client.get_work_page(uid, page)
            misses = 0
        except Exception as e:
            misses += 1
            if misses >= 3:
                job.log(f'连续 3 页失败，停止翻页: {e}')
                break
            page += 1
            continue
        wl = d.get('ugclist') or []
        if not wl:
            break
        works.extend(wl)
        job.log(f'  列表进度 {len(works)} 首…')
        if not d.get('has_more'):
            has_more = False
            break
        has_more = d.get('has_more')
        page += 1
    job.log(f'列表抓取完成：{len(works)} 首（账号显示 {profile["ugc_total_count"]}；'
            f'差值一般是 仅自己可见 的私密作品，需登录 Cookie 才能取到）')

    seen, uniq = set(), []
    for w in works:
        tkey = f"{w.get('time') or 0}_{w.get('ksong_mid') or ''}"
        if w.get('shareid') and tkey not in seen:
            seen.add(tkey)
            uniq.append(w)
    uniq.sort(key=lambda w: w.get('time') or 0)
    job.stats['total_list'] = len(uniq)

    # 增量跳过（tkey 稳定键 + 旧索引兼容），并检测"已备份但文件丢失"的自动补下
    legacy_key = {(e.get('time'), e.get('title')): k
                  for k, e in index.items() if not e.get('tkey')}
    seen_ugcids = {e.get('ugcid') for e in index.values() if e.get('ugcid')}

    since = args.since and dt.date.fromisoformat(args.since)
    until = args.until and dt.date.fromisoformat(args.until)
    todo, repair = [], []
    for w in uniq:
        tkey = f"{w.get('time') or 0}_{w.get('ksong_mid') or ''}"
        old_key, e = None, index.get(tkey)
        if e is None:
            old_key = legacy_key.get((ts2str(w.get('time')), w.get('title')))
            if old_key:
                e = index[old_key]
        if e is not None:
            # 已备份过：音频文件还在就跳过，丢了就登记补下（不受 --max 限制）
            if e.get('audio') and not os.path.exists(os.path.join(out, e['audio'])):
                repair.append((w, e.get('seq'), old_key))
            continue
        t = w.get('time')
        if t and (since or until):
            d = dt.date.fromtimestamp(t)
            if (since and d < since) or (until and d > until):
                continue
        todo.append(w)
    if args.max:
        todo = todo[:args.max]
    if repair:
        job.log(f'检测到 {len(repair)} 首已备份但音频缺失，自动补下')
    plan = [(w, None, None) for w in todo] + repair

    if args.list_only:
        csv_path = write_list_csv(out, todo if (since or until or args.max) else uniq)
        job.log(f'清单模式完成：{len(todo if (since or until or args.max) else uniq)} 首 → {csv_path}')
        return

    job.stats['new'] = len(todo)
    job.stats['repair'] = len(repair)
    job.log(f'本次待处理 {len(plan)} 首（新增 {len(todo)}，补下 {len(repair)}），'
            f'并发 {args.workers}，标签嵌入{"开" if not args.no_tags and ffmpeg_available() else "关"}')
    job.state = 'running'
    if job.cancelled:
        job.log('已取消')
        return

    opts = {'no_audio': args.no_audio, 'no_cover': args.no_cover,
            'no_comments': args.no_comments, 'no_meta': args.no_meta,
            'no_lyric': getattr(args, 'no_lyric', False),
            'flat': getattr(args, 'flat', False),
            'want_video': args.video, 'seen_ugcids': seen_ugcids,
            'force': args.force, 'job': job,
            'tags': (not args.no_tags) and ffmpeg_available()}
    lock = threading.Lock()
    counter = {'seq': max([e.get('seq', 0) for e in index.values()] or [0])}
    fail_map = {}  # shareid -> {'plan_item','title','shareid','error'}（重试成功会移除）

    def task(plan_item, is_retry=False):
        # 暂停 = 挂在此处等待（每首歌是原子单位，暂停不会撕裂下载）
        while job.paused and not job.cancelled:
            time.sleep(0.3)
        if job.cancelled:
            return
        item, force_seq, old_key = plan_item
        sid = item.get('shareid') or ''
        with lock:
            if force_seq:
                seq = force_seq  # 补下文件沿用原序号，文件名不变
            else:
                counter['seq'] += 1
                seq = counter['seq']
        try:
            e = process_work(client, out, seq, item, opts, force=bool(force_seq))
            with lock:
                if e is None:
                    if not is_retry:
                        job.stats['done'] += 1
                    job.log(f"  ○ {item.get('title')}: 重复作品，跳过")
                    return
                if old_key and old_key in index and old_key != e['tkey']:
                    del index[old_key]
                index[e['tkey']] = e
                if e['ugcid']:
                    opts['seen_ugcids'].add(e['ugcid'])
                fail_map.pop(sid, None)
                job.stats['ok'] += 1
                if not is_retry:
                    job.stats['done'] += 1
                job.log(f"  {'✓ [重试成功] ' if is_retry else '✓ '}"
                        f"[{job.stats['done']}/{len(plan)}] #{seq:04d} {e['title']}"
                        + ('（仅MV）' if e['audio'].endswith('_MV.mp4') else '')
                        + ('（补下）' if force_seq else ''))
        except Exception as ex:
            with lock:
                fail_map[sid] = {'plan_item': plan_item, 'shareid': sid,
                                 'title': item.get('title'), 'error': str(ex)}
                if not is_retry:
                    job.stats['done'] += 1
                    job.stats['fail'] += 1
                    job.log(f"  ✗ [{job.stats['done']}/{len(plan)}] {item.get('title')}: {ex}"
                            f"（多数为瞬时网络问题，稍后自动重试）")
                else:
                    job.log(f"  ✗ [重试仍失败] {item.get('title')}: {ex}（已记入 failed.json，重跑命令可再补）")

    if plan:
        with cf.ThreadPoolExecutor(max_workers=max(1, args.workers)) as ex:
            list(ex.map(task, plan))

    # 整批跑完后，把 ✗ 的当场自动重试一轮（多为瞬时网络/链接过期，换个新请求往往就好）
    if fail_map and not job.cancelled:
        retry = [rec['plan_item'] for rec in fail_map.values()]
        pass1_fail = len(fail_map)
        job.log(f'—— {len(retry)} 首失败，自动重试一轮 ——')
        with cf.ThreadPoolExecutor(max_workers=max(1, args.workers)) as ex:
            list(ex.map(lambda pi: task(pi, True), retry))
        rescued = pass1_fail - len(fail_map)
        if rescued > 0:
            job.log(f'✅ 首轮失败的 {pass1_fail} 首中，{rescued} 首已自动重试救回，'
                    f'文件正常无需处理；剩余 {len(fail_map)} 首见 failed.json')

    job.stats['fail'] = len(fail_map)
    job.stats['index'] = len(index)
    job.stats['bytes'] = client.bytes_downloaded

    if job.cancelled:
        job.log('用户已停止，正在保存已完成的进度…')

    if args.mp3:
        convert_mp3(out, list(index.values()), job=job)

    profile['backup_count'] = len(index)
    generated = ts2str(time.time())
    with open(idx_path, 'w', encoding='utf-8') as f:
        json.dump({'profile': profile, 'generated': generated, 'works': index},
                  f, ensure_ascii=False, indent=1)
    with open(os.path.join(out, 'profile.json'), 'w', encoding='utf-8') as f:
        json.dump(profile, f, ensure_ascii=False, indent=1)

    entries = list(index.values())
    csv_path = write_csv(out, entries) if entries else ''
    html_path = write_html(out, entries, profile, generated) if entries and not args.no_html else ''
    cd_path = write_cd_page(out, entries, profile, generated) if entries and not args.no_html else ''

    zip_path = ''
    if args.zip and entries:
        parent = os.path.dirname(os.path.abspath(out))
        zp = shutil.make_archive(os.path.abspath(out), 'zip',
                                 root_dir=parent, base_dir=os.path.basename(out))
        zip_path = f'{zp}（{os.path.getsize(zp) / 1048576:.0f}MB）'

    job.log('=' * 56)
    job.log(f'完成：本次成功 {job.stats["ok"]}，失败 {job.stats["fail"]}，索引总计 {len(index)} 首')
    if client.bytes_downloaded:
        job.log(f'本次下载: {client.bytes_downloaded / 1048576:.1f} MB')
    if profile.get('ugc_total_count') and len(uniq) < profile['ugc_total_count'] and cookie:
        job.log(f'注意：有 {profile["ugc_total_count"] - len(uniq)} 首未能获取——'
                f'Cookie 可能已过期（重新复制一次），或这些作品仅 App 内可见')
    job.log(f'输出目录: {os.path.abspath(out)}')
    if csv_path:
        job.log(f'目录清单: {csv_path}')
    if html_path:
        job.log(f'HTML歌单: {html_path} （双击用浏览器打开即可试听）')
    if cd_path:
        job.log(f'CD播放器: {cd_path} （复古CD机 · 歌词滚动 · 可与歌单互相切换）')
    if zip_path:
        job.log(f'ZIP打包: {zip_path}')
    if fail_map:
        fp = os.path.join(out, 'failed.json')
        with open(fp, 'w', encoding='utf-8') as f:
            json.dump([{'shareid': r['shareid'], 'title': r['title'], 'error': r['error']}
                       for r in fail_map.values()], f, ensure_ascii=False, indent=1)
        job.log(f'失败清单: {fp}（直接重跑本命令即可自动重试失败项）')


# ---------------------------------------------------------------- CD 播放器页面

CD_TEMPLATE = r"""<!DOCTYPE html>
<html lang="zh-cn"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>__NICK__ · 复古CD机 · 全民K歌备份</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{background:radial-gradient(1100px 700px at 72% -12%,#1d2233,#0b0d13 62%);color:#e8eaf0;height:100vh;
 font-family:-apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei",sans-serif;overflow:hidden;display:flex;flex-direction:column}
canvas#bg{position:fixed;inset:0;z-index:0}
header{position:relative;z-index:2;display:flex;align-items:baseline;gap:14px;padding:14px 26px 6px;color:#9aa3b2;font-size:13px}
header b{color:#fff;font-size:17px}
header a{color:#7cb8ff;text-decoration:none;margin-left:auto}
main{position:relative;z-index:1;flex:1;display:flex;gap:30px;padding:6px 30px 0;min-height:0}
.deck{flex:0 0 400px;display:flex;flex-direction:column;align-items:center;justify-content:center;position:relative}
.cdwrap{position:relative;width:330px;height:330px;margin-top:6px}
.cd{position:absolute;inset:0;border-radius:50%;background:radial-gradient(circle,#181b22 62%,#20242e 63%,#0e1015 100%);
 box-shadow:0 18px 60px rgba(0,0,0,.65),0 0 0 1px rgba(255,255,255,.06) inset;overflow:hidden}
.cd .cover{position:absolute;inset:0;border-radius:50%;object-fit:cover;
 animation:spin 14s linear infinite;animation-play-state:paused;will-change:transform}
.cd.playing .cover{animation-play-state:running}
.cd .glare{position:absolute;inset:0;border-radius:50%;
 background:conic-gradient(from 210deg,rgba(255,255,255,.14),rgba(255,255,255,.02) 40deg,rgba(255,255,255,.16) 80deg,rgba(255,255,255,.03) 130deg,rgba(255,255,255,.15) 190deg,rgba(255,255,255,.02) 260deg,rgba(255,255,255,.14) 320deg);
 mix-blend-mode:screen;animation:spin 14s linear infinite;animation-play-state:paused}
.cd.playing .glare{animation-play-state:running}
.cd .hole{position:absolute;left:50%;top:50%;width:64px;height:64px;margin:-32px;
 border-radius:50%;background:#0b0d13;border:1px solid rgba(255,255,255,.15);z-index:3}
.cd .hole::after{content:'';position:absolute;inset:18px;border-radius:50%;background:#171b24}
@keyframes spin{to{transform:rotate(360deg)}}
.arm{position:absolute;right:-26px;top:-34px;width:200px;height:300px;z-index:4;pointer-events:none;
 transform:rotate(-24deg);transform-origin:78% 14%;transition:transform .7s cubic-bezier(.2,.8,.2,1)}
.cdwrap.playing .arm{transform:rotate(6deg)}
.now{margin-top:26px;text-align:center;max-width:360px}
.now .t{font-size:20px;font-weight:700;color:#fff}
.now .s{color:#8f9aac;font-size:13px;margin-top:5px}
.now .c{color:#6b7484;font-size:12px;margin-top:9px;line-height:1.6;max-height:52px;overflow:hidden}
.side{flex:1;display:flex;flex-direction:column;min-width:0}
.lyrics{flex:1;overflow-y:auto;padding:26px 8px;text-align:center;scroll-behavior:smooth;
 -webkit-mask-image:linear-gradient(transparent,#fff 14%,#fff 86%,transparent);
 mask-image:linear-gradient(transparent,#fff 14%,#fff 86%,transparent)}
.lyrics p{padding:9px 4px;color:#5c6675;font-size:15px;transition:all .25s;cursor:default}
.lyrics p.on{color:#ffd76a;font-size:18px;font-weight:600;text-shadow:0 0 18px rgba(255,215,106,.35)}
.ctrl{display:flex;align-items:center;gap:14px;padding:10px 2px 4px}
.ctrl button{background:none;border:1px solid #333a47;color:#dfe3ec;border-radius:50%;width:44px;height:44px;
 font-size:17px;cursor:pointer;transition:.15s}
.ctrl button:hover{border-color:#ffd76a;color:#ffd76a}
.ctrl .big{width:58px;height:58px;font-size:21px;background:#ffd76a;color:#111;border:none;font-weight:700}
.time{color:#8f9aac;font-size:12px;font-variant-numeric:tabular-nums}
#seek{flex:1;appearance:none;height:4px;border-radius:4px;background:#262c38;outline:none}
#seek::-webkit-slider-thumb{appearance:none;width:13px;height:13px;border-radius:50%;background:#ffd76a;cursor:pointer}
.strip{position:relative;z-index:1;flex:none;height:128px;display:flex;align-items:center;gap:12px;
 overflow-x:auto;padding:8px 26px 14px;background:rgba(255,255,255,.025)}
.strip::-webkit-scrollbar{height:6px}
.strip::-webkit-scrollbar-thumb{background:#2a3040;border-radius:6px}
.pl{flex:none;width:92px;cursor:pointer;text-align:center}
.pl img{width:86px;height:86px;border-radius:10px;object-fit:cover;opacity:.62;transition:.2s;border:2px solid transparent}
.pl:hover img{opacity:1}
.pl.on img{opacity:1;border-color:#ffd76a}
.pl div{font-size:11px;color:#8f9aac;margin-top:5px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.pl.on div{color:#ffd76a}
</style></head><body>
<canvas id="bg"></canvas>
<header><b>__NICK__ 的复古CD机</b><span>__TOTAL__ 首 · 备份于 __GENERATED__</span>
 <a href="index.html">📋 目录视图</a></header>
<main>
 <div class="deck">
  <div class="cdwrap" id="cdwrap">
   <div class="cd" id="cd">
     <img class="cover" id="cover" alt="">
     <div class="glare"></div><div class="hole"></div>
   </div>
   <svg class="arm" viewBox="0 0 200 300">
     <circle cx="156" cy="42" r="26" fill="#20242e" stroke="#3a4150" stroke-width="3"/>
     <circle cx="156" cy="42" r="8" fill="#0b0d13"/>
     <rect x="148" y="52" width="16" height="150" rx="8" fill="#2a2f3a" stroke="#3a4150"/>
     <rect x="138" y="196" width="52" height="26" rx="12" fill="#20242e" stroke="#3a4150" transform="rotate(-18 164 209)"/>
   </svg>
  </div>
  <div class="now">
   <div class="t" id="nTitle">—</div>
   <div class="s" id="nSub">选择下方唱片开始播放</div>
   <div class="c" id="nDesc"></div>
  </div>
 </div>
 <div class="side">
  <div class="lyrics" id="lyrics"><p style="color:#7c8496">♪ 点下方唱片，让时间倒回你开口唱的那一刻</p></div>
  <div class="ctrl">
   <span class="time" id="tCur">0:00</span>
   <input type="range" id="seek" min="0" max="1000" value="0">
   <span class="time" id="tEnd">0:00</span>
   <button onclick="prev()" title="上一首">⏮</button>
   <button class="big" id="playBtn" onclick="togglePlay()" title="播放/暂停">▶</button>
   <button onclick="next()" title="下一首">⏭</button>
  </div>
 </div>
</main>
<div class="strip" id="strip"></div>
<audio id="au" preload="none"></audio>
<script>
const SONGS = __DATA__;
const $ = s => document.querySelector(s);
const au = $('#au');
let cur = -1, lrc = [], lrcOn = -1, seeking = false;

/* ---------- 粒子背景 ---------- */
const cv = $('#bg'), cx = cv.getContext('2d');
let W, H, P = [];
function rs(){ W = cv.width = innerWidth; H = cv.height = innerHeight; }
rs(); addEventListener('resize', rs);
for (let i = 0; i < 110; i++) P.push({
  x: Math.random(), y: Math.random(), r: .6 + Math.random() * 2.1,
  vy: .12 + Math.random() * .5, vx: (Math.random() - .5) * .16,
  a: .08 + Math.random() * .3, ph: Math.random() * 6.28, warm: Math.random() < .28
});
(function tick(t){
  cx.clearRect(0, 0, W, H);
  for (const p of P) {
    p.y -= p.vy / H * 3.2; p.x += p.vx / W * 3.2 + Math.sin(t / 2400 + p.ph) * .00045;
    if (p.y < -.04) { p.y = 1.04; p.x = Math.random(); }
    if (p.x < -.04) p.x = 1.04; if (p.x > 1.04) p.x = -.04;
    const tw = .55 + .45 * Math.sin(t / 700 + p.ph);
    cx.beginPath();
    cx.arc(p.x * W, p.y * H, p.r, 0, 6.28);
    cx.fillStyle = p.warm ? 'rgba(255,214,106,' + (p.a * tw) + ')' : 'rgba(190,205,255,' + (p.a * tw * .8) + ')';
    cx.fill();
  }
  requestAnimationFrame(tick);
})(0);

/* ---------- LRC ---------- */
function parseLrc(text){
  const out = [];
  for (const line of text.split(/\r?\n/)) {
    const m = line.match(/\[(\d+):(\d+(?:\.\d+)?)\]/g);
    if (!m) continue;
    const txt = line.replace(/\[[^\]]*\]/g, '').trim();
    for (const tm of m) {
      const mm = tm.match(/\[(\d+):(\d+(?:\.\d+)?)\]/);
      if (mm) out.push({ t: (+mm[1]) * 60 + (+mm[2]), text: txt });
    }
  }
  return out.sort((a, b) => a.t - b.t);
}
function renderLrc(){
  const box = $('#lyrics');
  if (!lrc.length) { box.innerHTML = '<p style="color:#7c8496">这首歌没有找到歌词文件</p>'; return; }
  box.innerHTML = lrc.map((l, i) => `<p data-i="${i}">${l.text || '♪'}</p>`).join('');
}
function syncLrc(t){
  if (!lrc.length) return;
  let i = -1;
  for (let k = 0; k < lrc.length; k++) if (lrc[k].t <= t) i = k;
  if (i === lrcOn) return;
  lrcOn = i;
  const box = $('#lyrics');
  [...box.querySelectorAll('p')].forEach((p, k) => p.classList.toggle('on', k === i));
  const el = box.querySelector('p[data-i="' + i + '"]');
  if (el) el.scrollIntoView({ block: 'center', behavior: 'smooth' });
}

/* ---------- 播放 ---------- */
function fmt(s){ s = Math.max(0, s | 0); return (s / 60 | 0) + ':' + String(s % 60).padStart(2, '0'); }
function load(i, autoplay){
  if (!SONGS.length) return;
  cur = (i + SONGS.length) % SONGS.length;
  const e = SONGS[cur];
  au.src = e.audio;
  $('#cover').src = e.cover || '';
  $('#nTitle').textContent = e.title;
  $('#nSub').textContent = '原唱：' + (e.singer ? e.singer + '《' + e.song + '》' : (e.song || '—'))
    + '　·　' + e.time + (e.score ? '　·　评分 ' + e.score : '');
  $('#nDesc').textContent = e.content || '';
  $('#tEnd').textContent = fmt(au.duration || 0);
  lrc = []; lrcOn = -1; renderLrc();
  if (e.lrc) fetch(e.lrc).then(r => r.ok ? r.text() : '').then(t => {
    if (e.audio === au.getAttribute('src') && t) { lrc = parseLrc(t); renderLrc(); syncLrc(au.currentTime || 0); }
  }).catch(() => {});
  [...document.querySelectorAll('.pl')].forEach((p, k) => p.classList.toggle('on', k === cur));
  if (autoplay) play(); else pauseUi();
  const on = document.querySelector('.pl.on');
  if (on) on.scrollIntoView({ inline: 'center', block: 'nearest', behavior: 'smooth' });
}
function play(){ au.play().catch(() => {}); }
function pauseUi(){ $('#cd').classList.remove('playing'); $('#cdwrap').classList.remove('playing'); $('#playBtn').textContent = '▶'; }
function togglePlay(){
  if (cur < 0) { load(0, true); return; }
  if (au.paused) play(); else au.pause();
}
au.onplay = () => { $('#cd').classList.add('playing'); $('#cdwrap').classList.add('playing'); $('#playBtn').textContent = '⏸'; };
au.onpause = () => pauseUi();
au.onended = () => load(cur + 1, true);
au.ontimeupdate = () => {
  if (!seeking && au.duration) $('#seek').value = au.currentTime / au.duration * 1000;
  $('#tCur').textContent = fmt(au.currentTime);
  $('#tEnd').textContent = fmt(au.duration || 0);
  syncLrc(au.currentTime);
};
au.onloadedmetadata = () => { $('#tEnd').textContent = fmt(au.duration || 0); };
$('#seek').addEventListener('input', () => { seeking = true; });
$('#seek').addEventListener('change', () => {
  if (au.duration) au.currentTime = $('#seek').value / 1000 * au.duration;
  seeking = false;
});
function prev(){ load(cur - 1, true); }
function next(){ load(cur + 1, true); }
addEventListener('keydown', e => {
  if (e.code === 'Space') { e.preventDefault(); togglePlay(); }
  if (e.code === 'ArrowRight' && e.altKey) next();
  if (e.code === 'ArrowLeft' && e.altKey) prev();
});

/* ---------- 初始化 ---------- */
const strip = $('#strip');
SONGS.forEach((e, i) => {
  const d = document.createElement('div');
  d.className = 'pl';
  d.innerHTML = `<img loading="lazy" src="${e.cover || ''}" onerror="this.style.visibility='hidden'"><div>${e.seq}. ${e.title}</div>`;
  d.onclick = () => load(i, true);
  strip.appendChild(d);
});
if (SONGS.length) load(0, false);
</script></body></html>
"""


def write_cd_page(out, entries, profile, generated):
    path = os.path.join(out, 'cd.html')

    def esc(s):
        return html_mod.escape(str(s if s is not None else ''))

    data = []
    for e in sorted(entries, key=lambda x: x['seq']):
        if not e.get('audio'):
            continue
        data.append({
            'seq': e['seq'], 'title': e.get('title') or '未命名',
            'song': e.get('song_name') or '', 'singer': e.get('singer_name') or '',
            'time': (e.get('time') or '')[:10], 'score': e.get('score'),
            'content': e.get('content') or '',
            'audio': e.get('audio') or '', 'cover': e.get('cover') or '',
            'lrc': e.get('lrc') or '',
        })
    data_json = json.dumps(data, ensure_ascii=False).replace('</', '<\\/')
    nick = esc(profile.get('nickname', ''))
    doc = (CD_TEMPLATE
           .replace('__DATA__', data_json)
           .replace('__NICK__', nick)
           .replace('__TOTAL__', str(len(data)))
           .replace('__GENERATED__', esc(generated[:10])))
    with open(path, 'w', encoding='utf-8') as f:
        f.write(doc)
    return path


# ---------------------------------------------------------------- CLI 入口

def cli_main():
    ap = argparse.ArgumentParser(
        description='全民K歌个人作品批量备份：音频/封面/MV/上传时间/介绍/评论/全部详情 + CSV目录 + HTML歌单',
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    ap.add_argument('target', nargs='+', help='个人主页链接或 uid（可给多个，依次备份）')
    ap.add_argument('--out', default='', help='输出目录（默认 kg_backup_昵称）')
    ap.add_argument('--cookie', default='', help='浏览器登录 Cookie 原始字符串（备份私密作品用）')
    ap.add_argument('--cookie-file', default='', help='从文件读取 Cookie（一行原始 Cookie）')
    ap.add_argument('--proxy', default='', help='HTTP 代理，如 http://127.0.0.1:7890')
    ap.add_argument('--workers', type=int, default=3, help='并发下载数')
    ap.add_argument('--page-sleep', type=float, default=0.8, help='翻页间隔秒')
    ap.add_argument('--retries', type=int, default=3, help='失败重试次数')
    ap.add_argument('--timeout', type=int, default=30, help='请求超时秒')
    ap.add_argument('--max', type=int, default=0, help='最多处理多少首（0=全部）')
    ap.add_argument('--since', default='', help='只备份此日期(含)之后 YYYY-MM-DD')
    ap.add_argument('--until', default='', help='只备份此日期(含)之前 YYYY-MM-DD')
    ap.add_argument('--no-audio', action='store_true', help='不下载音频/MV')
    ap.add_argument('--no-cover', action='store_true', help='不下载封面/配图')
    ap.add_argument('--no-comments', action='store_true', help='不导出评论')
    ap.add_argument('--no-meta', action='store_true', help='不保存详情JSON')
    ap.add_argument('--no-html', action='store_true', help='不生成HTML歌单')
    ap.add_argument('--no-tags', action='store_true', help='不往 m4a 嵌标签（默认检测到 ffmpeg 即启用）')
    ap.add_argument('--no-lyric', action='store_true', help='不下载/嵌入歌词（默认：LRC 边车文件 + 写入音频歌词标签）')
    ap.add_argument('--flat', action='store_true', help='文件放一起：音频/封面/歌词/详情 JSON 不分子目录，同前缀成组')
    ap.add_argument('--video', action='store_true', help='有 MV 的作品额外下载 MV 视频')
    ap.add_argument('--mp3', action='store_true', help='额外生成 MP3 副本（192kbps，车机/老设备兼容，m4a 母版保留）')
    ap.add_argument('--zip', action='store_true', help='备份完成后打包为 ZIP（方便传网盘）')
    ap.add_argument('--list-only', action='store_true', help='只生成作品清单CSV，不抓详情不下载')
    ap.add_argument('--force', action='store_true', help='忽略已有索引，全部重新处理')
    args = ap.parse_args()

    if sys.stdout:
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

    for t in args.target:
        backup_one(t, args)
        print()


if __name__ == '__main__':
    cli_main()
