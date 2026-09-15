"""Stage 4A: URL-driven, read-only ehall login and page inspection."""
import argparse
import fcntl
import hashlib
import json
import os
import sys
import tempfile
import uuid
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from persistence import AppError


ROOT = Path(__file__).resolve().parent
DEFAULT_TASK_HOSTS = ('ehall.nju.edu.cn', 'ehallapp.nju.edu.cn')
DEFAULT_NAVIGATION_HOSTS = DEFAULT_TASK_HOSTS + ('authserver.nju.edu.cn',)
MAX_URL = 8192
MAX_TEXT = 200000


def now():
    return datetime.now(timezone.utc).isoformat()


def require(ok, code, message):
    if not ok:
        raise AppError(code, message)


def host_list(value, default):
    if value is None:
        value = list(default)
    require(isinstance(value, list) and value, 'CONFIG_ERROR', '允许域名配置无效。')
    result = []
    for item in value:
        require(isinstance(item, str) and item == item.lower() and
                item.strip('.') == item and '*' not in item and '/' not in item,
                'CONFIG_ERROR', '允许域名必须是小写的精确主机名。')
        result.append(item)
    return tuple(dict.fromkeys(result))


def load_ehall_config(path):
    path = Path(path)
    require(path.is_file() and not path.is_symlink(), 'CONFIG_ERROR', 'ehall 本地配置不存在。')
    require(path.stat().st_mode & 0o077 == 0, 'CONFIG_ERROR', 'ehall 本地配置权限须为 600。')
    try:
        value = json.loads(path.read_text())
    except (OSError, ValueError):
        raise AppError('CONFIG_ERROR', 'ehall 本地配置不是有效 JSON。') from None
    require(isinstance(value, dict), 'CONFIG_ERROR', 'ehall 本地配置必须是 JSON 对象。')
    value = dict(value)
    value['task_hosts'] = host_list(value.get('task_hosts'), DEFAULT_TASK_HOSTS)
    value['allowed_navigation_hosts'] = host_list(
        value.get('allowed_navigation_hosts'), DEFAULT_NAVIGATION_HOSTS)
    require(set(value['task_hosts']) <= set(value['allowed_navigation_hosts']),
            'CONFIG_ERROR', '任务域名必须包含在导航白名单中。')
    timeout = value.get('timeout_seconds', 30)
    require(type(timeout) is int and 5 <= timeout <= 120,
            'CONFIG_ERROR', 'timeout_seconds 须为 5—120。')
    require(type(value.get('headless', True)) is bool, 'CONFIG_ERROR', 'headless 须为布尔值。')
    qr_wait = value.get('qr_wait_seconds', 180)
    require(type(qr_wait) is int and 30 <= qr_wait <= 600,
            'CONFIG_ERROR', 'qr_wait_seconds 须为 30—600。')
    value['timeout_seconds'] = timeout
    value['headless'] = value.get('headless', True)
    value['qr_wait_seconds'] = qr_wait
    return value


def normalize_url(raw, allowed_hosts=DEFAULT_TASK_HOSTS):
    require(isinstance(raw, str) and raw.strip() and len(raw) <= MAX_URL,
            'INPUT_ERROR', '任务 URL 无效或过长。')
    # Only decode the explicit HTML entity. html.unescape() also rewrites the
    # genuine portal parameter name "amp_sec_version_" when no semicolon exists.
    raw = raw.strip().replace('&amp;', '&')
    parsed = urlsplit(raw)
    require(parsed.scheme.lower() == 'https', 'URL_NOT_ALLOWED', 'ehall 任务 URL 必须使用 HTTPS。')
    require(parsed.username is None and parsed.password is None,
            'URL_NOT_ALLOWED', '任务 URL 不能包含账号或密码。')
    require(parsed.hostname is not None and parsed.hostname.lower() in set(allowed_hosts),
            'URL_NOT_ALLOWED', '任务 URL 不在允许的 ehall 域名内。')
    try:
        port = parsed.port
    except ValueError:
        raise AppError('URL_NOT_ALLOWED', '任务 URL 端口无效。') from None
    require(port in (None, 443), 'URL_NOT_ALLOWED', '任务 URL 不能使用非 HTTPS 标准端口。')
    require(not any(ord(ch) < 32 for ch in raw), 'INPUT_ERROR', '任务 URL 包含控制字符。')
    netloc = parsed.hostname.lower()
    return urlunsplit(('https', netloc, parsed.path or '/', parsed.query, parsed.fragment))


def navigation_host(raw, allowed_hosts):
    parsed = urlsplit(raw)
    try:
        port = parsed.port
    except ValueError:
        raise AppError('NAVIGATION_BLOCKED', '浏览器跳转地址的端口无效。') from None
    require(parsed.scheme.lower() == 'https' and parsed.hostname and
            parsed.hostname.lower() in set(allowed_hosts) and port in (None, 443),
            'NAVIGATION_BLOCKED', '浏览器跳转到了未允许的地址，任务已暂停。')
    return parsed.hostname.lower()


