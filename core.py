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
APP_VERSION = '1.12.4'
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


def _icon_png_bytes():
    """内置应用图标（打包后在 _MEIPASS/static，源码运行在 ./static）。"""
    bases = [os.path.join(getattr(sys, '_MEIPASS', '') or '', 'static'),
             os.path.join(os.path.dirname(os.path.abspath(__file__)), 'static')]
    for base in bases:
        p = os.path.join(base, 'icon-192.png')
        if os.path.exists(p):
            with open(p, 'rb') as f:
                return f.read()
    return None


def _write_icon(out):
    b = _icon_png_bytes()
    p = os.path.join(out, 'kgicon.png')
    if b and not os.path.exists(p):
        try:
            with open(p, 'wb') as f:
                f.write(b)
        except OSError:
            return ''
    return 'kgicon.png' if (b and os.path.exists(p)) else ''


PAGE_TEMPLATE = r"""<!DOCTYPE html>
<html lang="zh-cn"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="theme-color" content="#111418">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-title" content="K歌歌单">
<link rel="apple-touch-icon" href="kgicon.png">
<title>__NICK__ 的全民K歌备份</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{background:#111418;color:#eee;min-height:100vh;display:flex;flex-direction:column;
 font-family:"Microsoft YaHei",system-ui,sans-serif;overflow-x:hidden}
canvas#bg{position:fixed;inset:0;z-index:0}
header{position:sticky;top:0;z-index:9;padding:18px 24px 10px;background:rgba(17,20,24,.96);
 backdrop-filter:blur(6px);border-bottom:1px solid #2a2f36}
.hrow{display:flex;align-items:center;gap:14px;flex-wrap:wrap}
h1{margin:0;font-size:22px}
.seg{display:inline-flex;align-items:center;background:#1a1f26;border-radius:999px;padding:4px;gap:4px;margin-left:auto;
 overflow-x:auto;max-width:100%}
.seg button{border:none;background:none;color:#9aa3ad;padding:7px 16px;border-radius:999px;font-size:13px;cursor:pointer;transition:.2s;white-space:nowrap;
 display:inline-flex;align-items:center;justify-content:center;gap:5px;line-height:1;vertical-align:middle}
.seg button.on{background:#ffd76a;color:#111;font-weight:700}
.meta{color:#9aa3ad;font-size:13px;display:flex;flex-wrap:wrap;align-items:center;gap:6px 10px;margin-top:8px}
#q{padding:7px 12px;border-radius:8px;border:1px solid #3a4149;background:#1b2026;color:#eee;width:min(200px,38vw)}
select{padding:7px;border-radius:8px;border:1px solid #3a4149;background:#1b2026;color:#eee}
/* ---------- 目录视图 ---------- */
#classicView{position:relative;z-index:1;flex:1}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:16px;padding:20px}
.card{background:#1a1f26;border-radius:12px;overflow:hidden;display:flex;flex-direction:column}
.card img,.noimg{width:100%;height:190px;object-fit:cover;display:flex;align-items:center;justify-content:center;font-size:48px;color:#555;background:#232932}
.body{padding:12px 14px 14px;display:flex;flex-direction:column;gap:5px}
.title a{color:#fff;font-weight:600;text-decoration:none;font-size:15px}
.title a:hover{color:#7cb8ff}
.sub{color:#aab4bf;font-size:13px}
.time,.stats{color:#8b95a1;font-size:12px}
.content{color:#cfd6dd;font-size:13px;background:#222932;border-left:3px solid #3f6ea5;padding:6px 9px;border-radius:0 6px 6px 0;white-space:pre-wrap}
audio{width:100%;height:34px;margin-top:4px}
.hidden{display:none}
/* ---------- 皮肤通用 ---------- */
.skin-view{display:none;flex:1;position:relative;min-height:0;z-index:1}
body.vvinyl #skinVinyl,body.vtape #skinTape,body.vspectrum #skinSpectrum,body.vjukebox #skinJuke{display:flex}
body:not(.vgrid) #classicView{display:none}
body:not(.vgrid) #strip{display:flex}
#dmask{position:fixed;inset:0;z-index:55;background:rgba(0,0,0,.45);display:none}
body.dopen #dmask{display:block}
#drawer{position:fixed;top:0;right:0;bottom:0;width:min(390px,94vw);z-index:60;
 background:rgba(14,17,26,.92);backdrop-filter:blur(18px);border-left:1px solid #2a3040;
 transform:translateX(105%);transition:transform .34s cubic-bezier(.2,.8,.2,1);
 display:flex;flex-direction:column;box-shadow:-20px 0 60px rgba(0,0,0,.45)}
body.dopen #drawer{transform:translateX(0)}
#drawer .dhead{padding:18px 18px 12px;border-bottom:1px solid #222836}
#drawer .dtitle{display:flex;align-items:center;justify-content:space-between;margin-bottom:12px}
#drawer .dtitle b{font-size:16px;color:#fff}
#drawer .dtitle span{font-size:11px;color:#7c8496;margin-left:8px}
#drawer .dclose{background:none;border:none;color:#8f9aac;font-size:20px;cursor:pointer;line-height:1;padding:4px 8px;border-radius:8px}
#drawer .dclose:hover{color:#fff;background:#232a38}
#dq{width:100%;padding:10px 14px;border-radius:12px;border:1px solid #2e3542;background:#14181f;color:#eee;font-size:13.5px;outline:none;transition:.15s}
#dq:focus{border-color:#ffd76a;box-shadow:0 0 0 3px rgba(255,215,106,.15)}
#dcount{font-size:11px;color:#7c8496;margin:10px 18px 4px}
#dlist{flex:1;overflow-y:auto;padding:6px 12px 18px}
.ditem{display:flex;align-items:center;gap:12px;padding:10px 12px;border-radius:14px;cursor:pointer;transition:background .15s;border-left:3px solid transparent}
.ditem:hover{background:#1a2130}
.ditem.playing{background:rgba(255,215,106,.07);border-left-color:#ffd76a}
.ditem .n{flex:none;width:34px;color:#69737f;font-size:11.5px;font-variant-numeric:tabular-nums}
.ditem.playing .n{color:#ffd76a}
.ditem img{width:50px;height:50px;border-radius:10px;object-fit:cover;flex:none;background:#202634}
.ditem .c{flex:1;min-width:0}
.ditem .t{font-size:14px;font-weight:600;color:#e8eaf0;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.ditem .s{font-size:11.5px;color:#8f9aac;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;margin-top:2px}
.ditem .eq{display:none;gap:2.5px;align-items:flex-end;height:16px;flex:none}
.ditem.playing .eq{display:flex}
.eq i{width:3px;background:#ffd76a;border-radius:1.5px;animation:eqb .9s ease-in-out infinite}
.eq i:nth-child(2){animation-delay:.18s}
.eq i:nth-child(3){animation-delay:.36s}
@keyframes eqb{0%,100%{height:5px}50%{height:16px}}
#fab{display:none;position:fixed;right:18px;bottom:20px;z-index:30;width:52px;height:52px;border-radius:50%;
 background:linear-gradient(180deg,#ffd76a,#e8b64c);color:#221d38;border:none;font-size:20px;cursor:pointer;
 box-shadow:0 10px 30px rgba(0,0,0,.45),0 0 22px rgba(255,215,106,.35);-webkit-appearance:none;appearance:none;
 align-items:center;justify-content:center;line-height:1}
body:not(.vgrid) #fab{display:inline-flex}
#fab:hover{transform:scale(1.06)}
#cdCtrls{display:none}
body:not(.vgrid) #cdCtrls{display:flex}
.lyrics{flex:1;overflow-y:auto;text-align:center;scroll-behavior:smooth;
 -webkit-mask-image:linear-gradient(transparent,#fff 14%,#fff 86%,transparent);
 mask-image:linear-gradient(transparent,#fff 14%,#fff 86%,transparent);
 scrollbar-width:thin;scrollbar-color:rgba(255,215,106,.3) transparent}
/* 细滚动条：平时几乎隐形， hover 才明显 */
.lyrics::-webkit-scrollbar{width:5px}
.lyrics::-webkit-scrollbar-track{background:transparent}
.lyrics::-webkit-scrollbar-thumb{background:rgba(255,215,106,.22);border-radius:5px}
.lyrics::-webkit-scrollbar-thumb:hover{background:rgba(255,215,106,.4)}
.lyrics p{padding:9px 4px;color:#5c6675;font-size:15px;transition:all .25s}
.lyrics p.on{color:#ffd76a;font-size:18px;font-weight:600;text-shadow:0 0 18px rgba(255,215,106,.35)}
.ctrl{display:flex;align-items:center;gap:14px;padding:10px 2px 4px}
.ctrl button{background:none;border:1px solid #333a47;color:#dfe3ec;border-radius:50%;width:44px;height:44px;flex:none;
 font-size:17px;cursor:pointer;transition:.15s;-webkit-appearance:none;appearance:none;display:inline-flex;align-items:center;justify-content:center;padding:0;line-height:1}
.ctrl button:hover{border-color:#ffd76a;color:#ffd76a}
.ctrl .big{width:58px;height:58px;flex:none;font-size:21px;background:#ffd76a;color:#111;border:none;font-weight:700;-webkit-appearance:none;appearance:none;display:inline-flex;align-items:center;justify-content:center;line-height:1}
.ctrl .big:hover{background:#ffe28a;border:none;color:#111}
.time{color:#8f9aac;font-size:12px;font-variant-numeric:tabular-nums}
#seek,#seek2{flex:1;min-width:0;-webkit-appearance:none;appearance:none;height:4px;border-radius:4px;background:#262c38;outline:none}
#seek::-webkit-slider-thumb,#seek2::-webkit-slider-thumb{appearance:none;-webkit-appearance:none;width:13px;height:13px;border-radius:50%;background:#ffd76a;cursor:pointer}
/* ---------- 黑胶（精修） ---------- */
#skinVinyl{flex-direction:column;background:radial-gradient(1100px 700px at 72% -12%,#1d2233,#0b0d13 62%)}
.vmain{flex:1;display:flex;gap:48px;padding:16px 40px 14px;min-height:0;width:100%;max-width:1200px;
 margin:0 auto;align-items:center;justify-content:center}
.deck{flex:0 0 400px;display:flex;flex-direction:column;align-items:center;justify-content:center;position:relative}
.deck .ctrl{width:100%;max-width:370px;margin-top:16px;justify-content:center}
.cdwrap{position:relative;width:330px;height:330px;margin-top:6px}
.cd{position:absolute;inset:0;border-radius:50%;background:radial-gradient(circle,#181b22 62%,#20242e 63%,#0e1015 100%);
 box-shadow:0 18px 60px rgba(0,0,0,.65),0 0 0 1px rgba(255,255,255,.06) inset;overflow:hidden}
.cd .cover{position:absolute;inset:0;width:100%;height:100%;border-radius:50%;object-fit:cover;
 animation:spin 14s linear infinite;animation-play-state:paused;will-change:transform}
.cd .grooves{position:absolute;inset:0;border-radius:50%;z-index:2;pointer-events:none;
 background:repeating-radial-gradient(circle at 50% 50%,rgba(255,255,255,.03) 0 2px,rgba(0,0,0,.06) 2px 5px);
 mix-blend-mode:overlay;animation:spin 14s linear infinite;animation-play-state:paused}
.cd .glare{position:absolute;inset:0;border-radius:50%;z-index:3;
 background:conic-gradient(from 210deg,rgba(255,255,255,.14),rgba(255,255,255,.02) 40deg,rgba(255,255,255,.16) 80deg,rgba(255,255,255,.03) 130deg,rgba(255,255,255,.15) 190deg,rgba(255,255,255,.02) 260deg,rgba(255,255,255,.14) 320deg);
 mix-blend-mode:screen;animation:spin 14s linear infinite;animation-play-state:paused}
.cd .hole{position:absolute;left:50%;top:50%;width:64px;height:64px;margin:-32px;
 border-radius:50%;background:#0b0d13;border:1px solid rgba(255,255,255,.15);z-index:4}
.cd .hole::after{content:'';position:absolute;inset:18px;border-radius:50%;background:#171b24}
.cd.playing .cover,.cd.playing .grooves,.cd.playing .glare{animation-play-state:running}
@keyframes spin{to{transform:rotate(360deg)}}
.arm{position:absolute;right:-26px;top:-34px;width:200px;height:300px;z-index:4;pointer-events:none;
 transform:rotate(-24deg);transform-origin:78% 14%;transition:transform .7s cubic-bezier(.2,.8,.2,1)}
.cdwrap.playing .arm{transform:rotate(6deg)}
.now{margin-top:18px;text-align:center;max-width:360px}
.now .t{font-size:20px;font-weight:700;color:#fff}
.now .s{color:#8f9aac;font-size:13px;margin-top:5px}
.now .c{color:#6b7484;font-size:12px;margin-top:9px;line-height:1.6;max-height:52px;overflow:hidden}
.vside{flex:1;display:flex;flex-direction:column;min-width:0;min-height:0;max-width:560px;padding:10px 0}
/* 歌词区：定高 + 内部滚动。不能依赖 flex-basis:0 —— 在内容自适应高度的父容器里会直接塌缩成 0（v1.11.8 歌词消失的根因） */
.vside .lyrics{flex:none;height:min(54vh,470px);min-height:220px;position:relative}
@media (max-width:880px){.vside .lyrics{height:38vh;min-height:170px}}
/* ---------- 卡座 ---------- */
#skinTape{flex-direction:column;align-items:center;justify-content:center;padding:20px;
 background:linear-gradient(180deg,#20242e,#12151c 70%,#0b0d13)}
.tape-body{width:min(680px,94vw);border-radius:22px;padding:22px 26px 26px;
 background:linear-gradient(180deg,#e7dcc3,#d6c6a2 60%,#c4b28c);
 box-shadow:0 24px 60px rgba(0,0,0,.55),0 2px 0 rgba(255,255,255,.35) inset,0 -6px 18px rgba(0,0,0,.2) inset;
 color:#4a3f2e}
.tape-brand{display:flex;justify-content:space-between;align-items:center;font-size:11px;letter-spacing:3px;
 color:#7a6c50;font-weight:700;margin-bottom:12px}
.tape-window{position:relative;height:190px;border-radius:14px;
 background:linear-gradient(180deg,#2c2620,#1a1712);box-shadow:0 4px 14px rgba(0,0,0,.4) inset;
 display:flex;align-items:center;justify-content:center;gap:44px;padding:14px}
.reel{position:relative;width:130px;height:130px;border-radius:50%;
 background:radial-gradient(circle,#3a3226 30%,#241f18 31%,#171310 100%);
 box-shadow:0 0 0 4px rgba(0,0,0,.35),0 0 22px rgba(0,0,0,.5) inset}
.reel img{position:absolute;inset:14px;width:calc(100% - 28px);height:calc(100% - 28px);border-radius:50%;object-fit:cover;opacity:.9;
 animation:spin 8s linear infinite;animation-play-state:paused}
.reel .spokes{position:absolute;inset:0;border-radius:50%;
 background:repeating-conic-gradient(rgba(255,255,255,.12) 0 14deg,transparent 14deg 45deg);
 animation:spin 8s linear infinite;animation-play-state:paused}
.reel .hub{position:absolute;left:50%;top:50%;width:34px;height:34px;margin:-17px;border-radius:50%;
 background:#d6c6a2;box-shadow:0 2px 6px rgba(0,0,0,.5);z-index:3}
.reel .hub::after{content:'';position:absolute;inset:8px;border-radius:50%;background:#8a7a58}
.tape-window.playing .reel img,.tape-window.playing .reel .spokes{animation-play-state:running}
.tape-link{position:absolute;left:50%;top:50%;transform:translate(-50%,-50%);width:120px;height:10px;border-radius:6px;
 background:linear-gradient(90deg,#1a1208,#3d2c14 40%,#1a1208);box-shadow:0 1px 3px rgba(0,0,0,.6)}
.tape-meters{display:flex;align-items:center;justify-content:space-between;margin:16px 6px 6px}
.vu{display:flex;align-items:center;gap:10px}
.vu-gauge{position:relative;width:110px;height:56px;border-radius:10px 10px 4px 4px;overflow:hidden;
 background:linear-gradient(180deg,#14110c,#241e14);box-shadow:0 2px 8px rgba(0,0,0,.4) inset}
.vu-gauge::after{content:'';position:absolute;left:8%;right:8%;bottom:0;height:2px;background:#6b5c40}
.vu-needle{position:absolute;left:50%;bottom:4px;width:3px;height:44px;border-radius:2px;
 background:linear-gradient(#ff6b5e,#ffb020);transform-origin:50% 100%;transform:rotate(-38deg);
 transition:transform .12s ease-out;box-shadow:0 0 6px rgba(255,176,32,.5)}
.vu-lab{font-size:11px;color:#7a6c50;font-weight:700}
.led{padding:8px 18px;border-radius:8px;background:#16120c;border:1px solid #3a3122;
 font-family:Consolas,Menlo,monospace;font-size:26px;letter-spacing:4px;color:#ffb02e;
 text-shadow:0 0 12px rgba(255,176,32,.65);box-shadow:0 2px 10px rgba(0,0,0,.6) inset}
.tape-keys{display:flex;align-items:center;justify-content:center;gap:14px;margin-top:16px}
.tkey{min-width:86px;padding:12px 16px;border:none;border-radius:8px;cursor:pointer;
 background:linear-gradient(180deg,#f3ead6,#d9c9a8);color:#4a3f2e;font-weight:700;font-size:13px;letter-spacing:2px;
 box-shadow:0 3px 0 #a8987288,0 6px 14px rgba(0,0,0,.3);-webkit-appearance:none;appearance:none;transition:.1s}
.tkey:active{transform:translateY(3px);box-shadow:0 0 0 #a8987288,0 2px 8px rgba(0,0,0,.3)}
.tkey.on{background:linear-gradient(180deg,#ffd76a,#e8b64c);color:#3a2f14}
.tape-title{text-align:center;margin-top:14px;font-size:15px;font-weight:700;color:#4a3f2e;
 white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.tape-sub{text-align:center;font-size:12px;color:#8a7a58;margin-top:4px}
/* ---------- 频谱 ---------- */
#skinSpectrum{justify-content:center;align-items:center;overflow-y:auto;overflow-x:hidden;padding-bottom:12px}
.spec-bg{position:absolute;inset:-40px;width:calc(100% + 80px);height:calc(100% + 80px);object-fit:cover;
 filter:blur(60px) brightness(.42) saturate(1.3);z-index:0}
.spec-stage{position:relative;z-index:1;width:min(680px,94vw);text-align:center;display:flex;flex-direction:column;align-items:center;gap:14px;padding:20px 0 8px}
.spec-cover{width:min(300px,60vw);height:min(300px,60vw);border-radius:20px;object-fit:cover;
 box-shadow:0 30px 90px rgba(0,0,0,.6);animation:breathe 4.5s ease-in-out infinite;animation-play-state:paused}
body.vspectrum.playing .spec-cover{animation-play-state:running}
@keyframes breathe{50%{transform:scale(1.035)}}
.spec-title{font-size:24px;font-weight:700;color:#fff;text-shadow:0 2px 18px rgba(0,0,0,.5)}
.spec-sub{color:#b9c2d4;font-size:13px}
.spec-line{min-height:30px;color:#ffd76a;font-size:16px;font-weight:600;text-shadow:0 2px 14px rgba(0,0,0,.55)}
#specCanvas{width:min(640px,92vw);height:90px}
.sprog{width:100%;display:flex;align-items:center;gap:12px}
.sp-track{flex:1;height:6px;border-radius:4px;background:rgba(255,255,255,.16);position:relative;cursor:pointer;transition:height .15s}
.sp-track:hover{height:9px}
.sp-fill{height:100%;width:0%;border-radius:4px;background:linear-gradient(90deg,#ffd76a,#ff9d5c);box-shadow:0 0 12px rgba(255,215,106,.35);position:relative}
.sp-fill::after{content:'';position:absolute;right:-6px;top:50%;transform:translateY(-50%);width:13px;height:13px;border-radius:50%;
 background:#ffd76a;box-shadow:0 0 12px rgba(255,215,106,.7)}
/* ---------- 点唱机 ---------- */
#skinJuke{flex-direction:column;justify-content:center;align-items:center;overflow-y:auto;overflow-x:hidden;padding:26px 12px;
 background:radial-gradient(900px 600px at 50% 0%,#241426,#120a14 55%,#0a0710)}
.juke{width:min(430px,94vw);margin:0;flex:none}
.jk-cab{position:relative;border-radius:210px 210px 20px 20px / 150px 150px 20px 20px;
 background:linear-gradient(180deg,#5c2032,#3c1424 42%,#2a0d1a 80%,#200a14);
 box-shadow:0 0 0 6px #170911,0 0 0 8px #c9a44a,0 0 44px rgba(255,79,163,.22),0 34px 90px rgba(0,0,0,.75);
 padding:30px 30px 22px;overflow:hidden}
.jk-arch{position:absolute;inset:0 0 auto 0;height:170px;overflow:hidden;pointer-events:none}
.arc{position:absolute;left:50%;transform:translateX(-50%);border-radius:50%;border-style:solid;border-color:transparent}
.arc.a1{width:600px;height:600px;top:-430px;border-width:5px;border-top-color:#ff4fa3;
 box-shadow:0 0 20px rgba(255,79,163,.55);animation:huerot 8s linear infinite}
.arc.a2{width:680px;height:680px;top:-478px;border-width:3px;border-top-color:#4fe3ff;
 box-shadow:0 0 14px rgba(79,227,255,.45);animation:huerot 8s linear infinite reverse}
.arc.a3{width:640px;height:640px;top:-452px;border-width:2px;border-top-color:#ffd76a;
 box-shadow:0 0 12px rgba(255,215,106,.4)}
@keyframes huerot{to{filter:hue-rotate(360deg)}}
.jk-logo{position:relative;text-align:center;font-size:12px;letter-spacing:7px;color:#ffd76a;font-weight:700;
 text-shadow:0 0 14px rgba(255,215,106,.65);margin:6px 0 16px}
.jk-window{position:relative;border-radius:16px;overflow:hidden;padding:20px;text-align:center;
 background:radial-gradient(circle at 50% 28%,#2c1520,#170a12 72%);
 box-shadow:0 8px 26px rgba(0,0,0,.65) inset,0 0 0 4px #c9a44a,0 0 0 7px #6b4e1e}
.jk-disc{position:relative;width:212px;height:212px;margin:0 auto;border-radius:50%;overflow:hidden;
 background:radial-gradient(circle,#141414 30%,#0d0d0d 31%,#090909 62%,#060606 63%);
 box-shadow:0 0 0 3px rgba(255,255,255,.1),0 10px 34px rgba(0,0,0,.6);
 animation:jspin 5.5s linear infinite;animation-play-state:paused}
body.playing .jk-disc{animation-play-state:running}
.jk-disc img{position:absolute;left:50%;top:50%;width:86px;height:86px;margin:-43px;border-radius:50%;object-fit:cover;
 box-shadow:0 0 0 3px rgba(255,255,255,.18),0 0 18px rgba(0,0,0,.5);z-index:2}
.jk-disc::after{content:'';position:absolute;inset:0;border-radius:50%;
 background:repeating-radial-gradient(circle at 50% 50%,rgba(255,255,255,.055) 0 2px,transparent 2px 4.5px)}
@keyframes jspin{to{transform:rotate(360deg)}}
.jk-strips{margin:16px auto 4px;width:88%}
.jk-strip{background:linear-gradient(180deg,#ffe9a8,#e2bd62);color:#3a2410;font-weight:800;font-size:15px;
 border-radius:7px 7px 0 0;padding:8px 12px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;
 box-shadow:0 2px 0 #9a7a32}
.jk-strip2{background:linear-gradient(180deg,#fdf3cf,#e2c274);color:#6b5220;font-size:12px;
 border-radius:0 0 7px 7px;padding:5px 12px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;
 box-shadow:0 3px 0 #9a7a32}
.jk-btns{display:grid;grid-template-columns:repeat(6,1fr);gap:9px;margin:18px auto 4px;width:88%}
.jkb{padding:10px 0;border-radius:8px;border:1px solid #8a6a2e;cursor:pointer;
 background:linear-gradient(180deg,#fdf3cf,#dfbd6c);color:#3a2410;font-weight:800;font-size:12px;
 box-shadow:0 2px 0 #9a7a32;transition:.1s;font-family:inherit;-webkit-appearance:none;appearance:none}
.jkb:active{transform:translateY(2px);box-shadow:none}
.jk-grille{height:36px;border-radius:9px;margin:14px auto 2px;width:88%;
 background:repeating-linear-gradient(90deg,#160a10 0 7px,#421826 7px 10px);
 box-shadow:0 3px 10px rgba(0,0,0,.45) inset}
.jk-panel{display:flex;align-items:center;justify-content:center;gap:18px;margin-top:18px}
.jk-ctrl{background:none;border:1px solid #8a5c72;color:#e8c9d8;border-radius:50%;width:46px;height:46px;flex:none;
 font-size:16px;cursor:pointer;-webkit-appearance:none;appearance:none;display:inline-flex;align-items:center;justify-content:center;
 padding:0;line-height:1;transition:.15s}
.jk-ctrl:hover{border-color:#ffd76a;color:#ffd76a}
.jk-play{width:60px;height:60px;flex:none;border:none;border-radius:50%;cursor:pointer;font-size:20px;font-weight:700;
 background:linear-gradient(180deg,#ffd76a,#e8b64c);color:#3a2410;-webkit-appearance:none;appearance:none;
 display:inline-flex;align-items:center;justify-content:center;padding:0;line-height:1;
 box-shadow:0 0 26px rgba(255,215,106,.5),0 5px 0 #9a7a32}
.jk-play:active{transform:translateY(4px);box-shadow:0 0 26px rgba(255,215,106,.5)}
.jk-lights{display:flex;gap:11px;justify-content:center;margin-top:16px}
.jk-lights i{width:9px;height:9px;border-radius:50%;background:#ff4fa3;box-shadow:0 0 12px #ff4fa3;animation:blink 1.6s infinite}
.jk-lights i:nth-child(2){background:#ffd76a;box-shadow:0 0 12px #ffd76a;animation-delay:.2s}
.jk-lights i:nth-child(3){background:#4fe3ff;box-shadow:0 0 12px #4fe3ff;animation-delay:.4s}
.jk-lights i:nth-child(4){background:#7cffb2;box-shadow:0 0 12px #7cffb2;animation-delay:.6s}
.jk-lights i:nth-child(5){background:#c9a44a;box-shadow:0 0 12px #c9a44a;animation-delay:.8s}
body.playing .jk-lights i{animation-duration:.65s}
/* 播放时机身呼吸光晕（点唱机动效） */
body.playing .jk-cab{animation:jukeGlow 2.6s ease-in-out infinite}
@keyframes jukeGlow{50%{box-shadow:0 0 0 6px #170911,0 0 0 8px #c9a44a,0 0 70px rgba(255,79,163,.45),0 34px 90px rgba(0,0,0,.75)}}
@keyframes blink{50%{opacity:.22}}
/* ---------- 视图状态 ---------- */
body.vvinyl #classicView,body.vtape #classicView,body.vspectrum #classicView,body.vjukebox #classicView{display:none}
body.vgrid #classicView{display:block}
body.vvinyl #skinVinyl,body.vtape #skinTape,body.vspectrum #skinSpectrum,body.vjukebox #skinJuke{display:flex}
body:not(.vgrid) #gridCtrls{display:none}
#cdCtrls{display:none}
body.vvinyl #cdCtrls,body.vtape #cdCtrls,body.vspectrum #cdCtrls,body.vjukebox #cdCtrls{display:flex}
body.vvinyl,body.vtape,body.vspectrum,body.vjukebox{overflow:hidden;height:100vh;height:100dvh}
@media (max-width:640px){
  header{padding:14px 14px 8px}
  header .hrow{gap:8px}
  h1{font-size:18px}
  .seg button{padding:6px 12px;font-size:12px}
  .grid{grid-template-columns:repeat(auto-fill,minmax(260px,1fr));gap:12px;padding:14px}
}
@media (max-width:880px){
  .vmain{flex-direction:column;align-items:center;gap:10px;padding:4px 16px 0}
  .deck{flex:none}
  .cdwrap{width:240px;height:240px;margin-top:2px}
  .arm{width:146px;height:220px;right:-10px;top:-16px}
  .now{margin-top:14px}
  .now .t{font-size:17px}
  .vside{width:100%;padding:10px 16px 0}
  .lyrics{min-height:210px;padding:10px 2px}
  .lyrics p{font-size:14px;padding:7px 3px}
  .lyrics p.on{font-size:16px}
  .ctrl{gap:8px}
  .ctrl .big{width:50px;height:50px}
  body.vvinyl,body.vtape,body.vspectrum,body.vjukebox{overflow-y:auto;overflow-x:hidden;height:auto}
  #cdView{min-height:100dvh}
  .tape-body{padding:16px 14px 20px}
  .tape-window{gap:22px;height:150px}
  .reel{width:104px;height:104px}
  .tape-link{width:90px}
  .led{font-size:20px;padding:6px 12px}
  .spec-cover{width:min(220px,56vw);height:min(220px,56vw)}
}
/* ---------- 手机端统一收口（各播放器都要不歪、不挤） ---------- */
@media (max-width:700px){
  header{padding:12px 12px 10px;border-bottom:none;box-shadow:0 12px 26px -20px #000}
  .hrow{flex-direction:column;align-items:stretch;gap:9px}
  h1{font-size:17.5px;text-align:center;line-height:1.32}
  /* 模式切换条：整行居中、可横滑、不再被顶部横线压住 */
  .seg{margin:0 auto;width:fit-content;max-width:100%;justify-content:center;padding:3px;gap:3px;
   overflow-x:auto;overflow-y:hidden;scrollbar-width:none;overscroll-behavior-x:contain;-webkit-overflow-scrolling:touch}
  .seg::-webkit-scrollbar{display:none}
  .seg button{padding:6px 10px;font-size:12px;flex:none}
  .meta{font-size:12px;justify-content:center;text-align:center}
  #cdCtrls{display:none!important}   /* 手机不显示键盘快捷键说明 */
  .ctrl{flex-wrap:wrap;justify-content:center}
  .ctrl .jsSeek{min-width:96px;order:-1;flex:1 1 100%}
  /* 目录卡片两边留出呼吸感，别顶到屏幕边 */
  .grid{padding:20px 22px;gap:14px}
  .card{max-width:460px;margin:0 auto;width:100%}
  /* 黑胶：唱片往下沉一点，别顶着头部 */
  .vmain{padding:28px 14px 0}
  .cdwrap{margin-top:6px}
}
/* 卡座表头（L 表+计时器+R 表）在窄屏也要一行放下 */
@media (max-width:600px){
  .tape-meters{gap:10px}
  .vu{flex:1;min-width:0}
  .vu-gauge{width:100%;max-width:130px;height:48px}
  .led{flex:none;font-size:18px;padding:8px 12px;letter-spacing:2px}
}
/* 点唱机：手机上整体收小一号，绝不撑满全屏 */
@media (max-width:560px){
  #skinJuke{padding:12px 8px calc(12px + env(safe-area-inset-bottom))}
  .juke{width:min(340px,86vw)}
  .jk-cab{padding:18px 14px 14px;border-radius:120px 120px 14px 14px / 88px 88px 14px 14px}
  .jk-arch{height:96px}
  .jk-logo{font-size:10px;letter-spacing:4px;margin:2px 0 10px}
  .jk-window{padding:11px;border-radius:12px}
  .jk-disc{width:min(136px,36vw);height:min(136px,36vw)}
  .jk-disc img{width:58px;height:58px;margin:-29px}
  .jk-strips{margin:12px auto 2px;width:94%}
  .jk-btns{grid-template-columns:repeat(3,1fr);gap:6px;width:94%;margin-top:12px}
  .jkb{padding:7px 0;font-size:11px;border-radius:7px}
  .jk-strip{font-size:12px;padding:6px 9px}
  .jk-strip2{font-size:10.5px;padding:4px 9px}
  .jk-grille{height:22px;margin-top:9px}
  .jk-panel{gap:10px;margin-top:12px}
  .jk-ctrl{width:40px;height:40px;font-size:13px}
  .jk-play{width:52px;height:52px;font-size:17px}
  .jk-lights{margin-top:10px;gap:7px}
  .jk-lights i{width:7px;height:7px}
}
@media (max-width:420px){
  .seg button{padding:5px 8px;font-size:11px}
  .seg{gap:2px}
  h1{font-size:16px}
  /* 卡座：L 表 + 计时器 + R 表 一行完整放下，不再被裁掉 */
  .tape-body{padding:14px 12px 16px;width:min(680px,96vw)}
  .tape-window{gap:14px;height:118px}
  .reel{width:80px;height:80px}
  .tape-link{width:66px}
  .tape-meters{gap:8px;margin:12px 2px 4px}
  .vu{flex:1;min-width:0}
  .vu-gauge{width:100%;max-width:120px;height:44px}
  .led{flex:none;font-size:16px;padding:8px 10px;letter-spacing:2px}
  .tkey{min-width:0;flex:1;padding:10px 6px;font-size:12px;letter-spacing:1px}
  .tape-keys{gap:8px}
  .lyrics{min-height:170px}
  .jk-cab{padding:14px 12px 12px}
  .grid{padding:18px 16px}
}

</style></head><body data-view="grid">
<canvas id="bg"></canvas>
<header>
 <div class="hrow">
  <h1>__NICK__ 的全民K歌备份</h1>
  <div class="seg">
   <button id="segGrid" class="on" onclick="setView('grid')">📋 目录</button>
   <button id="segVinyl" onclick="setView('vinyl')">🎧 黑胶</button>
   <button id="segTape" onclick="setView('tape')">📼 卡座</button>
   <button id="segSpec" onclick="setView('spectrum')">🌈 频谱</button>
   <button id="segJuke" onclick="setView('jukebox')">🎙 点唱机</button>
  </div>
 </div>
 <div class="meta">__STAT__ · 总播放 __PLAYS__</div>
 <div class="meta" id="gridCtrls">备份时间 __GENERATED__ · 本地文件仅供个人收藏 ·
   <input id="q" placeholder="搜索歌名/介绍…" oninput="flt()">
   <select onchange="flt()"><option value="new">最新在前</option><option value="old">最早在前</option><option value="hot">播放最多</option><option value="score">评分最高</option></select>
 </div>
 <div class="meta" id="cdCtrls">空格键 播放/暂停 · Alt+←/→ 切歌 · 换肤不停播 · 切回目录音乐不断</div>
</header>
<div id="classicView">
 <div class="grid" id="g"></div>
</div>

<div class="skin-view" id="skinVinyl">
 <div class="vmain">
  <div class="deck">
   <div class="cdwrap" id="cdwrap">
    <div class="cd" id="cd">
      <img class="cover jsCover" alt="">
      <div class="grooves"></div>
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
    <div class="t jsTitle">—</div>
    <div class="s jsSub">切到本视图，点 ▶ 开始播放</div>
    <div class="c jsDesc"></div>
   </div>
   <div class="ctrl">
    <span class="time jsCur">0:00</span>
    <input type="range" class="jsSeek" min="0" max="1000" value="0">
    <span class="time jsEnd">0:00</span>
    <button class="jsPrev" onclick="prev()" title="上一首">⏮</button>
    <button class="big jsPlay" onclick="togglePlay()" title="播放/暂停">▶</button>
    <button class="jsNext" onclick="next()" title="下一首">⏭</button>
   </div>
  </div>
  <div class="vside">
   <div class="lyrics" id="lyVinyl"><p style="color:#7c8496">♪ 选择一首歌开始播放</p></div>
   <div class="offset-ctl" id="offsetCtl" style="display:none">歌词偏移: <button onclick="adjOffset(-0.5)">-0.5s</button><span id="offsetVal">0.0s</span><button onclick="adjOffset(0.5)">+0.5s</button></div>
  </div>
 </div>
</div>

<div class="skin-view" id="skinTape">
 <div class="tape-body">
  <div class="tape-brand"><span>K-SONG STEREO</span><span>TAPE ▸ __TOTAL__</span></div>
  <div class="tape-window" id="tapeWin">
    <div class="reel"><img class="jsCover" alt=""><div class="spokes"></div><div class="hub"></div></div>
    <div class="tape-link"></div>
    <div class="reel"><div class="spokes"></div><div class="hub"></div></div>
  </div>
  <div class="tape-meters">
    <div class="vu"><div class="vu-lab">L</div><div class="vu-gauge"><div class="vu-needle" id="vuL"></div></div></div>
    <div class="led jsLED" id="ledTime">00:00</div>
    <div class="vu"><div class="vu-lab">R</div><div class="vu-gauge"><div class="vu-needle" id="vuR"></div></div></div>
  </div>
  <div class="tape-keys">
    <button class="tkey jsPrev" onclick="prev()">⏮ PREV</button>
    <button class="tkey big jsPlay" onclick="togglePlay()">▶ PLAY</button>
    <button class="tkey jsNext" onclick="next()">⏭ NEXT</button>
  </div>
  <div class="tape-title jsTitle">—</div>
  <div class="tape-sub jsSub"></div>
 </div>
</div>

<div class="skin-view" id="skinSpectrum">
 <img class="spec-bg jsCover" alt="">
 <div class="spec-stage">
  <img class="spec-cover jsCover" alt="">
  <div class="spec-title jsTitle">—</div>
  <div class="spec-sub jsSub"></div>
  <div class="spec-line" id="specLyric">♪</div>
  <canvas id="specCanvas"></canvas>
  <div class="sprog">
   <span class="time jsCur">0:00</span>
   <div class="sp-track" id="spTrack" title="点击跳转"><div class="sp-fill" id="spFill"></div></div>
   <span class="time jsEnd">0:00</span>
  </div>
  <div class="ctrl">
   <button class="jsPrev" onclick="prev()">⏮</button>
   <button class="big jsPlay" onclick="togglePlay()">▶</button>
   <button class="jsNext" onclick="next()">⏭</button>
  </div>
 </div>
</div>

<div class="skin-view" id="skinJuke">
 <div class="juke">
  <div class="jk-cab">
   <div class="jk-arch"><div class="arc a1"></div><div class="arc a2"></div><div class="arc a3"></div></div>
   <div class="jk-logo">✦ K·SONG ✦</div>
   <div class="jk-window">
    <div class="jk-disc"><img class="jsCover" alt=""></div>
   </div>
   <div class="jk-strips">
    <div class="jk-strip jsTitle">—</div>
    <div class="jk-strip2 jsSub"></div>
   </div>
   <div class="jk-btns" id="jkBtns"></div>
   <div class="jk-grille"></div>
  </div>
  <div class="jk-panel">
   <button class="jk-ctrl jsPrev" onclick="prev()" title="上一首">⏮</button>
   <button class="jk-play jsPlay" onclick="togglePlay()" title="播放/暂停">▶</button>
   <button class="jk-ctrl jsNext" onclick="next()" title="下一首">⏭</button>
   <button class="jk-ctrl" onclick="toggleDrawer()" title="歌单">☰</button>
  </div>
  <div class="jk-lights"><i></i><i></i><i></i><i></i><i></i></div>
 </div>
</div>
<div id="dmask" onclick="closeDrawer()"></div>
<aside id="drawer">
 <div class="dhead">
  <div class="dtitle"><b>歌单</b><span id="dcount"></span><button class="dclose" onclick="closeDrawer()" title="关闭">✕</button></div>
  <input id="dq" oninput="fltDrawer()" placeholder="搜索歌名 / 原唱…">
 </div>
 <div id="dlist"></div>
</aside>
<button id="fab" onclick="toggleDrawer()" title="歌单">☰</button>

<audio id="au" preload="none"></audio>
<script>const SONGS = __DATA__;const CARDS = __CARDS__;</script>
<script>
/* ============ 目录视图（增量渲染，千首不卡） ============ */
const BATCH = 60;
let view = [], shown = 0;
function cardNode(c){ const d = document.createElement('div'); d.innerHTML = c.h; return d.firstElementChild; }
function renderMore(){
  const g = document.getElementById('g');
  const sent = document.getElementById('gSentinel');
  const frag = document.createDocumentFragment();
  view.slice(shown, shown + BATCH).forEach(c => frag.appendChild(cardNode(c)));
  if (sent) g.insertBefore(frag, sent); else g.appendChild(frag);
  shown = Math.min(view.length, shown + BATCH);
}
function resetGrid(){
  const g = document.getElementById('g');
  g.innerHTML = '';
  shown = 0;
  const s = document.createElement('div');
  s.id = 'gSentinel';
  g.appendChild(s);
  gridIO.observe(s);
  renderMore();
}
const gridIO = new IntersectionObserver(es => {
  if (es[0].isIntersecting && shown < view.length) renderMore();
}, { rootMargin: '800px' });
function flt(){
  const q = document.getElementById('q').value.toLowerCase();
  const m = document.querySelector('select').value;
  view = CARDS.filter(c => !q || c.text.includes(q));
  if (m === 'old') view.sort((a,b) => a.seq - b.seq);
  else if (m === 'hot') view.sort((a,b) => (b.play||0) - (a.play||0));
  else if (m === 'score') view.sort((a,b) => (b.score||0) - (a.score||0));
  else view.sort((a,b) => b.seq - a.seq);
  resetGrid();
}
/* ============ 播放内核 ============ */
const au = document.getElementById('au');
let cur = -1, lrc = [], lrcOn = -1, seeking = false, cdBuilt = false;
/* 歌词偏移（秒）：本机记忆，正数让歌词提前出现 */
let lyricOffset = 0;
try { lyricOffset = parseFloat(localStorage.getItem('kgoffset') || '0') || 0; } catch(e){}
function adjOffset(d){
  lyricOffset = Math.round((lyricOffset + d) * 10) / 10;
  if (lyricOffset > 10) lyricOffset = 10;
  if (lyricOffset < -10) lyricOffset = -10;
  try { localStorage.setItem('kgoffset', String(lyricOffset)); } catch(e){}
  const v = document.getElementById('offsetVal');
  if (v) v.textContent = (lyricOffset > 0 ? '+' : '') + lyricOffset.toFixed(1) + 's';
  lrcOn = -2;
  syncLrc(au.currentTime || 0);
}
const $ = s => document.querySelector(s);
const $$ = s => [...document.querySelectorAll(s)];
function esc(s){return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')}
/* Web Audio 分析（频谱/电平表/霓虹律动共用） */
let actx = null, analyser = null, freq = null, fakeT = 0;
const fakeArr = new Uint8Array(64);
function ensureAudioGraph(){
  if (actx) { if (actx.state === 'suspended') actx.resume(); return true; }
  try {
    actx = new (window.AudioContext || window.webkitAudioContext)();
    const srcN = actx.createMediaElementSource(au);
    analyser = actx.createAnalyser();
    analyser.fftSize = 256;
    srcN.connect(analyser);
    analyser.connect(actx.destination);
    freq = new Uint8Array(analyser.frequencyBinCount);
    return true;
  } catch (e) { actx = null; analyser = null; return false; }
}
function levels(){
  if (analyser) { analyser.getByteFrequencyData(freq); return freq; }
  fakeT += .09;
  for (let i = 0; i < fakeArr.length; i++)
    fakeArr[i] = 34 + 66 * Math.abs(Math.sin(fakeT * (1 + i * .05) + i * .28));
  return fakeArr;
}
/* 粒子背景 */
const cv = document.getElementById('bg'), cx = cv.getContext('2d');
let W, H, P = [];
let specCv = null, scx = null;
function rs(){ W = cv.width = innerWidth; H = cv.height = innerHeight; specFit(); }
rs(); addEventListener('resize', () => { rs(); });
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
/* LRC */
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
  const boxes = ['#lyVinyl'];
  for (const sel of boxes) {
    const box = document.querySelector(sel);
    if (!box) continue;
    box.innerHTML = lrc.length ? lrc.map((l, i) => `<p data-i="${i}">${l.text || '♪'}</p>`).join('')
                               : '<p style="color:#7c8496">这首歌没有找到歌词文件</p>';
  }
  lrcOn = -1;
}
function syncLrc(t){
  if (!lrc.length) return;
  const tt = t + lyricOffset;
  let i = -1;
  for (let k = 0; k < lrc.length; k++) if (lrc[k].t <= tt) i = k;
  if (i === lrcOn) return;
  lrcOn = i;
  const box = $('#lyVinyl');
  [...box.querySelectorAll('p')].forEach((p, k) => p.classList.toggle('on', k === i));
  const el = box.querySelector('p[data-i="' + i + '"]');
  // 只在歌词框内部滚动。scrollIntoView 会连带滚动整个页面（黑胶界面被顶上去的元凶之一）
  if (el) box.scrollTo({ top: el.offsetTop - box.clientHeight / 2 + el.clientHeight / 2, behavior: 'smooth' });
  const line = lrc[i] ? (lrc[i].text || '♪') : '';
  const sp = document.getElementById('specLyric');
  if (sp && document.body.dataset.view === 'spectrum') sp.textContent = line || '♪';
}
function fmt(s){ s = Math.max(0, s | 0); return (s / 60 | 0) + ':' + String(s % 60).padStart(2, '0'); }
function fmtL(s){ s = Math.max(0, s | 0); return String(s / 60 | 0).padStart(2, '0') + ':' + String(s % 60).padStart(2, '0'); }
function load(i, autoplay){
  if (!SONGS.length) return;
  cur = (i + SONGS.length) % SONGS.length;
  const e = SONGS[cur];
  au.src = e.audio;
  $$('.jsCover').forEach(el => el.src = e.cover || '');
  $$('.jsTitle').forEach(el => el.textContent = e.title);
  const sub = '原唱：' + (e.singer ? e.singer + '《' + e.song + '》' : (e.song || '—'))
    + '　·　' + e.time + (e.score ? '　·　评分 ' + e.score : '');
  $$('.jsSub').forEach(el => el.textContent = sub);
  $$('.jsDesc').forEach(el => el.textContent = e.content || '');
  $$('.jsEnd').forEach(el => el.textContent = fmt(au.duration || 0));
  lrc = []; lrcOn = -1; renderLrc();
  if (e.lrc) fetch(e.lrc).then(r => r.ok ? r.text() : '').then(t => {
    if (e.audio === au.getAttribute('src') && t) { lrc = parseLrc(t); renderLrc(); syncLrc(au.currentTime || 0); }
  }).catch(() => {});
  $$('.ditem').forEach((d, k) => d.classList.toggle('playing', k === cur));
  if (autoplay) play(); else pauseUi();
  const on = document.querySelector('.pl.on');
  if (on) on.scrollIntoView({ inline: 'center', block: 'nearest', behavior: 'smooth' });
}
function play(){ au.play().catch(() => {}); }
function pauseUi(){ $('#cd').classList.remove('playing'); $('#cdwrap').classList.remove('playing');
  document.getElementById('tapeWin').classList.remove('playing');
  document.body.classList.remove('playing');  // 点唱机：body.playing 驱动唱片旋转/灯光加速
  $$('.jsPlay').forEach(el => el.textContent = el.classList.contains('tkey') ? '▶ PLAY' : '▶'); }
function togglePlay(){
  if (cur < 0) { load(0, true); return; }
  if (au.paused) play(); else au.pause();
}
au.onplay = () => {
  $('#cd').classList.add('playing'); $('#cdwrap').classList.add('playing');
  document.getElementById('tapeWin').classList.add('playing');
  document.body.classList.add('playing');  // 点唱机：唱片转起来、灯光加速、机身呼吸光
  $$('.jsPlay').forEach(el => el.textContent = el.classList.contains('tkey') ? '⏸ PAUSE' : '⏸');
  ensureAudioGraph();
  $$('.card audio').forEach(a => { try { a.pause(); } catch(e){} });
};
au.onpause = () => pauseUi();
au.onended = () => load(cur + 1, true);
au.ontimeupdate = () => {
  if (au.duration) {
    if (!seeking) $$('.jsSeek').forEach(el => el.value = au.currentTime / au.duration * 1000);
    const sp = document.getElementById('spFill');
    if (sp) sp.style.width = (au.currentTime / au.duration * 100) + '%';
  }
  $$('.jsCur').forEach(el => el.textContent = fmt(au.currentTime));
  $$('.jsEnd').forEach(el => el.textContent = fmt(au.duration || 0));
  document.getElementById('ledTime').textContent = fmtL(au.currentTime);
  syncLrc(au.currentTime);
};
const spTrack = document.getElementById('spTrack');
if (spTrack) spTrack.addEventListener('click', e => {
  if (!au.duration) return;
  const r = spTrack.getBoundingClientRect();
  au.currentTime = Math.max(0, Math.min(1, (e.clientX - r.left) / r.width)) * au.duration;
});
au.onloadedmetadata = () => { $$('.jsEnd').forEach(el => el.textContent = fmt(au.duration || 0)); };
$$('.jsSeek').forEach(el => {
  el.addEventListener('input', () => { seeking = true; });
  el.addEventListener('change', () => {
    if (au.duration) au.currentTime = el.value / 1000 * au.duration;
    seeking = false;
  });
});
function prev(){ load(cur - 1, true); }
function next(){ load(cur + 1, true); }
addEventListener('keydown', e => {
  if (e.code === 'Space' && document.body.dataset.view !== 'grid') { e.preventDefault(); togglePlay(); }
  if (e.code === 'ArrowRight' && e.altKey) next();
  if (e.code === 'ArrowLeft' && e.altKey) prev();
});
document.querySelectorAll('.card audio').forEach(a =>
  a.addEventListener('play', () => { if (!au.paused) au.pause(); }));
/* ============ 频谱 + 电平 + 律动 ============ */
specCv = document.getElementById('specCanvas');
scx = specCv.getContext('2d');
function specFit(){ if (!specCv) return; specCv.width = specCv.clientWidth || 640; specCv.height = specCv.clientHeight || 90; }
function drawSpec(){
  const w = specCv.width, h = specCv.height;
  scx.clearRect(0, 0, w, h);
  const L = levels(), n = 48, bw = w / n;
  for (let i = 0; i < n; i++) {
    const v = L[Math.floor(i * L.length / n / 2)] / 255;
    const bh = Math.max(2, v * h * .82);
    const grd = scx.createLinearGradient(0, h - bh, 0, h);
    grd.addColorStop(0, '#ffd76a'); grd.addColorStop(1, 'rgba(124,183,255,.35)');
    scx.fillStyle = grd;
    scx.fillRect(i * bw + 1.5, h - bh, bw - 3, bh);
  }
}
function vuSet(el, v){
  el.style.transform = 'rotate(' + (-38 + Math.min(1, v) * 76) + 'deg)';
}
function jukePulse(v){
  document.documentElement.style.setProperty('--lvl', v.toFixed(3));
}
(function vTick(){
  const view = document.body.dataset.view;
  if (view === 'spectrum' || view === 'tape' || view === 'jukebox') {
    const L = levels();
    const avg = a => a.reduce((x, y) => x + y, 0) / a.length / 255;
    const low = avg([...L.slice(0, 10)]);
    const mid = avg([...L.slice(10, 40)]);
    jukePulse(low);
    if (view === 'tape' && !au.paused) {
      vuSet(document.getElementById('vuL'), low * 1.4);
      vuSet(document.getElementById('vuR'), mid * 1.3);
    }
    if (view === 'spectrum') { specFit(); drawSpec(); }
  }
  requestAnimationFrame(vTick);
})(0);
/* ============ 视图切换 ============ */
function initCd(){
  if (cdBuilt) return;
  cdBuilt = true;
  const list = document.getElementById('dlist');
  SONGS.forEach((e, i) => {
    const d = document.createElement('div');
    d.className = 'ditem';
    d.innerHTML = `<span class="n">${String(e.seq).padStart(4, '0')}</span>` +
      (e.cover ? `<img loading="lazy" src="${e.cover}" onerror="this.remove()">` : '') +
      `<span class="c"><span class="t">${e.title}</span><span class="s">${e.singer ? e.singer + '《' + e.song + '》' : e.song} · ${e.time}</span></span>` +
      `<span class="eq"><i></i><i></i><i></i></span>`;
    d.onclick = () => { load(i, true); closeDrawer(); };
    list.appendChild(d);
  });
  const jb = document.getElementById('jkBtns');
  if (jb && !jb.children.length) {
    ['A1','A2','A3','A4','A5','A6','B1','B2','B3','B4','B5','B6'].forEach((lab, k) => {
      const b = document.createElement('button');
      b.className = 'jkb';
      b.textContent = lab;
      if (SONGS[k]) b.title = SONGS[k].title;
      b.onclick = () => { if (SONGS[k]) load(k, true); };
      jb.appendChild(b);
    });
  }
  if (SONGS.length) load(0, false);
}
function toggleDrawer(){ const d = document.getElementById('drawer');
  const open = d.classList.toggle('open');
  document.body.classList.toggle('dopen', open);
  if (open) fltDrawer(); }
function closeDrawer(){ document.getElementById('drawer').classList.remove('open');
  document.body.classList.remove('dopen'); }
function fltDrawer(){
  const q = (document.getElementById('dq') || {value: ''}).value.toLowerCase();
  [...document.querySelectorAll('.ditem')].forEach(it =>
    it.classList.toggle('hidden', !!q && !it.textContent.toLowerCase().includes(q)));
}
addEventListener('keydown', e => { if (e.key === 'Escape') closeDrawer(); });
function setView(v){
  document.body.dataset.view = v;
  document.body.classList.toggle('vgrid', v === 'grid');
  document.body.classList.toggle('vvinyl', v === 'vinyl');
  document.body.classList.toggle('vtape', v === 'tape');
  document.body.classList.toggle('vspectrum', v === 'spectrum');
  document.body.classList.toggle('vjukebox', v === 'jukebox');
  document.getElementById('segGrid').classList.toggle('on', v === 'grid');
  document.getElementById('segVinyl').classList.toggle('on', v === 'vinyl');
  document.getElementById('segTape').classList.toggle('on', v === 'tape');
  document.getElementById('segSpec').classList.toggle('on', v === 'spectrum');
  document.getElementById('segJuke').classList.toggle('on', v === 'jukebox');
  try { localStorage.setItem('kgview', v); } catch(e) {}
  if (v !== 'grid') initCd();
}
(function(){ let v = 'grid';
  const h = (location.hash || '').replace('#', '');
  if (['grid','vinyl','tape','spectrum','jukebox'].includes(h)) v = h;  // 支持 /preview/index.html#vinyl 直达皮肤
  else { try { v = localStorage.getItem('kgview') || 'grid'; } catch(e) {} }
  setView(['grid','vinyl','tape','spectrum','jukebox'].includes(v) ? v : 'grid');
})();
flt();
</script></body></html>
"""




