import json
import threading
import unittest
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer

from assistant import AppError, Assistant
from mail_tasks import MailTasks
from mail_web import WebApp, handler
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
        self.assertEqual(self.request('GET', '/app.js', authenticated=False)[0], 200)
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


if __name__ == '__main__':
    unittest.main()
