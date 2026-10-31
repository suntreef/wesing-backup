#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""core.py 纯函数单元测试（不依赖网络）。运行：python -m unittest discover -s tests -v"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import calc_gtk, extract_json_after, https, parse_uid, safe_name, ts2str


class TestParseUid(unittest.TestCase):
    def test_from_personal_url(self):
        self.assertEqual(parse_uid('https://kg.qq.com/node/personal?uid=639f9b862028378b37'),
                         '639f9b862028378b37')

    def test_from_node_url(self):
        self.assertEqual(
            parse_uid('https://node.kg.qq.com/personal.html?uid=63d9f5ec8462318c31&tuin=1'),
            '63d9f5ec8462318c31')

    def test_bare_uid(self):
        self.assertEqual(parse_uid(' 639f9b862028378b37 '), '639f9b862028378b37')

    def test_uid_inside_text(self):
        self.assertEqual(parse_uid('我的主页 https://kg.qq.com/node/personal?uid=abcDEF0123456789ab 快看'),
                         'abcDEF0123456789ab')

    def test_invalid(self):
        with self.assertRaises(ValueError):
            parse_uid('https://kg.qq.com/hello')
        with self.assertRaises(ValueError):
            parse_uid('')


class TestSafeName(unittest.TestCase):
    def test_removes_forbidden_chars(self):
        self.assertEqual(safe_name('a/b\\c:d*e?f"g<h>i|j'), 'a_b_c_d_e_f_g_h_i_j')

    def test_removes_quotes_and_newlines(self):
        self.assertEqual(safe_name("it's\nfine\tagain"), 'it_s_fine_again')

    def test_maxlen(self):
        self.assertEqual(len(safe_name('长' * 200, maxlen=10)), 10)

    def test_empty(self):
        self.assertEqual(safe_name('   '), 'untitled')
        self.assertEqual(safe_name('///'), '___')  # 替换为下划线而非清空


class TestExtractJson(unittest.TestCase):
    def test_simple(self):
        self.assertEqual(extract_json_after('var x = {"a":1};', 'var x ='), '{"a":1}')

    def test_nested_and_string_braces(self):
        text = 'window.__FETCH_RES__ = {"a":{"b":"包含}和{的字符串"},"c":[1,2]};'
        import json
        self.assertEqual(json.loads(extract_json_after(text, '__FETCH_RES__')),
                         {'a': {'b': '包含}和{的字符串'}, 'c': [1, 2]})

    def test_missing_marker(self):
        self.assertIsNone(extract_json_after('no marker here', 'zzz'))


class TestGtk(unittest.TestCase):
    def test_empty_is_default(self):
        self.assertEqual(calc_gtk(''), 5381)
        self.assertEqual(calc_gtk(None), 5381)

    def test_known_value(self):
        # 与 QQ 通用算法一致：对 'abc' 手算校验
        h = 5381
        for ch in 'abc':
            h += (h << 5) + ord(ch)
        self.assertEqual(calc_gtk('abc'), h & 0x7FFFFFFF)

    def test_range(self):
        self.assertTrue(0 <= calc_gtk('someskeyvalue') <= 0x7FFFFFFF)


class TestMisc(unittest.TestCase):
    def test_ts2str(self):
        self.assertEqual(ts2str(1571545506), '2019-10-20 12:25:06')  # 本地时区
        self.assertEqual(ts2str('not-a-time'), '')
        self.assertEqual(ts2str(None), '')

    def test_https(self):
        self.assertEqual(https('http://x.com/a'), 'https://x.com/a')
        self.assertEqual(https('https://x.com/a'), 'https://x.com/a')
        self.assertEqual(https('ftp://x'), 'ftp://x')


if __name__ == '__main__':
    unittest.main(verbosity=2)