def render_html_doc(entries, profile, generated):
    """渲染歌单页 HTML 文档字符串（不写盘）。/preview 实时渲染用，保证旧目录也是新版式。"""

    def esc(s):
        return html_mod.escape(str(s if s is not None else ''))

    cards_data = []
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
        text = ' '.join(x for x in [e.get('title'), e.get('song_name'), e.get('singer_name'),
                                    e.get('content')] if x).lower()
        cards_data.append({'h': f'''<div class="card">
  {cover_html}
  <div class="body">
    <div class="title"><a href="{esc(e['web_url'])}" target="_blank">{e['seq']:04d} {esc(e['title'])}</a></div>
    <div class="sub">{esc(e['song_name'])} · {esc(e['singer_name'])}</div>
    <div class="time">🕘 {esc(e['time'])}{duet}</div>
    {f'<div class="content">{content}</div>' if content else ''}
    <div class="stats">{'　'.join(stats)}</div>
    {player}
  </div>
</div>''', 'seq': e['seq'], 'play': e.get('play_num') or 0,
            'score': e.get('score') or 0, 'text': text})

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

    cards_json = json.dumps(cards_data, ensure_ascii=False).replace('</', '<\\/')
    doc = (PAGE_TEMPLATE
           .replace('__DATA__', data_json)
           .replace('__CARDS__', cards_json)
           .replace('__NICK__', nick)
           .replace('__STAT__', esc(stat))
           .replace('__PLAYS__', str(plays))
           .replace('__GENERATED__', esc(generated[:10]))
           .replace('__TOTAL__', str(total)))
    return doc


