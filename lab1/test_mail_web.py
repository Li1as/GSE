import base64
import json
import threading
import unittest
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer

from assistant import AppError, Assistant
from experience import ExperienceService, ExperienceStore
from mail_tasks import MailTasks
from mail_web import WebApp, handler
from ehall_tasks import EhallAttachmentStore, EhallTasks
from ehall_submit import EhallSubmitService
from ehall_worker import EhallWorker
from test_ehall_tasks import FakeBackend
from test_assistant import ScriptedClient
import test_mail_tasks as fixtures


class DraftEditTests(unittest.TestCase):
    setUp = fixtures.MailTaskTests.setUp
    responses = fixtures.MailTaskTests.responses
    waiting = fixtures.MailTaskTests.waiting
    supplement = fixtures.MailTaskTests.supplement
    finish = fixtures.MailTaskTests.finish
    # Reuse the isolated fixture and genuine stage-2B workflow helpers.
    def test_edit_preserves_history_and_rejects_old_version(self):
        task = self.finish()
        self.responses([])
        edited = self.app.edit(task['task_id'], 1, '修改后的正文', '新主题', ['teacher@example.test'])
        self.assertEqual(edited['draft']['version'], 2)
        self.assertEqual(edited['drafts'][0]['body'], task['draft']['body'])
        self.assertEqual(edited['draft']['editor'], 'user')
        with self.assertRaises(AppError) as raised:
            self.app.edit(task['task_id'], 1, '旧页面', '旧主题', ['teacher@example.test'])
        self.assertEqual(raised.exception.code, 'VERSION_CONFLICT')
        self.assertEqual(self.app.get(task['task_id'])['draft']['body'], '修改后的正文')

    def test_edit_rejects_header_injection(self):
        task = self.finish()
        with self.assertRaises(AppError):
            self.app.edit(task['task_id'], 1, '正文', '主题\nBcc: evil@example.test', ['teacher@example.test'])

    def test_stale_sources_block_edit(self):
        task = self.finish()
        from pathlib import Path
        (Path(self.config['personal_data_dir']) / 'resume.md').write_text('# 已更新\n')
        with self.assertRaises(AppError) as raised:
            self.app.edit(task['task_id'], 1, '正文', '主题', ['teacher@example.test'])
        self.assertEqual(raised.exception.code, 'DRAFT_STALE')


class WebTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.MailTaskTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.task = self.fixture.finish()
        self.web = WebApp(self.fixture.app, self.fixture.root, 't' * 40)
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), handler(self.web))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop)

    def stop(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def request(self, method, path, data=None, authenticated=True, origin=None):
        conn = HTTPConnection('127.0.0.1', self.server.server_port, timeout=5)
        headers = {'Content-Type': 'application/json'}
        if authenticated:
            headers['Authorization'] = 'Bearer ' + self.web.token
        if origin:
            headers['Origin'] = origin
        conn.request(method, path, body=json.dumps(data) if data is not None else None, headers=headers)
        response = conn.getresponse()
        body = response.read()
        status = response.status
        conn.close()
        return status, body

    def test_auth_and_cross_origin(self):
        self.assertEqual(self.request('GET', '/api/tasks', authenticated=False)[0], 401)
        self.assertEqual(self.request('GET', '/api/tasks', origin='http://evil.test')[0], 403)
        self.assertEqual(self.request('GET', '/api/tasks')[0], 200)

    def test_csp_allows_local_qr_blob_rendering(self):
        conn = HTTPConnection('127.0.0.1', self.server.server_port, timeout=5)
        conn.request('GET', '/')
        response = conn.getresponse()
        response.read()
        policy = response.getheader('Content-Security-Policy')
        conn.close()
        self.assertEqual(response.status, 200)
        self.assertIn("img-src 'self' blob:", policy)

    def test_http_edit_conflict_and_reopen(self):
        path = '/api/tasks/' + self.task['task_id']
        data = {'version': 1, 'body': '手机修改内容', 'subject': '手机主题', 'to': ['teacher@example.test']}
        status, _ = self.request('POST', path + '/edit', data)
        self.assertEqual(status, 200)
        self.assertEqual(self.request('POST', path + '/edit', data)[0], 409)
        # New HTTP connection reads the durable result.
        status, raw = self.request('GET', path)
        self.assertEqual(json.loads(raw)['draft']['body'], '手机修改内容')
        self.assertEqual(len(json.loads(raw)['drafts']), 2)

    def test_snapshot_selection_and_unconfirmed_send_rejected(self):
        status, raw = self.request('GET', '/api/snapshots')
        self.assertEqual(status, 200)
        self.assertEqual(len(json.loads(raw)), 1)
        self.assertEqual(self.request('POST', '/api/tasks/' + self.task['task_id'] + '/send', {})[0], 400)
        self.assertEqual(self.request('POST', '/api/tasks', {'snapshot': '../../config.local.json'})[0], 400)

    def test_static_assets_and_invalid_input(self):
        self.assertEqual(self.request('GET', '/', authenticated=False)[0], 200)
        status, script = self.request('GET', '/app.js', authenticated=False)
        self.assertEqual(status, 200)
        self.assertIn(b'/api/experience/from-classification', script)
        self.assertIn(b'/api/experience/from-draft', script)
        page = self.request('GET', '/', authenticated=False)[1]
        self.assertIn('经验规则'.encode(), page)
        self.assertIn(b'experience-active-rules', page)
        self.assertIn(b'experience-disabled-rules', page)
        self.assertEqual(self.request('POST', '/api/tasks', [1, 2])[0], 400)

    def test_scheduler_status_controls_and_classification_correction(self):
        from unittest.mock import Mock
        scheduler = Mock()
        scheduler.status.return_value = {'scheduler': {'status': 'active'}, 'monitor': {}}
        scheduler.items.return_value = [{'validity': '1', 'uid': 7, 'category': 'user_review'}]
        scheduler.pause.return_value = {'scheduler': {'status': 'paused'}}
        scheduler.resume.return_value = {'scheduler': {'status': 'active'}}
        scheduler.cycle.return_value = {'classified': 0, 'dispatched': 0}
        scheduler.correct.return_value = {'validity': '1', 'uid': 7, 'category': 'no_reply'}
        scheduler.dismiss.return_value = {'validity': '1', 'uid': 7, 'dismissed': True}
        scheduler.restore.return_value = {'validity': '1', 'uid': 7, 'dismissed': False}
        self.web.scheduler = scheduler
        self.assertEqual(self.request('GET', '/api/automation')[0], 200)
        self.assertEqual(self.request('GET', '/api/mail-queue')[0], 200)
        self.assertEqual(self.request('GET', '/api/mail-queue/handled')[0], 200)
        self.assertEqual(self.request('POST', '/api/automation/pause', {})[0], 200)
        self.assertEqual(self.request('POST', '/api/automation/resume', {})[0], 200)
        self.assertEqual(self.request('POST', '/api/automation/scan', {})[0], 200)
        data = {'validity': '1', 'uid': 7, 'category': 'no_reply', 'reason': '用户确认无需回复'}
        self.assertEqual(self.request('POST', '/api/mail-queue/correct', data)[0], 200)
        self.assertEqual(self.request('POST', '/api/mail-queue/dismiss', {'validity': '1', 'uid': 7})[0], 200)
        self.assertEqual(self.request('POST', '/api/mail-queue/restore', {'validity': '1', 'uid': 7})[0], 200)
        scheduler.cycle.assert_called_once_with(force=True)
        scheduler.correct.assert_called_once_with('1', 7, 'no_reply', '用户确认无需回复', '', '')
        scheduler.dismiss.assert_called_once_with('1', 7)
        scheduler.restore.assert_called_once_with('1', 7)

    def test_http_supplement_and_decision(self):
        self.fixture.path = fixtures.sample(self.fixture.root, '2')
        task = self.fixture.waiting()
        path = '/api/tasks/' + task['task_id']
        status, raw = self.request('GET', path)
        self.assertEqual(len(json.loads(raw)['requests']), 1)
        self.fixture.responses([{'relevant': True, 'conflict': False, 'quotes': [fixtures.REPLY], 'reason': '相关'},
                                fixtures.SEARCH, fixtures.final])
        status, raw = self.request('POST', path + '/answer', {'request_id': task['facts'][0]['request_id'],
                                  'text': fixtures.REPLY, 'scope': 'task'})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(raw)['status'], 'waiting_input')
        self.fixture.responses([{'relevant': True, 'quote': '参加', 'reason': '明确'}, fixtures.draft])
        status, raw = self.request('POST', path + '/decide', {'decision_id': '1', 'text': '参加'})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(raw)['status'], 'draft_ready')

    def test_server_restart_preserves_edit(self):
        path = '/api/tasks/' + self.task['task_id']
        self.request('POST', path + '/edit', {'version': 1, 'body': '重启后保留', 'subject': '主题',
                                            'to': ['teacher@example.test']})
        self.stop()
        tasks = MailTasks(Assistant(self.fixture.config, self.fixture.assistant.client,
                                   self.fixture.assistant.db_path))
        self.web = WebApp(tasks, self.fixture.root, 't' * 40)
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), handler(self.web))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        status, raw = self.request('GET', path)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(raw)['draft']['body'], '重启后保留')
        self.assertEqual(json.loads(raw)['draft']['version'], 2)

    def test_http_confirmed_send_only(self):
        from unittest.mock import Mock
        self.web.sender.config = {'address': 'student@example.test'}
        transport = Mock(return_value={'status': 'accepted', 'stage': 'data', 'smtp_code': 250})
        self.web.sender.transport = transport
        path = '/api/tasks/' + self.task['task_id']
        status, raw = self.request('POST', path + '/prepare-send', {'version': 1})
        self.assertEqual(status, 200)
        record = json.loads(raw)
        self.assertEqual(self.request('POST', path + '/send', {'send_id': record['id']})[0], 400)
        self.assertEqual(self.request('POST', path + '/confirm-send', {'send_id': record['id'],
                         'fingerprint': record['fingerprint'], 'confirmed': False})[0], 400)
        self.assertEqual(self.request('POST', path + '/confirm-send', {'send_id': record['id'],
                         'fingerprint': record['fingerprint'], 'confirmed': True})[0], 200)
        self.assertEqual(self.request('POST', path + '/send', {'send_id': record['id']})[0], 200)
        self.request('POST', path + '/send', {'send_id': record['id']})
        transport.assert_called_once()
        self.assertEqual(self.request('POST', path + '/archive', {})[0], 200)
        self.assertEqual(json.loads(self.request('GET', '/api/tasks')[1]), [])
        archived = json.loads(self.request('GET', '/api/tasks/archived')[1])
        self.assertEqual([item['task_id'] for item in archived], [self.task['task_id']])
        self.assertEqual(self.request('POST', path + '/resume', {})[0], 400)
        self.assertEqual(self.request('POST', path + '/restore', {})[0], 200)
        self.assertEqual(len(json.loads(self.request('GET', '/api/tasks')[1])), 1)

    def test_task_without_accepted_send_cannot_be_archived(self):
        self.assertEqual(self.request('POST', '/api/tasks/' + self.task['task_id'] + '/archive', {})[0], 400)

    def test_experience_draft_proposal_review_and_rule_controls(self):
        candidate = {
            'rule_id': 'mail.draft.concise', 'title': '保持回复简洁',
            'domains': ['mail.draft'], 'instruction': '回复只保留完成目标所需的信息。',
            'examples': ['直接回答明确问题。'], 'counterexamples': ['需要解释风险时不能省略说明。'],
            'rationale': '用户明确要求后续回复更简洁。',
        }
        store = ExperienceStore(self.fixture.assistant.db_path, self.fixture.root/'experience-rules')
        self.web.experience = ExperienceService(store, ScriptedClient([candidate]))
        path = '/api/tasks/' + self.task['task_id']
        self.assertEqual(json.loads(self.request('GET', '/api/experience/proposals')[1]), [])
        self.assertEqual(self.request('POST', path + '/edit', {
            'version': 1, 'body': '简洁回复。', 'subject': '回复',
            'to': ['teacher@example.test']})[0], 200)
        status, raw = self.request('POST', '/api/experience/from-draft', {
            'task_id': self.task['task_id'], 'version': 2,
            'guidance': '以后回复只保留完成目标所需的信息。'})
        self.assertEqual(status, 200)
        proposal = json.loads(raw)
        self.assertEqual(proposal['status'], 'pending')
        self.assertEqual(len(json.loads(self.request('GET', '/api/experience/proposals')[1])), 1)

        revised = dict(candidate, instruction='回复应简洁，但不能省略风险和必要事实。')
        status, raw = self.request('POST', '/api/experience/proposals/' + proposal['id'] + '/approve',
                                   {'candidate': revised})
        self.assertEqual(status, 200)
        rule = json.loads(raw)
        self.assertIn('不能省略风险', rule['versions'][0]['instruction'])
        snapshot = store.snapshot('mail.draft')
        application = store.record_application('mail_task', self.task['task_id'],
            'mail.draft', snapshot, [{
                'rule_id': 'mail.draft.concise', 'version': 1,
                'applicable': True, 'evidence_quotes': ['简洁回复。'],
                'conclusion': '用户当前草稿明确采用简洁回复。',
            }])
        detail = json.loads(self.request('GET', path)[1])
        self.assertEqual(detail['experience_applications'][0]['id'], application['id'])
        self.assertEqual(detail['experience_applications'][0]['payload']['snapshot'], snapshot)
        self.assertEqual(self.request('GET', path + '/experience', authenticated=False)[0], 401)
        status, raw = self.request('GET', path + '/experience')
        self.assertEqual(status, 200)
        audit = json.loads(raw)
        self.assertEqual(set(audit), {'task_id', 'experience_snapshot', 'applications'})
        self.assertEqual(audit['applications'][0]['id'], application['id'])
        self.assertNotIn('mail', audit)
        self.assertNotIn('draft', audit)
        rules = json.loads(self.request('GET', '/api/experience/rules')[1])
        self.assertEqual([item['id'] for item in rules], ['mail.draft.concise'])
        rule_path = '/api/experience/rules/mail.draft.concise/'
        self.assertEqual(self.request('POST', rule_path + 'disable', {'reason': '暂时停用'})[0], 200)
        self.assertFalse(json.loads(self.request('GET', '/api/experience/rules')[1])[0]['enabled'])
        self.assertEqual(self.request('POST', rule_path + 'restore', {'reason': '已核对'})[0], 200)
        self.assertTrue(json.loads(self.request('GET', '/api/experience/rules')[1])[0]['enabled'])

    def test_ehall_mobile_create_qr_decide_edit_and_readback(self):
        store = EhallAttachmentStore(self.fixture.root / 'ehall' / 'attachments')
        ehall = EhallTasks(self.fixture.assistant, attachment_store=store)
        snapshot = {'page_identity': 'my_timetable', 'courses': [
            {'course_id': 'course-a', 'name': '合成课程 A', 'withdrawal_available': True}]}
        worker = EhallWorker(ehall, FakeBackend(snapshot))
        self.web.ehall, self.web.ehall_worker, self.web.ehall_files = ehall, worker, store

        status, raw = self.request('POST', '/api/ehall/attachments', {
            'name': 'note.txt', 'base64': base64.b64encode(b'fiction').decode(),
            'content_type': 'text/plain'})
        self.assertEqual(status, 200)
        attachment = json.loads(raw)
        target = ('https://ehallapp.nju.edu.cn/jwapp/sys/wdkb/'
                  '*default/index.do?private=1#/xskcb')
        status, raw = self.request('POST', '/api/ehall/tasks', {
            'url': target, 'description': '仅作为任务描述', 'attachments': [attachment]})
        self.assertEqual(status, 200)
        task_id = json.loads(raw)['task_id']
        self.assertNotIn('url', json.loads(raw))
        self.assertEqual(worker.status(task_id)['status'], 'queued')

        self.assertEqual(worker.run_once()['status'], 'waiting_input')
        status, raw = self.request('GET', '/api/ehall/tasks/' + task_id)
        task = json.loads(raw)
        self.assertEqual(task['status'], 'waiting_input')
        self.assertNotIn('url', task)
        self.assertEqual(self.request('POST', '/api/ehall/tasks/' + task_id + '/decide',
                                      {'field_id': 'target_course', 'answer': 'course-a'})[0], 200)
        self.assertEqual(worker.run_once()['status'], 'done')
        task = json.loads(self.request('GET', '/api/ehall/tasks/' + task_id)[1])
        self.assertEqual(task['status'], 'preview_ready')
        version = task['field_version']
        edit = {'version': version, 'values': {'target_course': 'course-a'},
                'attachments': [attachment]}
        self.assertEqual(self.request('POST', '/api/ehall/tasks/' + task_id + '/edit', edit)[0], 200)
        self.assertEqual(self.request('POST', '/api/ehall/tasks/' + task_id + '/edit', edit)[0], 409)

        self.assertEqual(worker.run_once()['status'], 'done')
        submit = EhallSubmitService(ehall)
        self.web.ehall_submit = submit
        task = json.loads(self.request('GET', '/api/ehall/tasks/' + task_id)[1])
        self.assertEqual(task['previews'], [])
        status, raw = self.request('POST', '/api/ehall/tasks/' + task_id + '/preview',
                                   {'version': task['field_version']})
        self.assertEqual(status, 200)
        preview = json.loads(raw)
        self.assertEqual(preview['content']['fields'][0]['display'], '合成课程 A')
        confirm = {'preview_id': preview['id'], 'fingerprint': preview['fingerprint'],
                   'confirmed': False}
        self.assertEqual(self.request('POST', '/api/ehall/tasks/' + task_id + '/confirm',
                                      confirm)[0], 400)
        confirm['confirmed'] = True
        self.assertEqual(self.request('POST', '/api/ehall/tasks/' + task_id + '/confirm',
                                      confirm)[0], 200)
        status, raw = self.request('POST', '/api/ehall/tasks/' + task_id + '/submit',
                                   {'preview_id': preview['id']})
        self.assertEqual(status, 200)
        record = json.loads(raw)
        self.assertEqual(record['status'], 'queued')
        self.assertEqual(record['kind'], 'submit')
        self.assertTrue(hasattr(ehall.adapters['timetable_withdrawal'], 'submit'))
        detail = json.loads(self.request('GET', '/api/ehall/tasks/' + task_id)[1])
        self.assertEqual(detail['submissions'], [])
        self.assertEqual(detail['job']['kind'], 'submit')
        self.assertEqual(self.request('POST', '/api/ehall/tasks/' + task_id + '/archive', {})[0], 400)
        completed = ehall.get(task_id)
        completed['status'] = 'succeeded'
        ehall.save(completed)
        self.assertEqual(self.request('POST', '/api/ehall/tasks/' + task_id + '/archive', {})[0], 200)
        self.assertEqual(json.loads(self.request('GET', '/api/ehall/tasks')[1]), [])
        archived = json.loads(self.request('GET', '/api/ehall/tasks/archived')[1])
        self.assertEqual([item['task_id'] for item in archived], [task_id])
        self.assertEqual(self.request('POST', '/api/ehall/tasks/' + task_id + '/restore', {})[0], 200)
        self.assertEqual(len(json.loads(self.request('GET', '/api/ehall/tasks')[1])), 1)

        login = self.fixture.root / 'ehall' / 'login'
        login.mkdir(parents=True)
        (login / 'qr.png').write_bytes(b'fake-png')
        self.assertEqual(self.request('GET', '/api/ehall/login/qr', authenticated=False)[0], 401)
        self.assertEqual(self.request('GET', '/api/ehall/login/qr')[1], b'fake-png')


if __name__ == '__main__':
    unittest.main()
