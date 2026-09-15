import base64
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from assistant import Assistant
from ehall_adapters.timetable_withdrawal import TimetableWithdrawalAdapter
from ehall_tasks import EhallAttachmentStore, EhallTasks
from ehall_worker import EhallWorker
from persistence import AppError
from test_assistant import ScriptedClient


URL = 'https://ehallapp.nju.edu.cn/jwapp/sys/wdkb/*default/index.do?a=private#/xskcb'
SEARCH = {'action': 'search_personal_info', 'keywords': ['联系电话']}
SEARCH_AGAIN = {'action': 'search_personal_info', 'keywords': ['手机号码']}
MISSING = {'action': 'final', 'answers': [], 'missing': ['办理表单要求的联系电话'], 'conflicts': []}
REPLY = '本次办理使用的联系电话是 13800000000。'


def final_answer(messages):
    chunks = json.loads(messages[-1]['content'])['tool_result']['results']
    row = next(chunk for chunk in chunks if '13800000000' in chunk['text'])
    return {'action': 'final', 'answers': [{'text': REPLY, 'citations': [row['id']]}],
            'missing': [], 'conflicts': []}


class SyntheticAdapter:
    name = 'synthetic_form'
    version = 1
    transaction_name = '合成表单'

    def matches(self, url):
        return '/synthetic/' in url

    def inspect(self, snapshot):
        if snapshot != {'page_identity': 'synthetic'}:
            raise AppError('PAGE_CHANGED', '合成页面不匹配。')
        return {'transaction_name': self.transaction_name, 'page_identity': 'synthetic',
                'page_summary': {'field_count': 1}, 'page_structure': 'synthetic-v1',
                'fields': [{'id': 'phone', 'label': '联系电话', 'source': 'personal',
                            'required': True, 'query': '本次办理表单所需的联系电话是什么？'}],
                'blockers': []}


class FakeElement:
    def __init__(self, text='', attributes=None, cells=None, children_by_text=None):
        self.text, self.attributes, self.cells = text, attributes or {}, cells
        self.children_by_text = children_by_text or {}
        self.clicks = 0

    def inner_text(self):
        return self.text

    def get_attribute(self, name):
        return self.attributes.get(name)

    def locator(self, selector):
        if selector == 'xpath=ancestor::tr[1]':
            return FakeElement(cells=self.cells or [])
        if selector == 'td' and self.cells is not None:
            return FakeLocator(self.cells)
        raise AssertionError(selector)

    def click(self):
        self.clicks += 1

    def is_visible(self):
        return True

    def get_by_text(self, text, exact=False):
        return FakeLocator(self.children_by_text.get(text, []))


class FakeLocator:
    def __init__(self, elements):
        self.elements = elements

    def count(self):
        return len(self.elements)

    def nth(self, index):
        return self.elements[index]

    def inner_text(self):
        return self.elements[0].inner_text()

    def wait_for(self, state=None):
        return None


class FakeRequest:
    method = 'POST'


class FakeResponse:
    request = FakeRequest()
    url = 'https://ehallapp.nju.edu.cn/jwapp/sys/wdkb/wdkbController/tkljzx.do'
    status = 200
    ok = True

    def json(self):
        return {'code': '0', 'data': {'TS': '0'}}


class FakeResponseInfo:
    value = FakeResponse()

    def __init__(self, predicate):
        if not predicate(self.value):
            raise AssertionError('response predicate rejected expected response')

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False


class FakePage:
    def __init__(self, links, windows=None, overlays=None):
        self.links = links
        self.windows = windows or []
        self.overlays = overlays or []
        self.confirm = FakeElement('确定')

    def title(self):
        return '我的课表'

    def locator(self, selector):
        if selector == 'body':
            return FakeLocator([FakeElement('我的课表')])
        if selector == 'a#kblbtk.j-row-edit':
            return FakeLocator(self.links)
        if selector == '.jqx-window:visible':
            return FakeLocator(self.windows)
        if selector == '.jqx-window-modal:visible':
            return FakeLocator(self.overlays)
        raise AssertionError(selector)

    def get_by_text(self, text, exact=False):
        if text.startswith('是否确认退出'):
            return FakeLocator([FakeElement(text)])
        if text == '确定':
            return FakeLocator([self.confirm])
        return FakeLocator([])

    def expect_response(self, predicate):
        return FakeResponseInfo(predicate)

    def wait_for_timeout(self, milliseconds):
        self.overlays = []