def write_html(out, entries, profile, generated):
    path = os.path.join(out, 'index.html')
    _write_icon(out)
    doc = render_html_doc(entries, profile, generated)
    with open(path, 'w', encoding='utf-8') as f:
        f.write(doc)
    return path


# ---------------------------------------------------------------- CD 播放器页面

CD_TEMPLATE = r"""<!DOCTYPE html>
<html lang="zh-cn"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="theme-color" content="#0b0d13">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="apple-mobile-web-app-title" content="K歌CD机">
<link rel="apple-touch-icon" href="kgicon.png">
<title>__NICK__ · 复古CD机 · 全民K歌备份</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{background:radial-gradient(1100px 700px at 72% -12%,#1d2233,#0b0d13 62%);color:#e8eaf0;height:100vh;height:100dvh;
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
.cd .cover{position:absolute;inset:0;width:100%;height:100%;border-radius:50%;object-fit:cover;
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
.ctrl button{background:none;border:1px solid #333a47;color:#dfe3ec;border-radius:50%;width:44px;height:44px;flex:none;
 font-size:17px;cursor:pointer;transition:.15s;-webkit-appearance:none;appearance:none;display:inline-flex;align-items:center;justify-content:center;padding:0;line-height:1}
.ctrl button:hover{border-color:#ffd76a;color:#ffd76a}
.ctrl .big{width:58px;height:58px;flex:none;font-size:21px;background:#ffd76a;color:#111;border:none;font-weight:700;-webkit-appearance:none;appearance:none;display:inline-flex;align-items:center;justify-content:center;line-height:1}
.time{color:#8f9aac;font-size:12px;font-variant-numeric:tabular-nums}
#seek{flex:1;min-width:0;-webkit-appearance:none;appearance:none;height:4px;border-radius:4px;background:#262c38;outline:none}
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
@media (max-width:880px){
  html,body{overflow-x:hidden}
  body{overflow:auto}
  .arm{right:-10px;top:-16px}
  .ctrl{gap:8px}
  .ctrl .big{width:50px;height:50px}
  body{overflow:auto}
  header{flex-wrap:wrap;padding:12px 16px 4px}
  main{flex-direction:column;align-items:center;gap:10px;padding:4px 16px 0}
  .deck{flex:none}
  .cdwrap{width:240px;height:240px;margin-top:2px}
  .arm{width:146px;height:220px;right:-18px;top:-24px}
  .now{margin-top:14px}
  .now .t{font-size:17px}
  .side{width:100%}
  .lyrics{min-height:210px;padding:10px 2px}
  .lyrics p{font-size:14px;padding:7px 3px}
  .lyrics p.on{font-size:16px}
}
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


def render_cd_doc(entries, profile, generated):
    """渲染 CD 播放器页 HTML 文档字符串（不写盘）。"""

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
    return doc


def write_cd_page(out, entries, profile, generated):
    path = os.path.join(out, 'cd.html')
    _write_icon(out)
    doc = render_cd_doc(entries, profile, generated)
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
    if not args.list_only:
        # 旧版本备份原地升级：补歌词 LRC + 无损重嵌标签（不重新下载音频），并刷新页面
        try:
            upgraded = upgrade_index_dir(out, client, job, profile)
            if upgraded:
                entries = upgraded
        except Exception as e:
            job.log(f'⚠ 升级通道异常（不影响备份文件）: {e}')
    csv_path = write_csv(out, entries) if entries else ''
    html_path = write_html(out, entries, profile, generated) if entries and not args.no_html else ''
    cd_path = write_cd_page(out, entries, profile, generated) if entries and not args.no_html else ''

    zip_path = ''
    if args.zip and entries:
        parent = os.path.dirname(os.path.abspath(out))
        zp = shutil.make_archive(os.path.abspath(out), 'zip',
                                 root_dir=parent, base_dir=os.path.basename(out))
        zip_path = f'{zp}（{os.path.getsize(zp) / 1048576:.0f}MB）'

    save_author_preset(uid, profile.get('nickname') or uid, out)

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




def upgrade_index_dir(dir_path, client, job, profile=None):
    """独立升级一个旧备份目录：补歌词/标签/重生成页面（不重新下载音频）。返回更新后的 entries。"""
    idx_path = os.path.join(dir_path, 'index.json')
    if not os.path.exists(idx_path):
        job.log(f'目录中没有 index.json：{dir_path}')
        return []
    with open(idx_path, encoding='utf-8') as f:
        doc = json.load(f)
    index = doc.get('works', {})
    profile = profile or doc.get('profile') or {}
    if not index:
        job.log('目录为空，无需升级')
        return []
    upgrade_old_entries(client, dir_path, index, job, profile)
    generated = ts2str(time.time())
    with open(idx_path, 'w', encoding='utf-8') as f:
        json.dump({'profile': profile, 'generated': generated, 'works': index},
                  f, ensure_ascii=False, indent=1)
    entries = list(index.values())
    write_csv(dir_path, entries)
    write_html(dir_path, entries, profile, generated)
    write_cd_page(dir_path, entries, profile, generated)
    job.log('🆙 目录升级完成：index.html / cd.html / csv 已更新为最新版式')
    return entries


def upgrade_old_entries(client, out, index, job, profile):
    """旧版本备份原地升级：补歌词 LRC + 无损重嵌标签（不重新下载音频）。"""
    todo = []
    for e in index.values():
        if not e.get('audio', '').endswith('.m4a'):
            continue
        if not os.path.exists(os.path.join(out, e['audio'])):
            continue
        if not e.get('lrc') or not e.get('tagged'):
            todo.append(e)
    if not todo:
        return
    job.log(f'🆙 检测到 {len(todo)} 首旧备份缺少歌词/标签，自动原地升级（不重新下载音频）')
    done = 0
    for e in todo:
        if job.cancelled:
            break
        try:
            ksong_mid = ''
            tkey = e.get('tkey') or ''
            if '_' in tkey:
                ksong_mid = tkey.split('_', 1)[1]
            if not ksong_mid and e.get('meta'):
                mp = os.path.join(out, e['meta'])
                if os.path.exists(mp):
                    try:
                        with open(mp, encoding='utf-8') as mf:
                            ksong_mid = (json.load(mf).get('detail') or {}).get('ksong_mid') or ''
                    except Exception:
                        pass
            lyric = ''
            if ksong_mid and not e.get('lrc'):
                lyric = client.fetch_lyric(ksong_mid)
            if lyric:
                lrc_rel = e['audio'][:-4] + '.lrc'
                try:
                    with open(os.path.join(out, lrc_rel), 'w', encoding='utf-8') as f:
                        f.write(lyric)
                    e['lrc'] = lrc_rel
                except OSError:
                    pass
            if ffmpeg_available() and (not e.get('tagged') or lyric):
                cover_abs = os.path.join(out, e.get('cover') or '')
                bits = []
                if e.get('song_name'):
                    singer = e.get('singer_name') or ''
                    bits.append(f'原唱：{singer}《{e["song_name"]}》' if singer
                                else f'原唱：《{e["song_name"]}》')
                if e.get('score'):
                    bits.append(f'评分 {e["score"]}')
                if e.get('play_num') is not None:
                    bits.append(f'播放 {e["play_num"]}')
                if e.get('device'):
                    bits.append(f'录制设备 {e["device"]}')
                e['tagged'] = embed_tags(os.path.join(out, e['audio']),
                                         cover_abs if os.path.exists(cover_abs) else '',
                                         e.get('title') or '', profile.get('nickname', ''),
                                         (e.get('time') or '')[:10],
                                         e.get('content') or '',
                                         lyric=lyric, desc=' · '.join(bits), job=job)
            done += 1
            if done % 20 == 0 or done == len(todo):
                job.log(f'  🆙 升级进度 {done}/{len(todo)}')
            time.sleep(0.15)
        except Exception as ex:
            job.log(f'  ⚠ 升级跳过《{e.get("title")}》: {ex}')
    if done:
        job.log(f'🆙 升级完成：{done} 首已补齐歌词/标签')


GUEST_TEMPLATE = r"""<!DOCTYPE html>
<html lang="zh-cn"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="theme-color" content="#0f1220">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="apple-mobile-web-app-title" content="盲盒歌单">
<link rel="manifest" href="/static/guest-manifest.json">
<link rel="apple-touch-icon" href="/static/icon-192.png">
<link rel="icon" href="/static/icon-192.png">
<title>__FEST_TITLE____NICK__ 的盲盒歌单</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{background:radial-gradient(900px 600px at 50% -8%,#1d2233,#0b0d13 58%);color:#e8eaf0;min-height:100vh;min-height:100dvh;
 font-family:-apple-system,BlinkMacSystemFont,"SF Pro Text","PingFang SC","Microsoft YaHei",sans-serif;overflow-x:hidden;overflow-y:auto;padding-bottom:104px;
 -webkit-tap-highlight-color:transparent;-webkit-font-smoothing:antialiased;text-rendering:optimizeLegibility;
 overscroll-behavior-y:auto;-webkit-overflow-scrolling:touch}
button{touch-action:manipulation}
canvas#fx{position:fixed;inset:0;z-index:1;pointer-events:none}
header{position:sticky;top:0;z-index:9;text-align:center;padding:22px 16px 12px;background:rgba(15,18,32,.72);
 backdrop-filter:blur(22px) saturate(1.6);-webkit-backdrop-filter:blur(22px) saturate(1.6);
 border-bottom:1px solid rgba(255,255,255,.07)}
body.light header{background:rgba(247,248,252,.8);border-bottom-color:rgba(0,0,0,.06)}
header h1{font-size:19px}
header .sub{color:#9aa3b8;font-size:12px;margin-top:6px}
header .gold{color:#ffd76a}
.fest-banner{display:none;margin-top:10px;font-size:14px;font-weight:700;color:#ffd76a;
 text-shadow:0 0 16px rgba(255,215,106,.5)}
body.fest .fest-banner{display:block}
.wrap{max-width:760px;margin:0 auto;padding:22px 16px 0;position:relative;z-index:2}
/* 今日盲盒 */
.today{position:relative;text-align:center;border-radius:26px;padding:30px 22px 26px;margin-bottom:30px;
 background:linear-gradient(180deg,rgba(255,255,255,.055),rgba(255,255,255,.02));
 box-shadow:0 0 0 1px rgba(255,255,255,.08) inset,0 18px 50px rgba(0,0,0,.45);backdrop-filter:blur(16px);-webkit-backdrop-filter:blur(16px)}
body.fest-cny .today,body.fest-christmas .today{background:linear-gradient(180deg,#2e1420,#1c0f1c)}
.today .day{color:#9aa3b8;font-size:12px;letter-spacing:2px}
.gift{font-size:84px;line-height:1.15;display:inline-block;cursor:pointer;user-select:none;
 animation:sway 2.6s ease-in-out infinite;filter:drop-shadow(0 14px 26px rgba(255,215,106,.28));transition:transform .5s}
@keyframes sway{0%,100%{transform:rotate(-7deg)}50%{transform:rotate(7deg) translateY(-8px)}}
.gift.opened{animation:pop .55s ease-out}
@keyframes pop{40%{transform:scale(1.35) rotate(9deg)}70%{transform:scale(.94)}100%{transform:scale(1)}}
.gift.shaking{animation:shakeHard .42s ease-in-out 2}
@keyframes shakeHard{0%,100%{transform:rotate(0) scale(1)}25%{transform:rotate(-16deg) scale(1.06)}75%{transform:rotate(16deg) scale(1.06)}}
.rev-area{display:none;gap:18px;justify-content:center;flex-wrap:wrap;margin-top:6px;perspective:900px}
.rev-area.show{display:flex}
/* 老 iOS（如 15.6）跑不动 88° 侧翻 + 大阴影：角度放缓、强制合成层、隐藏背面（修"缺一半"） */
.rev-card{width:min(280px,64vw);border-radius:18px;overflow:visible;background:transparent;border:none;
 animation:reveal .8s cubic-bezier(.2,.9,.3,1.15) both;text-align:center;
 transform-origin:50% 50%;backface-visibility:hidden;-webkit-backface-visibility:hidden;
 will-change:transform,opacity;transform:translateZ(0)}
.rev-card:nth-child(2){animation-delay:.35s}
@keyframes reveal{0%{transform:rotateY(62deg) scale(.86);opacity:0}
 60%{transform:rotateY(-8deg) scale(1.02);opacity:1}
 100%{transform:rotateY(0) scale(1)}}
.rev-card img{width:100%;aspect-ratio:1;object-fit:cover;display:block;border-radius:22px;cursor:pointer;
 box-shadow:0 20px 50px rgba(0,0,0,.5);transition:transform .2s}
/* 手机上阴影减半、彩带去描边，动画期间不触发大面积重绘 */
@media (max-width:700px){
 .rev-card img{box-shadow:0 10px 24px rgba(0,0,0,.35)}
 .confetti{text-shadow:none}
}
/* 开盒仪式进行中：暂停毛玻璃（老 iPhone 的卡顿大户），结束后恢复 */
body.ceremony header,body.ceremony .today{backdrop-filter:none;-webkit-backdrop-filter:none}
.rev-card img:active{transform:scale(.97)}
.rev-card .rt{font-size:15px;font-weight:800;color:#ffd76a;padding:12px 8px 2px}
.rev-card .rs{font-size:11.5px;color:#9aa3b8;padding:0 8px 12px}
/* iOS 毛玻璃胶囊按钮（点封面也能直接播放） */
.rev-play{display:inline-block;margin-top:2px;border:1px solid rgba(255,255,255,.16);cursor:pointer;font-family:inherit;
 font-size:13px;font-weight:700;color:#f2f4fa;background:rgba(255,255,255,.1);backdrop-filter:blur(14px);
 -webkit-backdrop-filter:blur(14px);border-radius:999px;padding:10px 30px;-webkit-appearance:none;appearance:none;transition:transform .18s,background .18s}
.rev-play:hover{background:rgba(255,255,255,.17)}
.rev-play:active{transform:scale(.94)}
.rev-title{font-size:19px;font-weight:800;color:#ffd76a;display:none;margin-top:12px}
.rev-title.show{display:block}
.btn{display:inline-block;border:none;cursor:pointer;font-family:inherit;font-size:16px;font-weight:700;color:#221d38;
 background:linear-gradient(180deg,#ffd76a,#e8b64c);border-radius:999px;padding:13px 34px;margin-top:16px;
 box-shadow:0 10px 28px rgba(255,215,106,.3);-webkit-appearance:none;appearance:none;transition:transform .18s,box-shadow .18s}
.btn:active{transform:scale(.95)}
.count{color:#8f9aac;font-size:12.5px;margin-top:14px}
.count b{color:#ffd76a;font-variant-numeric:tabular-nums}
/* 解锁柜 */
.sec{margin:26px 0 14px;display:flex;align-items:baseline;gap:10px}
.sec h2{font-size:16px}
.sec span{color:#8f9aac;font-size:12px}
.cabinet{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:14px}
.item{background:#171a26;border-radius:16px;overflow:hidden;cursor:pointer;transition:transform .18s,border-color .18s;position:relative;border:1px solid rgba(255,255,255,.07)}
.item:hover{transform:translateY(-3px);border-color:rgba(255,255,255,.16)}
.item:active{transform:scale(.97)}
.item img{width:100%;height:118px;object-fit:cover;display:block}
.item .nm{padding:9px 11px 3px;font-size:13px;font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.item .dt{padding:0 11px 10px;font-size:11px;color:#8f9aac}
.item .ud{padding:0 11px 10px;font-size:11px;color:#ffd76a}
.item.locked{cursor:default}
.item.locked img{filter:brightness(.12) blur(6px)}
.item .q{position:absolute;inset:0 0 34px;display:flex;align-items:center;justify-content:center;font-size:34px;color:#4a5468}
.item .tag{position:absolute;top:8px;left:8px;font-size:10px;background:rgba(255,215,106,.9);color:#221d38;
 border-radius:6px;padding:2px 7px;font-weight:700;z-index:2}
.item.locked .tag{background:rgba(124,140,180,.35);color:#c7d1e8}
.item .ftag{position:absolute;top:8px;right:8px;font-size:10px;background:rgba(255,79,163,.9);color:#fff;
 border-radius:6px;padding:2px 7px;font-weight:700;z-index:2}
.item.shake{animation:shk .4s}
@keyframes shk{25%{transform:translateX(-5px)}50%{transform:translateX(5px)}75%{transform:translateX(-3px)}}
.more-btn{display:block;margin:16px auto 4px;border:1px solid rgba(255,255,255,.12);background:rgba(255,255,255,.06);color:#9aa3b8;
 font-family:inherit;font-size:13px;font-weight:600;border-radius:999px;padding:10px 22px;cursor:pointer;
 -webkit-appearance:none;appearance:none;transition:.15s}
.more-btn:hover{border-color:#ffd76a;color:#ffd76a}
body.light .more-btn{background:#fff;border-color:#d8dce8;color:#5a6474}
.history{margin-top:8px;display:flex;flex-direction:column;gap:8px}
.hrow{display:flex;align-items:center;gap:12px;background:#171a26;border-radius:14px;padding:10px 14px;border:1px solid rgba(255,255,255,.07)}
.hrow .d{color:#ffd76a;font-size:12px;font-variant-numeric:tabular-nums;flex:none}
.hrow .t{font-size:13px;font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.hrow .s{color:#8f9aac;font-size:11px;margin-left:auto;flex:none}
.empty{text-align:center;color:#6b7488;padding:40px 10px;font-size:14px;line-height:2}
/* 播放条（iOS 毛玻璃迷你播放器，高度恒定不跳动） */
.bar{position:fixed;left:0;right:0;bottom:0;z-index:20;background:rgba(22,24,36,.78);
 backdrop-filter:blur(24px) saturate(1.7);-webkit-backdrop-filter:blur(24px) saturate(1.7);
 border-top:1px solid rgba(255,255,255,.07);padding:8px 14px calc(8px + env(safe-area-inset-bottom));
 display:none;align-items:center;gap:10px;flex-wrap:wrap}
.bar.show{display:flex}
.bar img{width:40px;height:40px;border-radius:9px;object-fit:cover;box-shadow:0 4px 14px rgba(0,0,0,.4)}
.bar .bi{flex:1;min-width:0}
.bar .bt{font-size:12.5px;font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.bar .bs{color:#8f9aac;font-size:10.5px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.bar button{background:rgba(255,255,255,.08);border:none;color:#f2f4fa;border-radius:50%;width:36px;height:36px;flex:none;
 font-size:14px;cursor:pointer;-webkit-appearance:none;appearance:none;display:inline-flex;align-items:center;justify-content:center;
 padding:0;line-height:1;-webkit-tap-highlight-color:transparent;outline:none;transition:transform .15s,background .15s}
.bar button:hover{background:rgba(255,255,255,.15)}
.bar button:active{transform:scale(.88)}
.bar button:focus-visible{outline:2px solid #ffd76a}
.bar .bp{background:#ffd76a;color:#221d38;font-size:16px;width:40px;height:40px}
.bar .bp:hover{background:#ffe28a}
.bar-lyric{width:100%;text-align:center;color:#ffd76a;font-size:12px;padding:0 4px;white-space:nowrap;
 overflow:hidden;text-overflow:ellipsis;min-height:16px;line-height:16px;margin-bottom:-2px}

/* 留言板 */
.comments-sec{margin:26px 0 14px}
.comments-sec h2{font-size:16px;margin-bottom:12px}
.comment-form{display:flex;gap:8px;margin-bottom:14px;flex-wrap:wrap}
.comment-form input{padding:10px 12px;border-radius:12px;border:1px solid rgba(255,255,255,.1);background:rgba(255,255,255,.06);color:#eef1f7;font-size:13px;outline:none;transition:.15s}
.comment-form input:focus{border-color:#ffd76a;background:rgba(255,255,255,.09)}
.comment-form .cname{width:90px}
.comment-form .ctext{flex:1;min-width:0}
.comment-form .cbtn{padding:10px 20px;border:none;border-radius:999px;background:#ffd76a;color:#221d38;font-weight:700;cursor:pointer;font-family:inherit;-webkit-appearance:none;appearance:none;transition:transform .15s}
.comment-form .cbtn:active{transform:scale(.94)}
.clist{display:flex;flex-direction:column;gap:8px}
.crow{background:#171a26;border-radius:14px;padding:10px 14px;border:1px solid rgba(255,255,255,.07)}
.crow .cn{font-size:12px;font-weight:700;color:#ffd76a}
.crow .ct{font-size:13px;color:#cfd6dd;margin-top:3px;word-break:break-all}
.crow .cd{font-size:10px;color:#6b7488;margin-top:3px}
/* 机主回复（跟随原留言展示给这位访客） */
.crow .creply{margin:7px 0 2px;padding:7px 10px;border-left:3px solid #ffd76a;
 background:rgba(255,215,106,.09);border-radius:0 9px 9px 0;font-size:12px;line-height:1.6;color:#e8eaf0;word-break:break-all}
.crow .creply b{color:#ffd76a;font-size:11px}
body.light .crow .creply{background:rgba(184,134,11,.08);color:#1d2430}
.cempty{color:#6b7488;text-align:center;padding:20px;font-size:13px}
/* 最新收录 */
.latest-sec{margin:26px 0 14px;display:flex;align-items:baseline;gap:10px}
.latest-sec h2{font-size:16px}
.latest-sec span{color:#8f9aac;font-size:12px}
.latest-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:12px;margin-bottom:14px}
.lcard{background:#171a26;border-radius:14px;overflow:hidden;cursor:pointer;border:1px solid rgba(127,255,178,.25);transition:transform .18s,border-color .18s}
.lcard:hover{transform:translateY(-3px);border-color:#7cffb2}
.lcard:active{transform:scale(.97)}
.lcard img{width:100%;height:100px;object-fit:cover}
.lcard .lt{padding:8px 10px 3px;font-size:12px;font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.lcard .ls{padding:0 10px 8px;font-size:10px;color:#7cffb2}
/* 播放计数 */
.pcount{display:inline-flex;align-items:center;gap:3px;font-size:10px;color:#8f9aac}
/* 主题切换（固定右上角小按钮，不再挡歌柜） */
.theme-toggle{position:fixed;top:10px;right:10px;z-index:9;width:34px;height:34px;border-radius:50%;border:1px solid rgba(255,255,255,.12);
 background:rgba(255,255,255,.07);backdrop-filter:blur(14px);-webkit-backdrop-filter:blur(14px);color:#ffd76a;font-size:14px;cursor:pointer;
 -webkit-appearance:none;appearance:none;display:inline-flex;align-items:center;justify-content:center;transition:.2s}
.theme-toggle:hover{border-color:#ffd76a}
.theme-toggle:active{transform:scale(.9)}
/* PWA 安装按钮 */
.pwa-btn{display:none;margin-top:12px;border:1px solid rgba(255,255,255,.12);background:rgba(255,255,255,.07);
 font-family:inherit;font-size:12.5px;font-weight:600;border-radius:999px;padding:8px 18px;cursor:pointer;
 -webkit-appearance:none;appearance:none;transition:.15s}
.pwa-btn:hover{border-color:#7cb8ff;color:#7cb8ff}
body.light .pwa-btn{background:#fff;border-color:#d8dce8;color:#5a7ea8}
/* 明亮主题（body.light） */
body.light{background:#f2f4f8;color:#2a3140}
body.light header{background:rgba(247,248,252,.92);border-bottom-color:#dfe3ec}
body.light header h1{color:#1d2430}
body.light header .sub{color:#5a6474}
body.light .today{background:linear-gradient(180deg,#ffffff,#eef0f7);
 box-shadow:0 0 0 1px #d8dce8 inset,0 20px 60px rgba(40,50,90,.16)}
body.light .today .day{color:#6a7488}
body.light .rev-card{background:#fff;border-color:#dfe3ec}
body.light .rev-card .rs{color:#5a6474}
body.light .item,body.light .lcard,body.light .hrow,body.light .crow{background:#fff;border-color:#dfe3ec}
body.light .item .nm,body.light .lcard .lt,body.light .hrow .t,body.light .crow .ct{color:#1d2430}
body.light .item .dt,body.light .hrow .s,body.light .item .q,body.light .pcount{color:#7c8698}
body.light .item .q{color:#aab3c5}
/* 浅色模式下的「未解锁」卡：封面整块隐去，只留干净的浅色底 + 小锁，不再灰蒙蒙 */
body.light .item.locked{background:linear-gradient(180deg,#f8f9fd,#eef1f8);border-color:#e5e9f3}
body.light .item.locked img{opacity:0;filter:none}
body.light .item.locked .q{color:#bcc4d6;font-size:26px}
body.light .item.locked .nm{color:#9aa3b6}
body.light .item.locked .dt{color:#b3bbcb}
body.light .item.locked .tag{background:rgba(124,140,180,.14);color:#7c8698}
body.light .rev-card img{box-shadow:0 16px 38px rgba(40,50,90,.18)}
body.light .empty,body.light .cempty{color:#8a94a6}
body.light .count,body.light .latest-sec span,body.light .sec span{color:#6a7488}
body.light .bar{background:rgba(255,255,255,.78);border-top-color:rgba(0,0,0,.06)}
body.light .bar .bt{color:#1d2430}
body.light .bar .bs,body.light .bar-lyric{color:#7c8698}
body.light .bar-lyric{color:#b8860b}
body.light .bar button{background:rgba(0,0,0,.06);color:#39414f}
body.light .bar button:hover{background:rgba(0,0,0,.11)}
body.light .comment-form input{background:#fff;border-color:#d8dce8;color:#1d2430}
body.light .theme-toggle{background:#fff;border-color:#c9cfdd}
/* 彩带（揭晓动画） */
.confetti{position:absolute;left:50%;top:40%;font-size:18px;pointer-events:none;
 animation:confetti 1s ease-out forwards;text-shadow:0 2px 8px rgba(0,0,0,.3)}
.confetti.big{font-size:27px}
@keyframes confetti{
 0%{transform:translate(0,0) scale(.6);opacity:1}
 100%{transform:translate(var(--dx),var(--dy)) scale(1.25) rotate(var(--dx));opacity:0}}
/* 歌词偏移 */
.offset-ctl{display:flex;align-items:center;justify-content:center;gap:6px;font-size:11px;color:#8f9aac;margin-top:4px}
.offset-ctl button{background:#1a1f26;border:1px solid #3a4150;color:#ccc;border-radius:5px;padding:2px 7px;cursor:pointer;font-size:10px;font-family:inherit}
.offset-ctl button:hover{border-color:#ffd76a;color:#ffd76a}
/* 无脑化大按钮 */
.gift{min-width:100px;min-height:100px;display:inline-flex;align-items:center;justify-content:center}
.btn{padding:15px 38px;font-size:17px}
.rev-play{padding:12px 34px;font-size:14px}

/* 播放进度（一行 12px：细线 + 时间，高度恒定不抖） */
.ptop{width:100%;display:flex;align-items:center;gap:9px;height:12px;margin:-1px 0 1px}
.pbar{position:relative;flex:1;height:12px;display:flex;align-items:center;cursor:pointer;touch-action:none}
.pbar i{display:block;width:100%;height:3px;border-radius:3px;background:rgba(255,255,255,.13);overflow:hidden}
.pfill{display:block;height:100%;width:0;border-radius:3px;background:linear-gradient(90deg,#ffd76a,#ffb347);transition:width .2s linear}
.bar-time{flex:none;font-size:10px;color:#8f9aac;font-variant-numeric:tabular-nums;letter-spacing:.2px}
body.light .pbar i{background:rgba(0,0,0,.1)}
body.light .bar-time{color:#7c8698}
/* 播放模式（单按钮循环：顺序 🔁 / 单曲 🔂 / 随机 🔀，不占额外高度） */
.bar #modeBtn{width:32px;height:32px;font-size:13px;background:transparent;border:1px solid rgba(255,255,255,.14);color:#aab3c5}
.bar #modeBtn:hover{background:rgba(255,255,255,.1)}
.bar #modeBtn.act{border-color:rgba(255,215,106,.75);color:#ffd76a}
body.light .bar #modeBtn{border-color:rgba(0,0,0,.14);color:#6a7488}
body.light .bar #modeBtn.act{border-color:rgba(184,134,11,.6);color:#b8860b}
@media (max-width:420px){
 .bar{gap:7px;padding-left:11px;padding-right:11px}
 .bar button{width:32px;height:32px;font-size:13px}
 .bar .bp{width:38px;height:38px}
 .bar #modeBtn{width:28px;height:28px;font-size:12px}
 .bar img{width:36px;height:36px}
}
/* 猫提示（留言送达 / 机主消息）：右上角堆叠，轻反馈不打断 */
#catStack{position:fixed;top:70px;right:14px;z-index:50;display:flex;flex-direction:column;gap:12px;
 max-width:320px;pointer-events:none}
.cat-notify{position:relative;pointer-events:auto;overflow:hidden;
 background:linear-gradient(160deg,rgba(40,44,66,.97),rgba(24,27,42,.97));
 backdrop-filter:blur(18px);-webkit-backdrop-filter:blur(18px);
 border:1px solid rgba(255,255,255,.12);border-radius:20px 20px 6px 20px;padding:14px 16px 16px;
 box-shadow:0 14px 40px rgba(0,0,0,.5),0 0 0 1px rgba(255,215,106,.08) inset;
 animation:catpop .5s cubic-bezier(.2,.9,.3,1.3)}
@keyframes catpop{0%{transform:scale(.4) translateY(-26px);opacity:0}100%{transform:scale(1) translateY(0);opacity:1}}
/* 底部 3px 倒计时条：走完自动消失 */
.cat-notify::after{content:'';position:absolute;left:0;bottom:0;height:3px;width:100%;
 background:linear-gradient(90deg,#ffd76a,#ff9d5c);transform-origin:left;animation:catLife 7s linear forwards}
@keyframes catLife{from{transform:scaleX(1)}to{transform:scaleX(0)}}
.cat-notify .cat-head{display:flex;align-items:center;gap:11px;margin-bottom:8px}
/* 黑猫本体（内嵌 PNG，不依赖任何请求），轻轻浮动卖萌 */
.cat-notify .cat-face{width:46px;height:46px;flex:none;border-radius:14px;
 background:url(data:image/png;base64,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) center/contain no-repeat;
 animation:catFloat 3.2s ease-in-out infinite;filter:drop-shadow(0 4px 10px rgba(0,0,0,.4))}
@keyframes catFloat{0%,100%{transform:translateY(0) rotate(-2deg)}50%{transform:translateY(-4px) rotate(2deg)}}
.cat-notify .cat-title{font-size:12px;font-weight:800;color:#ffd76a;letter-spacing:.5px}
.cat-notify .cat-msg{font-size:13px;color:#e8eaf0;line-height:1.65;word-break:break-all}
.cat-notify .cat-msg b{color:#ffd76a}
.cat-notify .cat-close{position:absolute;top:8px;right:10px;cursor:pointer;color:#8f9aac;font-size:14px;line-height:1;z-index:2}
body.light .cat-notify{background:rgba(255,255,255,.97);border-color:rgba(0,0,0,.1);
 box-shadow:0 14px 36px rgba(40,50,90,.22)}
body.light .cat-notify .cat-title{color:#b8860b}
body.light .cat-notify .cat-msg{color:#1d2430}
/* 底部弹出说明（安装引导等） */
.sheet-mask{position:fixed;inset:0;z-index:60;display:none;align-items:flex-end;justify-content:center;
 background:rgba(6,8,14,.55);backdrop-filter:blur(6px);-webkit-backdrop-filter:blur(6px);padding:16px}
.sheet-mask.show{display:flex;animation:fadeIn .22s ease-out}
@keyframes fadeIn{from{opacity:0}to{opacity:1}}
.sheet{width:min(420px,100%);background:rgba(26,30,46,.97);border:1px solid rgba(255,255,255,.12);border-radius:22px;
 padding:20px 20px calc(20px + env(safe-area-inset-bottom));color:#e8eaf0;box-shadow:0 24px 60px rgba(0,0,0,.6);
 animation:sheetUp .3s cubic-bezier(.2,.9,.3,1.1)}
@keyframes sheetUp{from{transform:translateY(28px);opacity:.5}to{transform:none;opacity:1}}
.sheet h3{font-size:16px;margin-bottom:10px}
.sheet p,.sheet li{font-size:13px;line-height:1.75;color:#b8c1d4}
.sheet ol{padding-left:18px;margin:6px 0}
.sheet .sheet-btns{display:flex;gap:10px;margin-top:16px}
.sheet .sb{flex:1;padding:12px;border-radius:14px;border:none;font-size:14px;font-weight:700;font-family:inherit;
 cursor:pointer;-webkit-appearance:none;appearance:none}
.sheet .sb.primary{background:linear-gradient(180deg,#ffd76a,#e8b64c);color:#221d38}
.sheet .sb.ghost{background:rgba(255,255,255,.1);color:#e8eaf0}
body.light .sheet{background:#fff;border-color:#e2e6f0;color:#1d2430}
body.light .sheet p,body.light .sheet li{color:#5a6474}
body.light .sheet .sb.ghost{background:rgba(0,0,0,.06);color:#39414f}
@media (max-width:420px){#catStack{left:12px;right:12px;top:64px;max-width:none}}
</style></head><body class="__FEST_CLASS__">

<canvas id="fx"></canvas>
<header>
 <h1>__FEST_EMOJI__ <span class="gold">__NICK__</span> 的盲盒歌单</h1>
 <div class="sub">每天自动掉落一首 __NICK__ 唱过的歌 · 已解锁 <span class="gold" id="unlockedN">__UNLOCKED__</span>/<b>__TOTAL__</b> 首 · 已解锁的永久可听</div>
 <div class="fest-banner" id="festBanner"></div>
 <button class="pwa-btn" id="pwaBtn" onclick="installPwa()">📲 安装到桌面，每天回来开盲盒</button>
</header>
<div class="sheet-mask" id="installSheet" onclick="if(event.target===this)closeSheet('installSheet')">
 <div class="sheet">
  <h3 id="sheetTitle">📲 装到桌面</h3>
  <div id="sheetBody"></div>
  <div class="sheet-btns">
   <button class="sb primary" id="sheetGo" onclick="sheetAction()">立即安装</button>
   <button class="sb ghost" onclick="closeSheet('installSheet')">稍后再说</button>
  </div>
 </div>
</div>
<div class="wrap">
 <div class="today">
  <div class="day">__TODAY__ · 今日盲盒<span id="festTag"></span></div>
  <div id="giftArea">
   <div class="gift" id="gift" onclick="reveal()">🎁</div>
   <div class="rev-area" id="revArea"></div>
   <div class="rev-title" id="revTitle"></div>
   <button class="btn" id="openBtn" onclick="reveal()">开启今日盲盒</button>
  </div>
  <div class="count">下一首盲盒 <b id="cd">--:--:--</b> 后掉落 · 记得回来看看</div>
 </div>

 <div class="latest-sec"><h2>🆕 最新收录</h2><span>最近加入盲盒的歌</span></div>
 <div class="latest-grid" id="latestGrid"></div>

 <div class="sec"><h2>🔓 已解锁的歌柜</h2><span>点封面播放 · 全部可反复听</span></div>
 <div class="cabinet" id="cabinet"></div>

 <div class="sec"><h2>💬 我的留言</h2><span>仅自己和机主可见</span></div>
 <div class="comments-sec">
  <div class="comment-form">
   <input class="cname" id="cName" placeholder="你的昵称（默认当前账号）" maxlength="20">
   <input class="ctext" id="cText" placeholder="说点什么…" maxlength="200">
   <button class="cbtn" onclick="submitComment()">发留言</button>
  </div>
  <div class="clist" id="cList"></div>
 </div>

 <div class="sec"><h2>📅 掉落日历</h2><span>每一首都有自己的日期</span></div>
 <div class="history" id="history"></div>
</div>
<button class="theme-toggle" onclick="toggleTheme()" title="切换主题">🌗</button>
<script>
/* 主题初始化（放在渲染前，避免闪白） */
try {
  if (localStorage.getItem('kgtheme') === 'light') {
    document.body.classList.add('light');
    document.querySelector('.theme-toggle').textContent = '🌙';
  }
} catch(e){}
function toggleTheme(){
  const light = !document.body.classList.contains('light');
  document.body.classList.toggle('light', light);
  try { localStorage.setItem('kgtheme', light ? 'light' : 'dark'); } catch(e){}
  const t = document.querySelector('.theme-toggle');
  if (t) t.textContent = light ? '🌙' : '🌗';
}
</script>
<div class="bar" id="bar">
 <div class="ptop">
  <div class="pbar" id="pbar"><i><span class="pfill" id="pfill"></span></i></div>
  <span class="bar-time" id="barTime">0:00 / 0:00</span>
 </div>
 <div class="bar-lyric" id="barLyric"></div>
 <img id="barCover" alt="">
 <div class="bi"><div class="bt" id="barTitle">—</div><div class="bs" id="barSub"></div>
 </div>
 <button onclick="barPrev()">⏮</button>
 <button class="bp" id="barPlay" onclick="barToggle()">▶</button>
 <button onclick="barNext()">⏭</button>
 <button id="modeBtn" onclick="cycleMode()" title="播放模式：顺序">🔁</button>
</div>
<audio id="au" preload="none"></audio>
<script>const SONGS = __DATA__;const HISTORY = __HISTORY__;const FEST = __FEST__;</script>
<script>
const $ = s => document.querySelector(s);
const au = document.getElementById('au');
let cur = -1;
const unlocked = SONGS.filter(s => !s.locked);
const dateMap = {};
HISTORY.forEach(h => { dateMap[h.uk] = h; });
let lyricOffset = 0;
try { lyricOffset = parseFloat(localStorage.getItem('kgoffset') || '0'); } catch(e){}
/* 已解锁的排前面（按掉落日期倒序），未解锁按发行时间 */
SONGS.sort((a, b) => {
  if (a.today) return -1;
  if (b.today) return 1;
  const au_ = !a.locked, bu_ = !b.locked;
  if (au_ !== bu_) return au_ ? -1 : 1;
  if (au_) return (dateMap[b.uk] ? dateMap[b.uk].date : '').localeCompare(dateMap[a.uk] ? dateMap[a.uk].date : '');
  return a.seq - b.seq;
});
/* 节日横幅 */
if (FEST) {
  const b = document.getElementById('festBanner');
  b.textContent = FEST.double ? ('🧨 ' + FEST.name + '快乐！今日双倍盲盒掉落') : ('🎉 ' + FEST.name + ' 彩蛋装扮中');
  b.parentElement.querySelector('h1').firstElementChild.textContent = FEST.gift;
  const tag = document.getElementById('festTag');
  tag.textContent = ' · ' + FEST.gift + ' ' + FEST.name + '彩蛋';
}
/* 节日特效层 */
const FX = FEST ? FEST.effect : null;
const fcv = document.getElementById('fx'), fxc = fcv.getContext('2d');
function fxResize(){ fcv.width = innerWidth; fcv.height = innerHeight; }
fxResize(); addEventListener('resize', fxResize);
let fparts = [], rockets = [], lastRocket = 0;
function spawnCount(){ return {snow:90,petals:46,hearts:26,lanterns:16,balloons:14}[FX] || 0; }
function spawn(i){
  if (FX === 'snow') return {x:Math.random()*fcv.width, y:-12, r:1+Math.random()*2.6, vy:.4+Math.random()*1.1, vx:(Math.random()-.5)*.6, o:.25+Math.random()*.55, ph:Math.random()*6.3};
  if (FX === 'petals') return {x:Math.random()*fcv.width, y:-14, r:3+Math.random()*4, vy:.5+Math.random()*.9, vx:(Math.random()-.5)*.9, rot:Math.random()*6.3, vr:(Math.random()-.5)*.09, o:.35+Math.random()*.4};
  if (FX === 'hearts') return {x:Math.random()*fcv.width, y:fcv.height+16, r:5+Math.random()*7, vy:.5+Math.random()*.8, vx:(Math.random()-.5)*.4, o:.28+Math.random()*.4, ph:Math.random()*6.3};
  if (FX === 'lanterns') return {x:Math.random()*fcv.width, y:fcv.height+24, r:9+Math.random()*11, vy:.32+Math.random()*.5, vx:(Math.random()-.5)*.3, o:.32+Math.random()*.35, ph:Math.random()*6.3};
  if (FX === 'balloons') return {x:Math.random()*fcv.width, y:fcv.height+24, r:8+Math.random()*9, vy:.4+Math.random()*.6, vx:(Math.random()-.5)*.4, o:.4+Math.random()*.3, hue:Math.random()*360};
  return null;
}
if (FX && FX !== 'fireworks') { const n = spawnCount(); for (let i = 0; i < n; i++) { const p = spawn(i); if (p) { if (FX !== 'snow') p.y = Math.random() * fcv.height; fparts.push(p); } } }
function drawFireworks(t){
  if (t - lastRocket > 1500 && rockets.length < 3) {
    lastRocket = t;
    rockets.push({x: fcv.width * (.15 + Math.random() * .7), y: fcv.height, vy: -(6 + Math.random() * 4), ty: fcv.height * (.18 + Math.random() * .3), hue: Math.random() * 360, boom: false, parts: []});
  }
  for (const r of rockets) {
    if (!r.boom) {
      r.y += r.vy;
      fxc.fillStyle = 'hsla(' + r.hue + ',100%,75%,.9)';
      fxc.fillRect(r.x - 1.5, r.y, 3, 10);
      if (r.y <= r.ty) { r.boom = true;
        for (let i = 0; i < 46; i++) { const a = Math.random() * 6.28, sp = 1 + Math.random() * 3.4;
          r.parts.push({x: r.x, y: r.y, vx: Math.cos(a) * sp, vy: Math.sin(a) * sp, life: 1, hue: r.hue + Math.random() * 50 - 25}); }
      }
    } else {
      for (const pt of r.parts) {
        pt.x += pt.vx; pt.y += pt.vy; pt.vy += .045; pt.life -= .016;
        if (pt.life > 0) { fxc.fillStyle = 'hsla(' + pt.hue + ',100%,' + (55 + pt.life * 25) + '%,' + pt.life + ')'; fxc.fillRect(pt.x, pt.y, 2.6, 2.6); }
      }
    }
  }
  for (let i = rockets.length - 1; i >= 0; i--) if (rockets[i].boom && rockets[i].parts.every(p => p.life <= 0)) rockets.splice(i, 1);
}
if (FX) (function fxTick(t){
  fxc.clearRect(0, 0, fcv.width, fcv.height);
  if (FX === 'fireworks') drawFireworks(t);
  for (const p of fparts) {
    p.y += p.vy; p.x += p.vx;
    if (FX === 'petals') { p.rot += p.vr; p.x += Math.sin(t / 1400 + p.ph) * .7; }
    if (FX === 'hearts' || FX === 'lanterns') p.x += Math.sin(t / 1800 + p.ph) * .5;
    if (p.y > fcv.height + 30) { p.y = -24; p.x = Math.random() * fcv.width; }
    if (p.y < -30) { p.y = fcv.height + 24; p.x = Math.random() * fcv.width; }
    if (p.x > fcv.width + 30) p.x = -20; if (p.x < -30) p.x = fcv.width + 20;
    fxc.save();
    if (FX === 'snow') { fxc.beginPath(); fxc.arc(p.x, p.y, p.r, 0, 6.28); fxc.fillStyle = 'rgba(255,255,255,' + p.o + ')'; fxc.fill(); }
    else if (FX === 'petals') { fxc.translate(p.x, p.y); fxc.rotate(p.rot); fxc.fillStyle = 'rgba(255,168,200,' + p.o + ')'; fxc.beginPath(); fxc.ellipse(0, 0, p.r, p.r * .55, 0, 0, 6.28); fxc.fill(); fxc.setTransform(1,0,0,1,0,0); }
    else if (FX === 'hearts') { fxc.font = p.r * 2.6 + 'px serif'; fxc.fillStyle = 'rgba(255,107,149,' + p.o + ')'; fxc.fillText('♥', p.x, p.y); }
    else if (FX === 'lanterns') { const g = fxc.createRadialGradient(p.x, p.y, 1, p.x, p.y, p.r); g.addColorStop(0, 'rgba(255,176,32,' + p.o + ')'); g.addColorStop(1, 'rgba(255,80,40,' + p.o * .5 + ')'); fxc.fillStyle = g; fxc.beginPath(); fxc.arc(p.x, p.y, p.r, 0, 6.28); fxc.fill(); fxc.fillStyle = 'rgba(255,220,120,' + p.o + ')'; fxc.fillRect(p.x - p.r * .45, p.y - p.r * 1.15, p.r * .9, 3); }
    else if (FX === 'balloons') { fxc.fillStyle = 'hsla(' + p.hue + ',85%,65%,' + p.o + ')'; fxc.beginPath(); fxc.ellipse(p.x, p.y, p.r * .8, p.r, 0, 0, 6.28); fxc.fill(); fxc.strokeStyle = 'rgba(255,255,255,.35)'; fxc.beginPath(); fxc.moveTo(p.x, p.y + p.r); fxc.quadraticCurveTo(p.x + 4, p.y + p.r + 8, p.x, p.y + p.r + 15); fxc.stroke(); }
    fxc.restore();
  }
  requestAnimationFrame(fxTick);
})(0);
/* LRC */
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
let glrc = [], glrcOn = -1;
/* 今日盲盒 */
const todaySongs = SONGS.filter(s => s.today);
let revealed = false, opening = false;
try { revealed = localStorage.getItem('kgreveal_' + '__TODAY__') === '1'; } catch(e){}
/* 开盒前：今日歌不算进"已解锁"，也不出现在任何列表里（不剧透） */
function hiddenTodayCount(){ return revealed ? 0 : todaySongs.filter(s => s.locked).length; }
function refreshUnlockedCount(){
  const el = document.getElementById('unlockedN');
  if (el) el.textContent = Math.max(0, unlocked.length - hiddenTodayCount());
}
refreshUnlockedCount();
function reveal(){
  if (opening) return;  // 仪式进行中防连点
  if (revealed) { playSong(SONGS.indexOf(todaySongs[0])); return; }
  revealed = true; opening = true;
  try { localStorage.setItem('kgreveal_' + '__TODAY__', '1'); } catch(e){}
  fetch('/api/g-open', {method:'POST', headers:{'Content-Type':'application/json'}, body: '{}'}).catch(() => {});
  document.body.classList.add('ceremony');   // 仪式期间暂停毛玻璃，老 iPhone 不卡
  const g = document.getElementById('gift');
  /* 仪式感三段式：摇一摇 → 开盒大爆彩带 → 揭晓卡 3D 翻牌（不自动播放，想听自己点） */
  g.classList.add('shaking');
  for (let i = 0; i < 8; i++) {
    const c = document.createElement('span');
    c.className = 'confetti';
    c.textContent = ['✨','💫','⭐'][i % 3];
    c.style.setProperty('--dx', (Math.random()*180-90) + 'px');
    c.style.setProperty('--dy', (Math.random()*-120-30) + 'px');
    g.appendChild(c);
  }
  setTimeout(() => {
    g.classList.remove('shaking');
    g.classList.add('opened');
    const CONF_N = matchMedia('(max-width:700px)').matches ? 14 : 30;  // 老手机减量，防卡顿
    for (let i = 0; i < CONF_N; i++) {
      const c = document.createElement('span');
      c.className = 'confetti big';
      c.textContent = ['🎉','✨','🎵','💛','🎈','🎊','🥳'][i % 7];
      c.style.setProperty('--dx', (Math.random()*440-220) + 'px');
      c.style.setProperty('--dy', (Math.random()*-320-60) + 'px');
      c.style.animationDuration = (0.9 + Math.random()*0.7) + 's';
      g.appendChild(c);
    }
  }, 850);
  setTimeout(() => {
    showRevealed();
    opening = false;
  }, 1350);
}
/* 已揭晓状态（本地开过 或 服务端记过——换浏览器也不会再开一遍） */
function showRevealed(){
  document.body.classList.remove('ceremony');
  const g = document.getElementById('gift');
  if (g) g.style.display = 'none';
  const ob = document.getElementById('openBtn');
  if (ob) ob.style.display = 'none';
  const area = document.getElementById('revArea');
  area.classList.add('show');
  const rt = document.getElementById('revTitle');
  if (rt && todaySongs.length) { rt.innerHTML = '🎉 今日揭晓：《' + todaySongs[0].title + '》'; rt.classList.add('show'); }
  todaySongs.forEach((s, k) => {
    if (area.querySelector('[data-reveal="' + s.uk + '"]')) return;
    const card = document.createElement('div');
    card.className = 'rev-card';
    card.dataset.reveal = s.uk;
    const playIt = `playSong(SONGS.indexOf(SONGS.find(x => x.uk === '${s.uk}')))`;
    card.innerHTML = `<img src="${s.cover || ''}" alt="" onclick="${playIt}" title="点封面播放">` +
      `<div class="rt">${k > 0 ? '🎁 加更 · ' : ''}${s.title}</div>` +
      `<div class="rs">原唱：${s.singer ? s.singer + '《' + s.song + '》' : s.song} · ${s.time}</div>` +
      `<button class="rev-play" onclick="${playIt}">▶&nbsp; 播放</button>`;
    area.appendChild(card);
  });
  mountTodayCards();
  unlockCardUi();
  refreshUnlockedCount();
}
/* 开盒后才把今日歌挂进歌柜（开盒前完全不渲染，避免剧透） */
function mountTodayCards(){
  todaySongs.slice().reverse().forEach(s => {
    if (document.querySelector('.item[data-uk="' + s.uk + '"]')) return;
    const d = document.createElement('div');
    d.className = 'item';
    d.dataset.uk = s.uk;
    d.innerHTML = `<img src="${s.cover || ''}" alt="" loading="lazy">` +
      (s.today ? '<div class="tag">今日</div>' : '') +
      (s.festival ? '<div class="ftag">🧧</div>' : '') +
      `<div class="nm">${s.seq}. ${s.title}</div><div class="dt">${s.time}</div>` +
      (dateMap[s.uk] ? `<div class="ud">📅 ${dateMap[s.uk].date} 掉落${dateMap[s.uk].festival ? ' · ' + dateMap[s.uk].festival : ''}</div>` : '');
    d.onclick = () => playSong(SONGS.indexOf(s));
    cab.prepend(d);
  });
}
function unlockCardUi(){
  todaySongs.forEach(s => {
    const el = document.querySelector('.item[data-uk="' + s.uk + '"]');
    if (el && el.classList.contains('locked')) {
      el.classList.remove('locked');
      el.querySelector('img').src = s.cover || '';
      el.querySelector('img').style.filter = '';
      const q = el.querySelector('.q'); if (q) q.remove();
      el.querySelector('.nm').textContent = s.seq + '. ' + s.title;
    }
  });
}
/* 倒计时 */
function tickCd(){
  const now = new Date();
  const mid = new Date(now); mid.setHours(24,0,0,0);
  const d = mid - now;
  const h = String(d/3600000|0).padStart(2,'0'), m = String((d/60000|0)%60).padStart(2,'0'), s = String((d/1000|0)%60).padStart(2,'0');
  document.getElementById('cd').textContent = h + ':' + m + ':' + s;
}
tickCd(); setInterval(tickCd, 1000);
/* 歌柜（已解锁在前） */
const cab = document.getElementById('cabinet');
const lockedPool = [];
/* 最新收录 */
const lg = document.getElementById('latestGrid');
const recent = SONGS.filter(s => !s.locked && !(s.today && !revealed) && dateMap[s.uk])
  .sort((a, b) => (dateMap[b.uk] ? dateMap[b.uk].date : '').localeCompare(dateMap[a.uk] ? dateMap[a.uk].date : ''))
  .slice(0, 3);
recent.forEach(s => {
  const d = document.createElement('div');
  d.className = 'lcard';
  d.innerHTML = `<img src="${s.cover || ''}" loading="lazy"><div class="lt">${s.title}</div><div class="ls">📅 ${dateMap[s.uk] ? dateMap[s.uk].date : ''}</div>`;
  d.onclick = () => playSong(SONGS.indexOf(s));
  lg.appendChild(d);
});

SONGS.forEach(s => {
  if (s.today && s.locked) return;  // 今日歌开盒前完全不渲染（不占歌柜、不进神秘卡）
  const hide = s.locked;  const d = document.createElement('div');
  d.className = 'item' + (hide ? ' locked' : '');
  d.dataset.uk = s.uk;
  d.innerHTML = `<img src="${hide ? '' : (s.cover || '')}" alt="" ${hide ? '' : 'loading="lazy"'}>` +
    (hide ? '<div class="q">❓</div>' : '') +
    (s.today ? '<div class="tag">今日</div>' : (hide ? '<div class="tag">🔒</div>' : '')) +
    (s.festival ? '<div class="ftag">🧧</div>' : '') +
    `<div class="nm">${hide ? '？？？' : s.seq + '. ' + s.title}</div><div class="dt">${s.time}</div>` +
    (!hide && dateMap[s.uk] ? `<div class="ud">📅 ${dateMap[s.uk].date} 掉落${dateMap[s.uk].festival ? ' · ' + dateMap[s.uk].festival : ''}</div>` : '');
  d.onclick = () => {
    if (hide) { d.classList.add('shake'); setTimeout(()=>d.classList.remove('shake'), 450); return; }
    playSong(SONGS.indexOf(s));
  };
  // 歌柜折叠：已解锁 + 前 8 张神秘卡直接展示，其余收进「展开全部」
  if (hide && !(s.today)) {
    lockedPool.push(d);
  } else {
    cab.appendChild(d);
  }
});
if (lockedPool.length > 8) {
  const extra = lockedPool.slice(8);
  const box = document.createElement('div');
  box.className = 'cabinet';
  box.style.display = 'none';
  extra.forEach(el => box.appendChild(el));
  const more = document.createElement('button');
  more.className = 'more-btn';
  more.textContent = `🔐 还有 ${extra.length} 首神秘卡，点开看看 ▾`;
  more.onclick = () => {
    const open = box.style.display !== 'none';
    box.style.display = open ? 'none' : 'grid';
    more.textContent = open ? `🔐 还有 ${extra.length} 首神秘卡，点开看看 ▾` : '收起神秘卡 ▴';
  };
  cab.insertAdjacentElement('afterend', more);
  more.insertAdjacentElement('afterend', box);
} else {
  lockedPool.slice(0, 8).forEach(el => cab.appendChild(el));
}
/* 掉落日历（今天这条开盒前先保密） */
const his = document.getElementById('history');
const hisList = HISTORY.filter(h => revealed || !todaySongs.some(t => t.uk === h.uk));
if (!hisList.length) his.innerHTML = '<div class="empty">盲盒从今天开始掉落 🎉 明天再来开新的！</div>';
hisList.forEach(h => {
  const d = document.createElement('div');
  d.className = 'hrow';
  d.innerHTML = `<span class="d">${h.date.slice(5)}</span><span class="t">《${h.title}》${h.festival ? ' 🧧' : ''}</span><span class="s">原唱 ${h.singer || '—'}</span>`;
  his.appendChild(d);
});
if (hisList.length < HISTORY.length) {
  const d = document.createElement('div');
  d.className = 'hrow';
  d.innerHTML = `<span class="d">${'__TODAY__'.slice(5)}</span><span class="t">开盒后揭晓 🔒</span><span class="s">今天的歌还在盒子里</span>`;
  his.appendChild(d);
}
/* 播放条 */
let listenTimer = null, listenSec = 0, countedUk = '';
/* 收听满 30s 静默计入统计，不再弹出任何提示 */
function startListenTimer(s){
  stopListenTimer();
  listenSec = 0;
  countedUk = s.uk;
  listenTimer = setInterval(() => {
    listenSec++;
    if (listenSec >= 30 && s.uk === countedUk) {
      stopListenTimer();
      fetch('/api/g-play', {method:'POST', headers:{'Content-Type':'application/json'},
        body: JSON.stringify({uk: s.uk})}).then(()=>refreshPlayBadges()).catch(()=>{});
    }
  }, 1000);
}
function stopListenTimer(){
  if (listenTimer) { clearInterval(listenTimer); listenTimer = null; }
}
function playSong(i){
  const s = SONGS[i]; if (!s || s.locked) return;
  if (cur === i && !au.paused) { au.pause(); document.getElementById('barPlay').textContent = '▶'; return; }
  stopListenTimer();
  cur = i;
  au.src = s.audio;
  glrc = []; glrcOn = -1;
  const bl = document.getElementById('barLyric'); bl.classList.remove('show'); bl.textContent = '';
  if (s.lrc) fetch(s.lrc).then(r => r.ok ? r.text() : '').then(t => { if (SONGS[cur] === s && t) { glrc = parseLrc(t); } }).catch(() => {});
  document.getElementById('barCover').src = s.cover || '';
  document.getElementById('barTitle').textContent = s.seq + '. ' + s.title;
  document.getElementById('barSub').textContent = '原唱：' + (s.singer ? s.singer + '《' + s.song + '》' : s.song) + ' · ' + s.time;
  document.getElementById('bar').classList.add('show');
  paintProgress();
  document.getElementById('barPlay').textContent = '⏸';  // 点击瞬间就切图标，不等音频事件（iOS 下事件可能延迟）
  au.play().catch(() => { document.getElementById('barPlay').textContent = '▶'; });  // 点选即播；被浏览器拦截则回退
}

au.onplay = () => { document.getElementById('barPlay').textContent = '⏸'; paintProgress();
  try { const s = SONGS[cur]; if (s && !s.locked) startListenTimer(s); } catch(e){} };
au.onpause = () => { stopListenTimer();
  document.getElementById('barPlay').textContent = '▶'; };
au.onloadedmetadata = () => paintProgress();
au.ontimeupdate = () => {
  paintProgress();
  if (!glrc.length) return;
  let i = -1;
  const tt = au.currentTime + lyricOffset;
  for (let k = 0; k < glrc.length; k++) if (glrc[k].t <= tt) i = k;
  if (i === glrcOn) return;
  glrcOn = i;
  const bl = document.getElementById('barLyric');
  bl.textContent = glrc[i] ? (glrc[i].text || '♪') : '♪';
  bl.classList.add('show');
};
function barToggle(){ if (!au.src) { if (unlocked.length) playSong(SONGS.indexOf(unlocked[0])); return; }
  if (au.paused) { au.play(); document.getElementById('barPlay').textContent = '⏸'; }
  else { au.pause(); document.getElementById('barPlay').textContent = '▶'; } }
/* 换歌：mode = 'step'（手动上下首）/ 'auto'（一曲播完，受播放模式影响） */
function barStep(step, mode){
  if (!unlocked.length) return;
  let i = unlocked.indexOf(SONGS[cur]);
  if (mode === 'auto' && playMode === 'rand' && unlocked.length > 1) {
    let n = i;
    while (n === i) n = Math.floor(Math.random() * unlocked.length);
    playSong(SONGS.indexOf(unlocked[n]));
    return;
  }
  if (i < 0) i = 0; else i = (i + step + unlocked.length) % unlocked.length;
  playSong(SONGS.indexOf(unlocked[i]));
}
function barPrev(){ barStep(-1, 'step'); }
function barNext(){ barStep(1, 'step'); }
au.onended = () => {
  stopListenTimer();
  if (playMode === 'loop') {  // 单曲循环：原地重播，不换歌
    au.currentTime = 0;
    au.play().catch(() => { document.getElementById('barPlay').textContent = '▶'; });
    return;
  }
  barStep(1, 'auto');
};
/* 已揭晓状态初始化 */
if (revealed && todaySongs.length) showRevealed();

/* ============ 留言板 + 播放统计（播放次数按访客各算各的） ============ */
let playsMap = {}, meFilled = false;
function loadComments(){
  fetch('/api/g-data').then(r => r.json()).then(j => {
    playsMap = j.plays || {};
    renderComments(j.comments || {});
    refreshPlayBadges();
    applyBonus(j.bonus || 0);
    handleNotices(j.msgs || []);
    /* 换浏览器/清缓存：服务端记着今天开过盒，直接进已揭晓状态，不能重开 */
    if (j.opened_today && !revealed && !opening) {
      revealed = true;
      try { localStorage.setItem('kgreveal_' + '__TODAY__', '1'); } catch(e){}
      showRevealed();
    }
    /* 留言昵称默认当前身份（登录账号 / 匿名ID），用户改过就不动 */
    if (!meFilled && j.me) {
      const n = document.getElementById('cName');
      if (n && !n.value) n.value = j.me;
      meFilled = true;
    }
  }).catch(() => {});
}
/* 机主加赠的开盒次数 */
function applyBonus(n){
  const btn = document.getElementById('openBtn');
  if (!btn || !n) return;
  btn.textContent = '开启今日盲盒 · 机主加赠 ' + n + ' 次';
}
/* 机主发来的消息：好看的猫弹窗，一条一条弹 */
let seenNotices = {};
try { seenNotices = JSON.parse(localStorage.getItem('kgseen') || '{}'); } catch(e){}
function handleNotices(msgs){
  if (!msgs.length) return;
  const fresh = msgs.filter(m => !seenNotices[m.at + '|' + (m.text || '')]);
  fetch('/api/g-seen', {method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({keys: msgs.map(m => m.at + '|' + (m.text || ''))})}).catch(() => {});
  fresh.forEach((m, i) => {
    seenNotices[m.at + '|' + (m.text || '')] = 1;
    const gift = m.gift ? '🎁 机主给你加了 <b>' + m.gift + '</b> 次开盒机会' : '';
    const text = (m.text || '').replace(/</g, '&lt;');
    setTimeout(() => showCat((gift && text ? gift + '<br>' : gift) + text, '机主留言'), 700 + i * 1400);
  });
  try { localStorage.setItem('kgseen', JSON.stringify(seenNotices)); } catch(e){}
}
function renderComments(comments){
  const list = document.getElementById('cList');
  if (!list) return;
  const all = [];
  for (const uk in comments) { for (const c of comments[uk]) all.push({...c, uk}); }
  all.sort((a, b) => b.time.localeCompare(a.time));
  const esc = t => String(t || '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  list.innerHTML = all.length ? all.map(c =>
    `<div class="crow"><div class="cn">${esc(c.name)}</div><div class="ct">${esc(c.text)}</div>` +
    (c.reply ? `<div class="creply"><b>🐱 机主回复</b>（${esc(c.reply.time)}）：${esc(c.reply.text)}</div>` : '') +
    `<div class="cd">${esc(c.time)}</div></div>`).join('')
    : '<div class="cempty">还没有留言，来抢沙发！</div>';
  checkReplies(all);
}
/* 机主的新回复 → 猫弹窗（看过就不再弹） */
let seenReplies = {};
try { seenReplies = JSON.parse(localStorage.getItem('kgreplyseen') || '{}'); } catch(e){}
function checkReplies(all){
  const fresh = all.filter(c => c.reply && !seenReplies[c.uk + '|' + (c.time || '')]);
  if (!fresh.length) return;
  fetch('/api/g-seen-reply', {method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({keys: fresh.map(c => c.uk + '|' + (c.time || ''))})}).catch(() => {});
  fresh.forEach((c, i) => {
    seenReplies[c.uk + '|' + (c.time || '')] = 1;
    setTimeout(() => showCat(String(c.reply.text || '').replace(/</g, '&lt;'), '机主回复了你的留言'), 900 + i * 1400);
  });
  try { localStorage.setItem('kgreplyseen', JSON.stringify(seenReplies)); } catch(e){}
}
function submitComment(){
  const text = document.getElementById('cText').value.trim();
  if (!text) { document.getElementById('cText').focus(); return; }
  const name = document.getElementById('cName').value.trim() || '匿名';
  const s = SONGS[cur] || SONGS[0];
  fetch('/api/g-comment', {method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({uk: s.uk || '', name, text})})
  .then(r => r.json()).then(j => { if (j.ok) { document.getElementById('cText').value = ''; loadComments(); } else alert(j.error); })
  .catch(() => alert('网络错误'));
}
function refreshPlayBadges(){
  document.querySelectorAll('.item').forEach(el => {
    const uk = el.dataset.uk;
    const pc = playsMap[uk] || 0;
    const dt = el.querySelector('.dt');
    if (dt && pc) { dt.textContent = dt.textContent.split(' · ')[0] + ' · 我听了 ' + pc + ' 次'; }
  });
}
loadComments();
/* 留言/播放数定时同步：别人新留的言不用刷新页面也能看到 */
setInterval(loadComments, 60000);
document.addEventListener('visibilitychange', () => { if (!document.hidden) loadComments(); });
/* ============ PWA：一键安装到桌面 ============ */
let installEvt = null, installMode = 'menu';
addEventListener('beforeinstallprompt', e => {
  e.preventDefault(); installEvt = e; installMode = 'prompt';
  const b = document.getElementById('pwaBtn'); if (b) b.style.display = 'inline-block';
});
addEventListener('appinstalled', () => {
  installEvt = null;
  const b = document.getElementById('pwaBtn');
  if (b) { b.textContent = '✅ 已装到桌面'; setTimeout(() => { b.style.display = 'none'; }, 2600); }
});
function closeSheet(id){ const el = document.getElementById(id); if (el) el.classList.remove('show'); }
function openSheet(id){ const el = document.getElementById(id); if (el) el.classList.add('show'); }
function sheetHtml(){
  const ua = navigator.userAgent;
  const isiOS = /iphone|ipad|ipod/i.test(ua);
  const inWx = /MicroMessenger/i.test(ua);
  const https = location.protocol === 'https:' || location.hostname === 'localhost' || location.hostname === '127.0.0.1';
  if (inWx) return ['⚠️ 微信里装不了', '<p>请点右上角「···」→「在浏览器打开」，再用浏览器安装。</p>', '知道了', false];
  if (isiOS) return ['📲 装到 iPhone / iPad 桌面',
    '<ol><li>用 <b>Safari</b> 打开本页</li><li>点底部「分享」（方框＋向上箭头）</li><li>选「添加到主屏幕」→「添加」</li></ol>',
    '我知道了', false];
  if (installEvt) return ['📲 装到桌面', '<p>点下面的按钮，浏览器会直接弹出安装确认。</p>', '立即安装', true];
  return ['📲 装到桌面',
    (https ? '<p>点浏览器右上角菜单（⋮），选「安装应用 / 添加到主屏幕」。</p>'
           : '<p>当前是 http 局域网地址，浏览器不允许一键安装。两种办法：</p>' +
             '<ol><li>用 <b>https 域名</b>（反代开启证书）打开本页，就能一键安装</li>' +
             '<li>或点浏览器菜单（⋮）→「添加到主屏幕」，效果一样</li></ol>'),
    '知道了', false];
}
function installPwa(){
  if (installEvt) { installEvt.prompt(); installEvt.userChoice && installEvt.userChoice.then(() => { installEvt = null; }).catch(() => {}); return; }
  const [title, body, label, canPrompt] = sheetHtml();
  document.getElementById('sheetBody').innerHTML = body;
  document.getElementById('sheetTitle').textContent = title;
  const go = document.getElementById('sheetGo');
  go.textContent = label;
  go.dataset.mode = canPrompt ? 'prompt' : 'ok';
  openSheet('installSheet');
}
function sheetAction(){
  const go = document.getElementById('sheetGo');
  if (go.dataset.mode === 'prompt' && installEvt) {
    installEvt.prompt();
    installEvt.userChoice && installEvt.userChoice.then(() => { installEvt = null; }).catch(() => {});
  }
  closeSheet('installSheet');
}
/* 注册 Service Worker：有了它浏览器才把本页当成"可安装的 App" */
if ('serviceWorker' in navigator) {
  addEventListener('load', () => { navigator.serviceWorker.register('/g/sw.js', {scope: '/g/'}).catch(() => {}); });
}
/* 已安装（独立窗口运行）则不显示安装按钮 */
try {
  const pb = document.getElementById('pwaBtn');
  if (pb && (matchMedia('(display-mode: standalone)').matches || navigator.standalone)) pb.style.display = 'none';
  else if (pb) pb.style.display = 'inline-block';
} catch(e){}

/* ============ 播放进度（播放栏顶部细线，点一下跳转） ============ */
const pbarEl = document.getElementById('pbar'), pfillEl = document.getElementById('pfill'), barTimeEl = document.getElementById('barTime');
function fmtT(s){ s = Math.max(0, Math.floor(s || 0)); return Math.floor(s / 60) + ':' + String(s % 60).padStart(2, '0'); }
function seekTo(e){
  if (!au.duration) return;
  if (!pbarEl) return;
  const r = pbarEl.getBoundingClientRect();
  const cx = (e.touches && e.touches[0]) ? e.touches[0].clientX : e.clientX;
  au.currentTime = Math.max(0, Math.min(1, (cx - r.left) / r.width)) * au.duration;
  paintProgress();
}
function paintProgress(){
  const d = au.duration || 0, t = au.currentTime || 0;
  if (pfillEl) pfillEl.style.width = (d ? (t / d * 100) : 0) + '%';
  if (barTimeEl) barTimeEl.textContent = fmtT(t) + ' / ' + fmtT(d);
}
if (pbarEl) {
  pbarEl.addEventListener('click', seekTo);
  pbarEl.addEventListener('touchstart', e => { seekTo(e); }, {passive:true});
}
/* ============ 播放模式：顺序 🔁 / 单曲 🔂 / 随机 🔀（单按钮循环，省地方） ============ */
const MODES = [
  {m:'seq',  icon:'🔁', tip:'顺序播放'},
  {m:'loop', icon:'🔂', tip:'单曲循环'},
  {m:'rand', icon:'🔀', tip:'随机播放'}
];
let playMode = 'seq';
try { playMode = localStorage.getItem('kgmode') || 'seq'; } catch(e){}
function paintMode(){
  const btn = document.getElementById('modeBtn');
  if (!btn) return;
  const it = MODES.find(x => x.m === playMode) || MODES[0];
  btn.textContent = it.icon;
  btn.title = '播放模式：' + it.tip;
  btn.classList.toggle('act', playMode !== 'seq');
}
function setMode(m){
  playMode = (MODES.some(x => x.m === m) ? m : 'seq');
  try { localStorage.setItem('kgmode', playMode); } catch(e){}
  paintMode();
}
function cycleMode(){
  const i = MODES.findIndex(x => x.m === playMode);
  setMode(MODES[(i + 1) % MODES.length].m);
  const it = MODES.find(x => x.m === playMode);
  if (typeof showCat === 'function') showCat('播放模式已切换：' + it.tip + ' ' + it.icon);
}
paintMode();
/* ============ 猫提示（轻反馈，不打断） ============ */
function showCat(msg, from){
  let stack = document.getElementById('catStack');
  if (!stack) { stack = document.createElement('div'); stack.id = 'catStack'; document.body.appendChild(stack); }
  const n = document.createElement('div'); n.className = 'cat-notify';
  n.innerHTML = '<div class="cat-close" onclick="this.parentElement.remove()">✕</div>' +
    '<div class="cat-head"><div class="cat-face"></div>' +
    '<div class="cat-title">' + (from || '小猫提醒') + '</div></div>' +
    '<div class="cat-msg">' + msg + '</div>';
  stack.appendChild(n);
  setTimeout(() => { if (n.parentElement) n.remove(); }, 7000);  // 与底部倒计时条同步
}
</script></body></html>"""



# ---------------------------------------------------------------- 节日彩蛋
FESTIVALS = [
    ('01-01', '元旦', 'newyear', 'fireworks', True, '🧨'),
    ('02-14', '情人节', 'valentine', 'hearts', False, '💝'),
    ('03-08', '妇女节', 'spring', 'petals', False, '🌸'),
    ('05-01', '劳动节', 'labour', 'fireworks', False, '🎆'),
    ('06-01', '儿童节', 'children', 'balloons', False, '🎈'),
    ('10-01', '国庆节', 'national', 'fireworks', True, '🎆'),
    ('11-11', '双十一', 'shopping', 'balloons', False, '🛍️'),
    ('12-24', '平安夜', 'christmas', 'snow', True, '🎄'),
    ('12-25', '圣诞节', 'christmas', 'snow', True, '🎄'),
    ('12-31', '跨年夜', 'newyear', 'fireworks', True, '🧨'),
    ('2026-02-16', '除夕', 'cny', 'fireworks', True, '🧧'),
    ('2026-02-17', '春节', 'cny', 'fireworks', True, '🧧'),
    ('2026-03-03', '元宵节', 'lantern', 'lanterns', True, '🏮'),
    ('2026-06-19', '端午节', 'duanwu', 'petals', False, '🚣'),
    ('2026-08-19', '七夕', 'qixi', 'hearts', True, '💝'),
    ('2026-09-25', '中秋节', 'midautumn', 'lanterns', True, '🥮'),
    ('2027-02-05', '除夕', 'cny', 'fireworks', True, '🧧'),
    ('2027-02-06', '春节', 'cny', 'fireworks', True, '🧧'),
    ('2027-02-20', '元宵节', 'lantern', 'lanterns', True, '🏮'),
    ('2027-06-09', '端午节', 'duanwu', 'petals', False, '🚣'),
    ('2027-08-08', '七夕', 'qixi', 'hearts', True, '💝'),
    ('2027-09-15', '中秋节', 'midautumn', 'lanterns', True, '🥮'),
    ('2028-01-25', '除夕', 'cny', 'fireworks', True, '🧧'),
    ('2028-01-26', '春节', 'cny', 'fireworks', True, '🧧'),
    ('2028-02-09', '元宵节', 'lantern', 'lanterns', True, '🏮'),
    ('2028-05-28', '端午节', 'duanwu', 'petals', False, '🚣'),
    ('2028-08-26', '七夕', 'qixi', 'hearts', True, '💝'),
    ('2028-10-03', '中秋节', 'midautumn', 'lanterns', True, '🥮'),
]


def get_festival(date_str):
    """date_str: YYYY-MM-DD。命中节日返回 {name, theme, effect, double, gift}，否则 None。"""
    md = date_str[5:] if len(date_str) >= 10 else ''
    for f in FESTIVALS:
        if f[0] == date_str or f[0] == md:
            return {'name': f[1], 'theme': f[2], 'effect': f[3], 'double': f[4], 'gift': f[5]}
    return None


def render_guest_page(entries, profile, unlocked_keys, history, today_keys, today_date, festival=None):
    """生成朋友盲盒页（kg_app 动态调用）。unlocked_keys 为已解锁歌曲键集合。"""
    def esc(s):
        return html_mod.escape(str(s if s is not None else ''))

    def uk(e):
        return e.get('ugcid') or ('s' + str(e.get('seq')))

    data = []
    today = None
    for e in sorted(entries, key=lambda x: x['seq']):
        if not e.get('audio'):
            continue
        k = uk(e)
        locked = k not in unlocked_keys
        is_today = (k in today_keys)
        if is_today:
            today = e
        data.append({
            'uk': k, 'seq': e['seq'], 'title': e.get('title') or '未命名',
            'song': e.get('song_name') or '', 'singer': e.get('singer_name') or '',
            'time': (e.get('time') or '')[:10],
            'cover': '' if locked else '/g/' + (e.get('cover') or ''),
            'audio': '' if locked else '/g/' + (e.get('audio') or ''),
            'lrc': '' if locked else '/g/' + (e.get('lrc') or ''),
            'locked': locked, 'today': is_today,
            'festival': e.get('_fest') or '',
        })
    hist = [{'date': h['date'], 'title': h['title'], 'singer': h.get('singer') or ''} for h in history]
    nick = esc(profile.get('nickname', ''))
    unlocked_n = sum(1 for d in data if not d['locked'])
    fest_json = json.dumps(festival, ensure_ascii=False) if festival else 'null'
    fest_class = ('fest-' + festival['theme']) if festival else ''
    fest_emoji = festival['gift'] if festival else '🎁'
    doc = (GUEST_TEMPLATE
           .replace('__DATA__', json.dumps(data, ensure_ascii=False).replace('</', '<\\/'))
           .replace('__HISTORY__', json.dumps(hist, ensure_ascii=False).replace('</', '<\\/'))
           .replace('__NICK__', nick)
           .replace('__UNLOCKED__', str(unlocked_n))
           .replace('__TOTAL__', str(len(data)))
           .replace('__TODAY__', esc(today_date))
           .replace('__FEST__', fest_json)
           .replace('__FEST_CLASS__', fest_class)
           .replace('__FEST_EMOJI__', fest_emoji)
           .replace('__FEST_TITLE__', (esc(festival['name']) + ' · ') if festival else ''))
    return doc



def save_author_preset(uid, nickname, out_dir):
    """记录备份过的作者（uid→输出目录），供下次一键增量更新。"""
    try:
        fp = os.path.join(os.getcwd(), 'authors.json')
        data = {}
        if os.path.exists(fp):
            try:
                with open(fp, encoding='utf-8') as f:
                    data = json.load(f)
            except Exception:
                data = {}
        data[uid] = {'uid': uid, 'nickname': nickname or uid,
                     'dir': os.path.abspath(out_dir), 'last': ts2str(time.time())}
        with open(fp, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
    except Exception:
        pass

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
