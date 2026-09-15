"""Stage 4B field model for choosing a course from the timetable.

The input is a bounded, structured snapshot produced by the browser reader.
Page text is never interpreted as instructions. Stage 4D binds submission to
the exact course read back for the confirmed preview.
"""
import hashlib
import json
from urllib.parse import urlsplit

from persistence import AppError


def require(ok, code, message):
    if not ok:
        raise AppError(code, message)


def short_text(value, limit):
    return isinstance(value, str) and bool(value.strip()) and len(value) <= limit


class TimetableWithdrawalAdapter:
    name = 'timetable_withdrawal'
    version = 3
    transaction_name = '退选指定课程'

    def matches(self, url):
        parsed = urlsplit(url)
        return (parsed.hostname == 'ehallapp.nju.edu.cn' and
                '/jwapp/sys/wdkb/' in parsed.path.lower() and
                parsed.fragment.startswith('/xskcb'))

    def inspect(self, snapshot):
        require(isinstance(snapshot, dict), 'PAGE_CHANGED', '课表页面快照无效。')
        require(snapshot.get('page_identity') == 'my_timetable', 'PAGE_CHANGED',
                '页面身份不是“我的课表”。')
        courses = snapshot.get('courses')
        require(isinstance(courses, list) and len(courses) <= 200, 'PAGE_CHANGED',
                '课程列表结构无效。')
        normalized = []
        seen = set()
        for course in courses:
            require(isinstance(course, dict), 'PAGE_CHANGED', '课程项结构无效。')
            course_id, name = course.get('course_id'), course.get('name')
            require(short_text(course_id, 200) and short_text(name, 300) and course_id not in seen,
                    'PAGE_CHANGED', '课程项缺少稳定编号或名称。')
            require(type(course.get('withdrawal_available')) is bool, 'PAGE_CHANGED',
                    '课程项缺少可退课状态。')
            seen.add(course_id)
            normalized.append({'course_id': course_id, 'name': name.strip(),
                               'withdrawal_available': course['withdrawal_available']})
        options = [{'value': row['course_id'], 'label': row['name']}
                   for row in normalized if row['withdrawal_available']]
        blockers = []
        if not options:
            blockers.append({'code': 'NO_WITHDRAWABLE_COURSE',
                             'message': '当前页面没有显示可退选的课程，需要人工核对。'})
        structure = {
            'page_identity': 'my_timetable',
            'adapter': self.name,
            'adapter_version': self.version,
            'courses': normalized,
        }
        fingerprint = hashlib.sha256(json.dumps(
            structure, ensure_ascii=False, sort_keys=True,
            separators=(',', ':')).encode()).hexdigest()
        return {
            'transaction_name': self.transaction_name,
            'page_identity': 'my_timetable',
            'page_summary': {'course_count': len(normalized),
                             'withdrawable_count': len(options)},
            'page_structure': fingerprint,
            'fields': [{
                'id': 'target_course',
                'label': '要退选的课程',
                'source': 'decision',
                'input': 'choice',
                'required': True,
                'question': '请明确选择要退选的具体课程。',
                'options': options,
            }],
            'blockers': blockers,
        }

    def inspect_page(self, page):
        """Extract unique withdrawable courses without invoking page actions."""
        visible = page.locator('body').inner_text()
        require('我的课表' in (page.title() + '\n' + visible)[:50000],
                'PAGE_CHANGED', '目标页面缺少“我的课表”标识。')
        links = page.locator('a#kblbtk.j-row-edit')
        courses = {}
        for index in range(min(links.count(), 400)):
            link = links.nth(index)
            if (link.inner_text() or '').strip() != '退课':
                continue
            course_id = link.get_attribute('data-jxbid')
            name = link.get_attribute('data-jxbmc')
            action = link.get_attribute('data-action')
            require(short_text(course_id, 200) and short_text(name, 300) and
                    short_text(action, 100), 'PAGE_CHANGED', '退课控件缺少稳定字段。')
            cells = link.locator('xpath=ancestor::tr[1]').locator('td')
            displayed = (cells.nth(2).inner_text() or '').strip() if cells.count() >= 3 else ''
            row = courses.setdefault(course_id, {'fallback_names': set(), 'display_names': set()})
            row['fallback_names'].add(name.strip())
            if displayed:
                row['display_names'].add(displayed)
        result = []
        for course_id, row in courses.items():
            names = row['display_names'] or row['fallback_names']
            require(len(names) == 1, 'PAGE_CHANGED', '同一教学班出现冲突的课程名称。')
            result.append({'course_id': course_id, 'name': next(iter(names)),
                           'withdrawal_available': True})
        return {'page_identity': 'my_timetable', 'courses': result}

    def fill(self, page, values, files):
        """Bind to the exact action only; never dispatch clicks or page events."""
        require(not files, 'UNSUPPORTED_ATTACHMENT', '当前退课页面没有可安全上传的附件字段。')
        course_id = values.get('target_course') if isinstance(values, dict) else None
        require(short_text(course_id, 200), 'INPUT_ERROR', '尚未选择要退选的课程。')
        links = page.locator('a#kblbtk.j-row-edit')
        matches = []
        for index in range(min(links.count(), 400)):
            link = links.nth(index)
            if (link.get_attribute('data-jxbid') == course_id and
                    (link.inner_text() or '').strip() == '退课'):
                matches.append(link)
        require(matches, 'PAGE_CHANGED', '所选课程的退课入口已不存在。')
        names = {link.get_attribute('data-jxbmc') for link in matches}
        actions = {link.get_attribute('data-action') for link in matches}
        require(len(names) == 1 and len(actions) == 1, 'PAGE_CHANGED',
                '所选课程的重复控件字段不一致。')
        # This is the complete 4C operation. Never call click(),
        # dispatch_event(), evaluate(), or site JavaScript here.
        return {'target_course': course_id, 'control_count': len(matches),
                'name': next(iter(names)), 'action': next(iter(actions))}

    def read_back(self, page, binding):
        fresh = self.fill(page, {'target_course': binding['target_course']}, [])
        require(fresh['name'] == binding['name'] and fresh['action'] == binding['action'],
                'READBACK_MISMATCH', '课程控件在试填期间发生变化。')
        return {'target_course': fresh['target_course']}

    def dismiss_known_dialogs(self, page):
        """Dismiss only the known, non-transactional timetable notice."""
        overlays = page.locator('.jqx-window-modal:visible')
        if overlays.count() == 0:
            return []
        windows = page.locator('.jqx-window:visible')
        recognized = []
        markers = ('提示', '上课时间冲突信息', '已选课程和已获成绩课程冲突信息',
                   '本学期已选课程冲突信息')
        for index in range(windows.count()):
            window = windows.nth(index)
            text = (window.inner_text() or '').strip()
            if all(marker in text for marker in markers):
                recognized.append(window)
        require(len(recognized) == 1, 'PAGE_BLOCKED',
                '页面存在未识别的遮挡弹窗，未继续提交。')
        closes = recognized[0].get_by_text('关闭', exact=True)
        visible = [closes.nth(index) for index in range(closes.count())
                   if closes.nth(index).is_visible()]
        require(len(visible) == 1, 'PAGE_BLOCKED', '已知提示窗的关闭按钮不唯一。')
        visible[0].click()
        page.wait_for_timeout(250)
        require(page.locator('.jqx-window-modal:visible').count() == 0, 'PAGE_BLOCKED',
                '已知提示窗未能安全关闭。')
        return ['timetable_conflict_notice']

    def normalize_decision(self, field, answer):
        require(field.get('input') == 'choice' and isinstance(answer, str),
                'INPUT_ERROR', '课程选择无效。')
        values = {item['value'] for item in field.get('options', [])}
        require(answer in values, 'INPUT_ERROR', '请从当前可退选课程中明确选择一项。')
        return answer

    def consequences(self, values):
        course_id = values.get('target_course') if isinstance(values, dict) else None
        require(short_text(course_id, 200), 'PREVIEW_INVALID', '冻结预览缺少退课目标。')
        return {
            'action': '退选一门课程',
            'warnings': [
                '退课会改变本学期选课结果，且可能无法自动恢复。',
                '确认后系统将点击该课程的退课入口和站点确定按钮各一次。',
                '提交后超时、断网或无法解析回执时结果记为不明，禁止自动重试。',
            ],
            'submission_mode': 'confirmed_once',
        }

    def open_confirmation(self, page, binding):
        """Open and verify the reversible site confirmation dialog."""
        fresh = self.fill(page, {'target_course': binding['target_course']}, [])
        require(fresh['name'] == binding['name'] and fresh['action'] == '退课',
                'READBACK_MISMATCH', '退课控件在提交前发生变化。')
        links = page.locator('a#kblbtk.j-row-edit')
        matches = [links.nth(index) for index in range(min(links.count(), 400))
                   if links.nth(index).get_attribute('data-jxbid') == binding['target_course'] and
                   (links.nth(index).inner_text() or '').strip() == '退课']
        require(matches, 'PAGE_CHANGED', '提交时退课入口已不存在。')
        # The timetable renders duplicate responsive rows for the same class.
        # fill() has already proved all duplicates carry identical stable
        # identifiers, names and actions, so deterministically use the first.
        matches[0].click()
        prompt = page.get_by_text('是否确认退出' + binding['name'], exact=True)
        prompt.wait_for(state='visible')
        require(prompt.count() == 1, 'PAGE_CHANGED', '站点退课确认内容与课程不一致。')
        buttons = page.get_by_text('确定', exact=True)
        visible = [buttons.nth(index) for index in range(buttons.count())
                   if buttons.nth(index).is_visible()]
        require(len(visible) == 1, 'PAGE_CHANGED', '站点退课确定按钮不唯一。')
        return {'button': visible[0], 'target_course': binding['target_course'],
                'name': binding['name'], 'prompt': '是否确认退出' + binding['name']}

    def submit(self, page, confirmation):
        """Click the irreversible confirmation once and parse its response."""
        with page.expect_response(lambda response:
                response.request.method == 'POST' and
                urlsplit(response.url).path.endswith('/wdkbController/tkljzx.do')) as response_info:
            confirmation['button'].click()
        response = response_info.value
        try:
            payload = response.json()
        except Exception:
            return {'status': 'unknown', 'evidence': {
                'kind': 'unreadable_withdrawal_response', 'http_status': response.status}}
        data = payload.get('data') if isinstance(payload, dict) else None
        message = data.get('TS') if isinstance(data, dict) else None
        evidence = {'kind': 'withdrawal_response', 'http_status': response.status,
                    'site_code': payload.get('code') if isinstance(payload, dict) else None,
                    'site_message': str(message)[:300] if message is not None else None}
        if response.ok and str(message) == '0':
            return {'status': 'succeeded', 'evidence': evidence}
        if response.ok and message is not None:
            return {'status': 'failed', 'evidence': evidence}
        return {'status': 'unknown', 'evidence': evidence}