class FakeBackend:
    def __init__(self, snapshot):
        self.snapshot = snapshot
        self.visits = []

    def visit(self, task, adapter, prepare=False):
        self.visits.append(prepare)
        if not prepare:
            return {'snapshot': self.snapshot}
        return {'page_structure': task['page_structure'],
                'values': dict(task['prepared_values'])}


class FailingBackend:
    def __init__(self, code):
        self.code = code

    def visit(self, task, adapter, prepare=False):
        raise AppError(self.code, '合成浏览器暂停。')


class EhallTaskTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.personal = self.root / 'personal'
        self.personal.mkdir()
        (self.personal / 'profile.md').write_text('# 合成个人资料\n\n未记录联系电话。\n', encoding='utf-8')
        config = {'model': 'test', 'personal_data_dir': str(self.personal), 'max_steps': 8}
        self.client = ScriptedClient([])
        self.assistant = Assistant(config, self.client, self.root / 'tasks.sqlite')

    def production(self):
        adapter = TimetableWithdrawalAdapter()
        return EhallTasks(self.assistant, {adapter.name: adapter})

    def synthetic(self):
        adapter = SyntheticAdapter()
        return EhallTasks(self.assistant, {adapter.name: adapter})

    @staticmethod
    def timetable_snapshot(available=True):
        return {'page_identity': 'my_timetable', 'courses': [
            {'course_id': 'course-a', 'name': '合成课程 A',
             'withdrawal_available': available},
            {'course_id': 'course-b', 'name': '合成课程 B',
             'withdrawal_available': False},
        ]}

    def test_url_description_and_attachment_are_task_scoped_and_persistent(self):
        app = self.production()
        attachment = {'name': 'proof.pdf', 'size': 7,
                      'sha256': hashlib.sha256(b'fiction').hexdigest(),
                      'content_type': 'application/pdf'}
        task = app.create(URL, '我想退课，请按页面字段继续询问。', [attachment])
        self.assertEqual(task['status'], 'new')
        self.assertEqual(task['description'], '我想退课，请按页面字段继续询问。')
        self.assertEqual(task['attachments'], [attachment])
        self.assertNotIn('a=private', task['public_url'])
        reopened = EhallTasks(Assistant(self.assistant.config, self.client,
                                        self.assistant.db_path))
        self.assertEqual(reopened.get(task['task_id'])['attachments'], [attachment])

    def test_description_does_not_choose_course_but_adapter_requires_confirmation_path(self):
        app = self.production()
        task = app.create(URL, '请退选合成课程 A')
        task = app.inspect(task['task_id'], self.timetable_snapshot())
        self.assertEqual(task['status'], 'waiting_input')
        self.assertIsNone(task['decisions'][0]['answer'])
        self.assertFalse(task['facts'])
        with self.assertRaises(AppError):
            app.decide(task['task_id'], 'target_course', 'course-b')
        task = app.decide(task['task_id'], 'target_course', 'course-a')
        self.assertEqual(task['status'], 'ready_to_fill')
        self.assertEqual(task['prepared_values']['target_course'], 'course-a')
        adapter = app.adapters['timetable_withdrawal']
        self.assertTrue(hasattr(adapter, 'submit'))

    def test_no_available_course_pauses_for_review(self):
        app = self.production()
        task = app.create(URL)
        task = app.inspect(task['task_id'], self.timetable_snapshot(False))
        self.assertEqual(task['status'], 'needs_review')
        self.assertEqual(task['blockers'][0]['code'], 'NO_WITHDRAWABLE_COURSE')

    def test_missing_fact_task_scope_resumes_same_parent_without_leaking(self):
        app = self.synthetic()
        self.client.responses = iter([SEARCH, SEARCH_AGAIN, MISSING])
        first = app.create('https://ehallapp.nju.edu.cn/synthetic/index.do')
        first = app.inspect(first['task_id'], {'page_identity': 'synthetic'})
        self.assertEqual(first['status'], 'waiting_input')
        request_id = first['facts'][0]['request_id']
        self.client.responses = iter([
            {'relevant': True, 'conflict': False, 'quotes': [REPLY], 'reason': '回答了当前字段'},
            SEARCH, final_answer])
        first = app.answer(first['task_id'], request_id, REPLY, 'task')
        self.assertEqual(first['status'], 'ready_to_fill')
        self.assertFalse(list(self.personal.glob('supplement-*.md')))

        self.client.responses = iter([SEARCH, SEARCH_AGAIN, MISSING])
        second = app.create('https://ehallapp.nju.edu.cn/synthetic/index.do')
        second = app.inspect(second['task_id'], {'page_identity': 'synthetic'})
        self.assertEqual(second['status'], 'waiting_input')
        self.assertNotEqual(second['facts'][0]['child_id'], first['facts'][0]['child_id'])
        with self.assertRaises(AppError):
            app.answer(second['task_id'], request_id, REPLY, 'task')

    def test_personal_scope_is_available_to_later_parent(self):
        app = self.synthetic()
        self.client.responses = iter([SEARCH, SEARCH_AGAIN, MISSING])
        first = app.inspect(app.create(
            'https://ehallapp.nju.edu.cn/synthetic/index.do')['task_id'],
            {'page_identity': 'synthetic'})
        self.client.responses = iter([
            {'relevant': True, 'conflict': False, 'quotes': [REPLY], 'reason': '回答了当前字段'},
            SEARCH, final_answer])
        first = app.answer(first['task_id'], first['facts'][0]['request_id'], REPLY, 'personal')
        self.assertEqual(first['status'], 'ready_to_fill')
        self.assertEqual(len(list(self.personal.glob('supplement-*.md'))), 1)

        self.client.responses = iter([SEARCH, final_answer])
        second = app.inspect(app.create(
            'https://ehallapp.nju.edu.cn/synthetic/index.do')['task_id'],
            {'page_identity': 'synthetic'})
        self.assertEqual(second['status'], 'ready_to_fill')

    def test_bad_snapshot_and_attachment_metadata_are_rejected(self):
        app = self.production()
        with self.assertRaises(AppError):
            app.create(URL, attachments=[{'name': '../x', 'size': 1, 'sha256': '0' * 64}])
        task = app.create(URL)
        with self.assertRaises(AppError):
            app.inspect(task['task_id'], {'page_identity': 'other', 'courses': []})
        paused = app.get(task['task_id'])
        self.assertEqual(paused['status'], 'needs_review')
        self.assertEqual(paused['pause_reason']['code'], 'PAGE_CHANGED')

    def test_live_page_shape_is_deduplicated_and_never_clicked(self):
        attrs = {'data-jxbid': 'course-a', 'data-jxbmc': '合成课程 A',
                 'data-action': 'withdraw'}
        cells = [FakeElement(''), FakeElement('00000000'), FakeElement('合成课程 A')]
        page = FakePage([FakeElement('退课', attrs, cells), FakeElement('退课', attrs)])
        adapter = TimetableWithdrawalAdapter()
        snapshot = adapter.inspect_page(page)
        self.assertEqual(len(snapshot['courses']), 1)
        binding = adapter.fill(page, {'target_course': 'course-a'}, [])
        self.assertEqual(binding['control_count'], 2)
        self.assertEqual(adapter.read_back(page, binding), {'target_course': 'course-a'})
        self.assertEqual(sum(link.clicks for link in page.links), 0)

    def test_submit_clicks_exact_course_and_confirmation_once(self):
        attrs = {'data-jxbid': 'course-a', 'data-jxbmc': '合成课程 A',
                 'data-action': '退课'}
        cells = [FakeElement(''), FakeElement('00000000'), FakeElement('合成课程 A')]
        link = FakeElement('退课', attrs, cells)
        duplicate = FakeElement('退课', attrs, cells)
        page = FakePage([link, duplicate])
        adapter = TimetableWithdrawalAdapter()
        binding = adapter.fill(page, {'target_course': 'course-a'}, [])
        confirmation = adapter.open_confirmation(page, binding)
        result = adapter.submit(page, confirmation)
        self.assertEqual(result['status'], 'succeeded')
        self.assertEqual(link.clicks, 1)
        self.assertEqual(duplicate.clicks, 0)
        self.assertEqual(page.confirm.clicks, 1)

    def test_known_notice_is_dismissed_but_unknown_modal_is_blocked(self):
        close = FakeElement('关闭')
        text = ('提示\n上课时间冲突信息\n已选课程和已获成绩课程冲突信息\n'
                '本学期已选课程冲突信息')
        window = FakeElement(text, children_by_text={'关闭': [close]})
        page = FakePage([], [window], [FakeElement()])
        adapter = TimetableWithdrawalAdapter()
        self.assertEqual(adapter.dismiss_known_dialogs(page), ['timetable_conflict_notice'])
        self.assertEqual(close.clicks, 1)
        blocked = FakePage([], [FakeElement('未知弹窗')], [FakeElement()])
        with self.assertRaises(AppError):
            adapter.dismiss_known_dialogs(blocked)

    def test_attachment_integrity_edit_versions_and_readback(self):
        store = EhallAttachmentStore(self.root / 'ehall-files')
        uploaded = store.upload('proof.txt', base64.b64encode(b'fiction').decode(), 'text/plain')
        app = EhallTasks(self.assistant, attachment_store=store)
        task = app.create(URL, attachments=[uploaded])
        task = app.inspect(task['task_id'], self.timetable_snapshot())
        task = app.decide(task['task_id'], 'target_course', 'course-a')
        old_version = task['field_version']
        task = app.edit(task['task_id'], old_version, {'target_course': 'course-a'}, [uploaded])
        self.assertGreater(task['field_version'], old_version)
        with self.assertRaises(AppError) as raised:
            app.edit(task['task_id'], old_version, {'target_course': 'course-a'}, [uploaded])
        self.assertEqual(raised.exception.code, 'VERSION_CONFLICT')
        with self.assertRaises(AppError) as raised:
            app.record_readback(task['task_id'], old_version, task['page_structure'],
                                task['prepared_values'])
        self.assertEqual(raised.exception.code, 'VERSION_CONFLICT')
        self.assertEqual(app.get(task['task_id'])['status'], 'ready_to_fill')
        with self.assertRaises(AppError):
            app.record_readback(task['task_id'], task['field_version'], 'changed',
                                task['prepared_values'])
        task = app.record_readback(task['task_id'], task['field_version'],
                                   task['page_structure'], task['prepared_values'])
        self.assertEqual(task['status'], 'preview_ready')
        self.assertFalse(task['fill_readback']['submitted'])
        (self.root / 'ehall-files' / uploaded['sha256']).write_bytes(b'changed')
        with self.assertRaises(AppError) as raised:
            store.verify(uploaded)
        self.assertEqual(raised.exception.code, 'ATTACHMENT_CHANGED')

    def test_worker_only_consumes_explicit_jobs_and_stops_for_decision(self):
        app = self.production()
        backend = FakeBackend(self.timetable_snapshot())
        worker = EhallWorker(app, backend)
        task = app.create(URL)
        self.assertIsNone(worker.run_once())
        worker.enqueue(task['task_id'])
        result = worker.run_once()
        self.assertEqual(result['status'], 'waiting_input')
        self.assertEqual(backend.visits, [False])
        task = app.decide(task['task_id'], 'target_course', 'course-a')
        worker.enqueue(task['task_id'])
        result = worker.run_once()
        self.assertEqual(result['status'], 'done')
        self.assertEqual(app.get(task['task_id'])['status'], 'preview_ready')
        self.assertEqual(backend.visits, [False, True])

    def test_worker_persists_login_and_page_change_pauses(self):
        for code in ('LOGIN_REQUIRED', 'PAGE_CHANGED'):
            with self.subTest(code=code):
                app = self.production()
                worker = EhallWorker(app, FailingBackend(code))
                task = app.create(URL)
                worker.enqueue(task['task_id'])
                result = worker.run_once()
                self.assertEqual(result['status'], 'failed')
                paused = app.get(task['task_id'])
                self.assertEqual(paused['status'], 'needs_review')
                self.assertEqual(paused['pause_reason']['code'], code)


if __name__ == '__main__':
    unittest.main()