def public_url(raw):
    """Never expose query tokens or fragments in CLI/API summaries."""
    parsed = urlsplit(raw)
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, '', ''))


def page_identity(url, title, visible_text):
    parsed = urlsplit(url)
    text = (title + '\n' + visible_text[:50000]).lower()
    if parsed.hostname == 'ehallapp.nju.edu.cn' and '/jwapp/sys/wdkb/' in parsed.path.lower():
        require('我的课表' in text, 'PAGE_CHANGED', '目标页面未出现“我的课表”标识。')
        return 'my_timetable'
    raise AppError('UNSUPPORTED_TRANSACTION', '该 URL 尚无阶段 4A 页面身份适配器。')


def atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(dir=str(path.parent), prefix='.probe-', suffix='.tmp')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, str(path))
        path.chmod(0o600)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def restore_storage_state(context, path):
    """Restore private session cookies explicitly, including session cookies."""
    path = Path(path)
    if not path.exists():
        return
    require(path.is_file() and not path.is_symlink() and path.stat().st_mode & 0o077 == 0,
            'SESSION_INVALID', '浏览器会话文件权限或类型无效。')
    try:
        state = json.loads(path.read_text(encoding='utf-8'))
        cookies = state.get('cookies', [])
        require(isinstance(cookies, list), 'SESSION_INVALID', '浏览器会话文件无效。')
        if cookies:
            context.add_cookies(cookies)
    except (OSError, ValueError):
        raise AppError('SESSION_INVALID', '浏览器会话文件无法读取。') from None


def save_storage_state(context, path):
    # storage_state contains authentication material and must stay private.
    atomic_json(Path(path), context.storage_state())


def atomic_screenshot(locator, path):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp.png')
    try:
        locator.screenshot(path=str(temporary))
        os.replace(str(temporary), str(path))
        path.chmod(0o600)
    finally:
        if temporary.exists():
            temporary.unlink()


@contextmanager
def qr_login_lock(directory):
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = directory / 'qr.lock'
    with path.open('a') as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise AppError('LOGIN_BUSY', '已有二维码登录正在等待扫码。') from None
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def first_visible(page, selectors):
    for selector in selectors:
        matches = page.locator(selector)
        try:
            for index in range(min(matches.count(), 20)):
                locator = matches.nth(index)
                if locator.is_visible():
                    return locator
        except Exception:
            continue
    return None


