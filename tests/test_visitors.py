#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""访客独立盲盒：每人一套掉落序列 + 自己的播放次数（v1.11.7 起）。"""

import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import kg_app as app  # noqa: E402


class VisitorCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='kg-vis-')
        self.old_cwd = os.getcwd()
        os.chdir(self.tmp)
        self.share = os.path.join(self.tmp, 'songs')
        os.makedirs(self.share)
        works = {}
        for i in range(30):
            k = f'k{i:02d}'
            works[k] = {'seq': i, 'ugcid': k, 'title': f'第{i}首',
                        'singer_name': '歌手', 'audio': f'a{i}.m4a'}
        with open(os.path.join(self.share, 'index.json'), 'w', encoding='utf-8') as f:
            json.dump({'works': works, 'profile': {'nickname': '氧气'}}, f, ensure_ascii=False)
        with open(os.path.join(self.tmp, 'shares.json'), 'w', encoding='utf-8') as f:
            json.dump({'dir': self.share, 'since': ''}, f, ensure_ascii=False)
        self.set_day('2026-11-01')

    def tearDown(self):
        os.chdir(self.old_cwd)
        os.environ.pop('KG_FAKE_DATE', None)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def set_day(self, day):
        os.environ['KG_FAKE_DATE'] = day

    def seq(self, vid, days):
        """取某访客在连续 days 天里每天抽到的歌名。"""
        out = []
        for d in days:
            self.set_day(d)
            _e, _p, _u, hist, _tk, _td, _f = app.blindbox_state(vid)
            out.append(hist[-1]['title'])
        return out

    def days(self, n, start=1):
        return [f'2026-11-{start + i:02d}' for i in range(n)]


class TestDropSequence(VisitorCase):
    def test_same_visitor_is_stable(self):
        a1 = self.seq('u:alice', self.days(6))
        a2 = self.seq('u:alice', self.days(6))
        self.assertEqual(a1, a2)

    def test_visitors_get_different_sequences(self):
        a = self.seq('u:alice', self.days(10))
        b = self.seq('u:bob', self.days(10))
        self.assertNotEqual(a, b, '不同账号必须拿到不同的掉落序列')

    def test_cookie_visitors_also_differ(self):
        a = self.seq('v:0123456789abcdef', self.days(10))
        b = self.seq('v:fedcba9876543210', self.days(10))
        self.assertNotEqual(a, b)

    def test_new_visitor_starts_today(self):
        self.set_day('2026-11-01')
        app.blindbox_state('u:early')
        self.set_day('2026-11-05')
        _e, _p, unlocked, hist, _tk, _td, _f = app.blindbox_state('u:late')
        self.assertEqual(len(hist), 1, '新访客只看到自己的第一天，不继承别人的日历')
        self.assertEqual(hist[0]['date'], '2026-11-05')
        self.assertEqual(len(unlocked), 1)

    def test_legacy_start_is_inherited_once(self):
        with open(os.path.join(self.share, 'guest_start'), 'w', encoding='utf-8') as f:
            f.write('2026-10-28')
        self.set_day('2026-11-01')
        _e, _p, _u, hist, _tk, _td, _f = app.blindbox_state('v:0000000000000001')
        self.assertEqual(hist[0]['date'], '2026-10-28', '老数据的第一位访客继承旧日历')
        st, _ = app.visitor_state(self.share, 'v:0000000000000001')
        self.assertTrue(st.get('legacy'), '继承者沿用旧随机种子，抽到的歌不变')
        # 第二个人不再继承
        _e, _p, _u, hist2, _tk, _td, _f = app.blindbox_state('v:0000000000000002')
        self.assertEqual(hist2[0]['date'], '2026-11-01')

    def test_legacy_sequence_matches_old_global(self):
        """继承者（salt='') 的掉落应与旧版全局序列完全一致。"""
        with open(os.path.join(self.share, 'guest_start'), 'w', encoding='utf-8') as f:
            f.write('2026-11-01')
        self.set_day('2026-11-01')
        app.blindbox_state('v:0000000000000009')
        visitor_seq = self.seq('v:0000000000000009', self.days(4))
        global_seq = self.seq(None, self.days(4))
        self.assertEqual(visitor_seq, global_seq)


class TestPlays(VisitorCase):
    def test_counts_are_per_visitor(self):
        d = self.share
        app.add_guest_play(d, 'k01', 'u:alice')
        app.add_guest_play(d, 'k01', 'u:alice')
        app.add_guest_play(d, 'k01', 'u:bob')
        self.assertEqual(app.visitor_plays(d, 'u:alice'), {'k01': 2})
        self.assertEqual(app.visitor_plays(d, 'u:bob'), {'k01': 1})
        self.assertEqual(app.all_plays(d), {'k01': 3})
        self.assertEqual(app.guest_plays(d), {}, '不再写全局文件，避免互通')

    def test_admin_aggregates_legacy_counts(self):
        d = self.share
        with open(os.path.join(d, 'guest_plays.json'), 'w', encoding='utf-8') as f:
            json.dump({'k01': 5}, f)
        app.add_guest_play(d, 'k01', 'u:alice')
        self.assertEqual(app.all_plays(d), {'k01': 6})

    def test_state_file_survives_reload(self):
        d = self.share
        app.add_guest_play(d, 'k02', 'v:abcdefabcdef1234')
        # 重新读文件（模拟服务重启）
        data = app.visitors(d)
        self.assertIn('v:abcdefabcdef1234', data)
        self.assertEqual(data['v:abcdefabcdef1234']['plays'], {'k02': 1})


class TestIdentityFormat(unittest.TestCase):
    def test_regex_rejects_junk(self):
        self.assertTrue(app._VID_RE.match('0123456789abcdef'))
        self.assertTrue(app._VID_RE.match('u_abc-123'))
        for bad in ('', 'a', '带中文', 'x' * 80, 'a b', 'a;b'):
            self.assertFalse(app._VID_RE.match(bad), bad)

    def test_label(self):
        self.assertEqual(app.visitor_label('u:氧气'), '氧气')
        self.assertTrue(app.visitor_label('v:0123456789abcdef').startswith('匿名·'))


if __name__ == '__main__':
    unittest.main(verbosity=2)
