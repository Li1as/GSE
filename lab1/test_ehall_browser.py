import json
import tempfile
import unittest
from pathlib import Path

from ehall_browser import (DEFAULT_NAVIGATION_HOSTS, load_ehall_config,
                           navigation_host, normalize_url, page_identity, public_url,
                           restore_storage_state, save_storage_state)
from persistence import AppError


class EhallUrlTests(unittest.TestCase):
    def test_accepts_task_url_and_decodes_html_ampersand(self):
        value = normalize_url('https://ehallapp.nju.edu.cn/jwapp/sys/wdkb/index.do?a=1&amp;b=2#route')
        self.assertEqual(value, 'https://ehallapp.nju.edu.cn/jwapp/sys/wdkb/index.do?a=1&b=2#route')
        self.assertEqual(public_url(value), 'https://ehallapp.nju.edu.cn/jwapp/sys/wdkb/index.do')

    def test_preserves_real_amp_sec_parameter(self):
        value = normalize_url('https://ehallapp.nju.edu.cn/jwapp/sys/wdkb/index.do?t=1&amp_sec_version_=1#/xskcb')
        self.assertIn('&amp_sec_version_=1', value)
        self.assertTrue(value.endswith('#/xskcb'))

    def test_rejects_non_https_unknown_host_credentials_and_port(self):
        values = ('http://ehall.nju.edu.cn/', 'https://evil.test/',
                  'https://user:secret@ehall.nju.edu.cn/', 'https://ehall.nju.edu.cn:444/')
        for value in values:
            with self.subTest(value=value), self.assertRaises(AppError):
                normalize_url(value)

    def test_navigation_uses_exact_allowlist(self):
        self.assertEqual(navigation_host('https://authserver.nju.edu.cn/login', DEFAULT_NAVIGATION_HOSTS),
                         'authserver.nju.edu.cn')
        with self.assertRaises(AppError):
            navigation_host('https://authserver.nju.edu.cn.evil.test/login', DEFAULT_NAVIGATION_HOSTS)

    def test_identifies_timetable_without_clicking(self):
        value = page_identity('https://ehallapp.nju.edu.cn/jwapp/sys/wdkb/index.do',
                              '南京大学', '首页\n我的课表\n课程名称')
        self.assertEqual(value, 'my_timetable')
        with self.assertRaises(AppError):
            page_identity('https://ehallapp.nju.edu.cn/jwapp/sys/wdkb/index.do', '南京大学', '未知页面')


class EhallConfigTests(unittest.TestCase):
    def test_loads_safe_exact_hosts(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'ehall.local.json'
            path.write_text(json.dumps({'headless': True, 'timeout_seconds': 10}))
            path.chmod(0o600)
            config = load_ehall_config(path)
            self.assertIn('ehallapp.nju.edu.cn', config['task_hosts'])

    def test_rejects_wildcard_host(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'ehall.local.json'
            path.write_text(json.dumps({'allowed_navigation_hosts': ['*.nju.edu.cn']}))
            path.chmod(0o600)
            with self.assertRaises(AppError):
                load_ehall_config(path)

    def test_rejects_world_readable_local_config(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'ehall.local.json'
            path.write_text('{}')
            path.chmod(0o644)
            with self.assertRaises(AppError):
                load_ehall_config(path)

    def test_private_storage_state_restores_session_cookies(self):
        class Context:
            def __init__(self):
                self.added = None

            def storage_state(self):
                return {'cookies': [{'name': 'session', 'value': 'private',
                                     'domain': '.nju.edu.cn', 'path': '/'}], 'origins': []}

            def add_cookies(self, cookies):
                self.added = cookies

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'state.json'
            first, second = Context(), Context()
            save_storage_state(first, path)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            restore_storage_state(second, path)
            self.assertEqual(second.added[0]['name'], 'session')
            path.chmod(0o644)
            with self.assertRaises(AppError):
                restore_storage_state(second, path)


if __name__ == '__main__':
    unittest.main()