class EhallProbe:
    def __init__(self, config, data_dir):
        self.config = config
        self.data_dir = Path(data_dir)

    def _login(self, page, target):
        if urlsplit(page.url).hostname in self.config['task_hosts']:
            return
        navigation_host(page.url, self.config['allowed_navigation_hosts'])
        qr = first_visible(page, ('#qr_img', '#qrLoginDiv img', '#qrLoginForm img'))
        require(qr is not None, 'LOGIN_REQUIRED',
                '统一认证页没有可识别的登录二维码，任务未继续。')
        login_dir = self.data_dir / 'login'
        qr_path = login_dir / 'qr.png'
        status_path = login_dir / 'qr.json'
        with qr_login_lock(login_dir):
            atomic_screenshot(qr, qr_path)
            last_qr_source = qr.get_attribute('src')
            atomic_json(status_path, {
                'status': 'waiting_scan', 'captured_at': now(),
                'qr_path': str(qr_path.resolve()),
                'meaning': '短期登录二维码，仅用于本次人工扫码。',
            })
            print(json.dumps({'event': 'qr_ready', 'path': str(qr_path.resolve())},
                             ensure_ascii=False), flush=True)
            deadline = time.monotonic() + self.config['qr_wait_seconds']
            try:
                while time.monotonic() < deadline:
                    page.wait_for_timeout(1000)
                    navigation_host(page.url, self.config['allowed_navigation_hosts'])
                    if urlsplit(page.url).hostname in self.config['task_hosts']:
                        current, requested = urlsplit(page.url), urlsplit(target)
                        if current.hostname != requested.hostname or current.path != requested.path:
                            page.goto(target, wait_until='domcontentloaded')
                        return
                    current_qr = first_visible(page, ('#qr_img', '#qrLoginDiv img', '#qrLoginForm img'))
                    if current_qr is not None:
                        current_source = current_qr.get_attribute('src')
                        if current_source != last_qr_source:
                            atomic_screenshot(current_qr, qr_path)
                            last_qr_source = current_source
                raise AppError('LOGIN_REQUIRED', '等待二维码扫码登录超时，任务未继续。')
            finally:
                for path in (qr_path, status_path):
                    if path.exists():
                        path.unlink()

    def run(self, raw_url):
        target = normalize_url(raw_url, self.config['task_hosts'])
        try:
            from playwright.sync_api import Error as PlaywrightError, sync_playwright
        except ImportError:
            raise AppError('BROWSER_UNAVAILABLE',
                           '未安装 Playwright；请安装 requirements-ehall.txt 并执行 playwright install chromium。') from None
        probe_id = uuid.uuid4().hex
        directory = self.data_dir / 'probes' / probe_id
        directory.mkdir(parents=True, mode=0o700)
        profile = self.data_dir / 'browser-profile'
        profile.mkdir(parents=True, exist_ok=True, mode=0o700)
        timeout = self.config['timeout_seconds'] * 1000
        try:
            with sync_playwright() as playwright:
                context = playwright.chromium.launch_persistent_context(
                    str(profile), headless=self.config['headless'], accept_downloads=False,
                    viewport={'width': 1440, 'height': 1000})
                try:
                    restore_storage_state(context, self.data_dir / 'storage-state.json')
                    page = context.pages[0] if context.pages else context.new_page()
                    page.set_default_timeout(timeout)
                    blocked = []
                    def guard(route):
                        request = route.request
                        if request.is_navigation_request() and request.frame == page.main_frame:
                            try:
                                navigation_host(request.url, self.config['allowed_navigation_hosts'])
                            except AppError:
                                blocked.append(public_url(request.url))
                                route.abort()
                                return
                        route.continue_()
                    page.route('**/*', guard)
                    try:
                        page.goto(target, wait_until='domcontentloaded', timeout=timeout)
                    except PlaywrightError:
                        if blocked:
                            raise AppError('NAVIGATION_BLOCKED',
                                           '浏览器尝试跳转到未允许的地址，任务已暂停。') from None
                        raise
                    navigation_host(page.url, self.config['allowed_navigation_hosts'])
                    self._login(page, target)
                    page.wait_for_timeout(3000)
                    navigation_host(page.url, self.config['allowed_navigation_hosts'])
                    body = page.locator('body').inner_text(timeout=timeout)
                    require(len(body) <= MAX_TEXT, 'PAGE_TOO_LARGE', '页面文本超过阶段 4A 上限。')
                    identity = page_identity(page.url, page.title(), body)
                    save_storage_state(context, self.data_dir / 'storage-state.json')
                    buttons = page.locator('button, [role="button"], input[type="button"], input[type="submit"]')
                    labels = []
                    for index in range(min(buttons.count(), 100)):
                        value = (buttons.nth(index).inner_text() or buttons.nth(index).get_attribute('value') or '').strip()
                        if value:
                            labels.append(value[:200])
                    screenshot = directory / 'page.png'
                    page.screenshot(path=str(screenshot), full_page=True)
                    screenshot.chmod(0o600)
                    record = {
                        'probe_id': probe_id, 'status': 'ready', 'created_at': now(),
                        'requested_url': target, 'final_url': page.url,
                        'page_title': page.title()[:500], 'page_identity': identity,
                        'visible_text': body, 'button_labels': labels,
                        'screenshot_sha256': hashlib.sha256(screenshot.read_bytes()).hexdigest(),
                        'read_only': True,
                    }
                    atomic_json(directory / 'probe.json', record)
                    return {
                        'probe_id': probe_id, 'status': 'ready', 'page_identity': identity,
                        'page_title': record['page_title'], 'final_url': public_url(page.url),
                        'button_count': len(labels), 'read_only': True,
                    }
                finally:
                    context.close()
        except AppError:
            raise
        except PlaywrightError as error:
            raise AppError('BROWSER_ERROR', '浏览器无法完成只读探测：' + type(error).__name__) from None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    check = sub.add_parser('check-url', help='只校验并规范化 URL，不访问网络')
    check.add_argument('url')
    probe = sub.add_parser('probe', help='登录并只读探测用户提供的 URL')
    probe.add_argument('url')
    probe.add_argument('--config', default=str(ROOT / 'ehall.local.json'))
    probe.add_argument('--data-dir', default=str(ROOT / 'data' / 'ehall'))
    probe.add_argument('--qr-wait', type=int,
                       help='等待人工扫码的秒数，覆盖本地配置（30—600）')
    args = parser.parse_args()
    try:
        if args.command == 'check-url':
            print(json.dumps({'url': public_url(normalize_url(args.url)), 'valid': True}, ensure_ascii=False))
            return
        config = load_ehall_config(args.config)
        if args.qr_wait is not None:
            require(30 <= args.qr_wait <= 600, 'INPUT_ERROR', '--qr-wait 须为 30—600。')
            config['qr_wait_seconds'] = args.qr_wait
        print(json.dumps(EhallProbe(config, args.data_dir).run(args.url), ensure_ascii=False))
    except AppError as error:
        print(json.dumps({'error': str(error), 'code': error.code}, ensure_ascii=False), file=sys.stderr)
        raise SystemExit(1)


if __name__ == '__main__':
    main()
