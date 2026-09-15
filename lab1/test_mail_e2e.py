"""Stage 2E: whole HTTP workflow and failures at persistence/network boundaries."""
import base64
import json
import socket
import threading
import unittest
from email import policy
from email.parser import BytesParser
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from unittest.mock import Mock, patch

from assistant import Assistant, AppError
from mail_reader import MailError
from mail_send import deliver
from mail_delivery_audit import audit
from mail_tasks import MailTasks
from mail_web import WebApp, handler
import test_mail_tasks as fixtures


class EndToEndTests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.MailTaskTests(); self.f.setUp(); self.addCleanup(self.f.doCleanups)
        self.config = {'address': 'student@example.test', 'password': 'fake', 'smtp_host': 'example.test', 'smtp_port': 465}
        self.start(self.f.app)
        self.addCleanup(self.stop)

    def start(self, app):
        self.web = WebApp(app, self.f.root, 't' * 40, self.config)
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), handler(self.web))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def stop(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join()

    def restart(self):
        self.stop()
        app = MailTasks(Assistant(self.f.config, self.f.assistant.client, self.f.assistant.db_path))
        self.start(app)

    def request(self, method, path, data=None):
        client = HTTPConnection('127.0.0.1', self.server.server_port, timeout=5)
        client.request(method, path, json.dumps(data) if data is not None else None,
                       {'Authorization': 'Bearer ' + self.web.token, 'Content-Type': 'application/json'})
        reply = client.getresponse(); status = reply.status; raw = reply.read(); client.close()
        return status, json.loads(raw)

    def create(self):
        self.f.responses([fixtures.PLAN, fixtures.SEARCH,
                          {'action': 'search_personal_info', 'keywords': ['分数']}, fixtures.MISSING])
        status, task = self.request('POST', '/api/tasks', {'snapshot': self.f.path.name, 'goal': '查询成绩并询问是否参加'})
        self.assertEqual(status, 200)
        return task

    def test_full_workflow_restart_edit_attachment_confirm_archive(self):
        task = self.create(); path = '/api/tasks/' + task['task_id']
        _, task = self.request('GET', path)
        self.assertEqual(task['status'], 'waiting_input')
        rid = task['requests'][0]['request_id']
        self.f.responses([{'relevant': False, 'conflict': False, 'quotes': [], 'reason': '无关'}])
        _, result = self.request('POST', path + '/answer', {'request_id': rid, 'text': '天气晴朗', 'scope': 'personal'})
        self.assertEqual(result['reply_feedback']['status'], 'irrelevant')
        self.f.responses([{'relevant': True, 'conflict': False, 'quotes': [fixtures.REPLY], 'reason': '相关'}, fixtures.SEARCH, fixtures.final])
        _, task = self.request('POST', path + '/answer', {'request_id': rid, 'text': fixtures.REPLY, 'scope': 'task'})
        self.assertEqual(task['status'], 'waiting_input')
        self.f.responses([{'relevant': True, 'quote': '参加', 'reason': '明确'}, fixtures.draft])
        _, task = self.request('POST', path + '/decide', {'decision_id': '1', 'text': '参加'})
        self.assertEqual(task['status'], 'draft_ready')
        self.restart()
        _, task = self.request('GET', path)
        self.assertEqual(task['requests'], [])
        _, attachment = self.request('POST', '/api/attachments', {'name': '报名说明.txt', 'base64': base64.b64encode(b'fixture attachment').decode()})
        edited = {'version': 1, 'body': '我确认参加，课程成绩为93分。附件供参考。', 'subject': '报名回复',
                  'to': ['teacher@example.test'], 'cc': [], 'bcc': ['archive@example.test'], 'attachments': [attachment]}
        self.assertEqual(self.request('POST', path + '/edit', edited)[0], 200)
        self.assertEqual(self.request('POST', path + '/edit', edited)[0], 409)
        _, preview = self.request('POST', path + '/prepare-send', {'version': 2})
        smtp = Mock(); smtp.mail.return_value=(250,b'ok'); smtp.rcpt.return_value=(250,b'ok'); smtp.data.return_value=(250,b'ok')
        self.web.sender.transport = lambda config, envelope, raw, phase: deliver(config, envelope, raw, phase, Mock(return_value=smtp))
        self.assertEqual(self.request('POST', path + '/send', {'send_id': preview['id']})[0], 400)
        self.request('POST', path + '/confirm-send', {'send_id': preview['id'], 'fingerprint': preview['fingerprint'], 'confirmed': True})
        _, receipt = self.request('POST', path + '/send', {'send_id': preview['id']})
        self.assertEqual(receipt['status'], 'accepted')
        raw = smtp.data.call_args.args[0]
        mime = BytesParser(policy=policy.default).parsebytes(raw)
        self.assertEqual(mime.get_body().get_content().strip(), edited['body'])
        self.assertIsNone(mime['Bcc'])
        self.assertEqual(next(mime.iter_attachments()).get_payload(decode=True), b'fixture attachment')
        self.request('POST', path + '/send', {'send_id': preview['id']})
        smtp.data.assert_called_once()
        self.restart()
        _, restored = self.request('GET', path)
        self.assertEqual(restored['send_records'][0]['content']['body'], edited['body'])
        self.assertEqual(restored['send_records'][0]['attempts'], 1)
        self.assertEqual(restored['send_records'][0]['status'], 'accepted')
        archived = self.web.sender.get(task['task_id'], preview['id'])
        checks, frozen = audit(archived)
        self.assertTrue(all(checks.values()))
        self.assertEqual(frozen, raw)
        archived['content']['subject'] = 'tampered'
        checks, _ = audit(archived)
        self.assertFalse(checks['content_fingerprint'])
        self.assertFalse(checks['subject'])
        self.assertFalse(list((self.f.root/'personal').glob('supplement-*')))

    def test_api_failure_then_resume_keeps_task(self):
        def unavailable(messages): raise AppError('API_TIMEOUT', '模拟超时')
        self.f.responses([unavailable])
        _, failed = self.request('POST', '/api/tasks', {'snapshot': self.f.path.name, 'goal': '简单回复'})
        self.assertEqual(failed['status'], 'failed')
        self.f.responses([{'facts': [], 'decisions': [], 'blockers': []}, {'body': '收到，谢谢。', 'used_sources': []}])
        _, resumed = self.request('POST', '/api/tasks/' + failed['task_id'] + '/resume', {})
        self.assertEqual(resumed['task_id'], failed['task_id'])
        self.assertEqual(resumed['status'], 'draft_ready')

    def test_expired_mail_login_preserves_existing_task(self):
        task = self.create()
        with patch('mail_web.Reader', side_effect=MailError('AUTH_FAILED')):
            self.assertEqual(self.request('GET', '/api/inbox')[0], 503)
        _, after = self.request('GET', '/api/tasks/' + task['task_id'])
        self.assertEqual(after['status'], 'waiting_input')
        self.assertEqual(after['facts'][0]['child_id'], task['facts'][0]['child_id'])

    def test_lost_local_ack_after_smtp_acceptance_recovers_unknown(self):
        task = self.f.finish(); sender = self.web.sender
        preview = sender.preview(task['task_id'], 1)
        sender.confirm(task['task_id'], preview['id'], preview['fingerprint'])
        sender.transport = Mock(return_value={'status': 'accepted', 'smtp_code': 250})
        original_save = sender.save
        def fail_final_write(record):
            if record['status'] == 'accepted': raise OSError('simulated storage failure')
            original_save(record)
        with patch.object(sender, 'save', side_effect=fail_final_write):
            with self.assertRaises(OSError): sender.send(task['task_id'], preview['id'])
        self.restart()
        self.assertEqual(self.web.sender.get(task['task_id'], preview['id'])['status'], 'unknown')
        transport = Mock(); self.web.sender.transport=transport
        self.web.sender.send(task['task_id'], preview['id']); transport.assert_not_called()

    def test_browser_disconnect_does_not_cancel_committed_work(self):
        entered, release, finished = threading.Event(), threading.Event(), threading.Event()
        def plan(messages):
            entered.set(); release.wait(4)
            return {'facts': [], 'decisions': [], 'blockers': []}
        self.f.responses([plan, {'body': '收到，谢谢。', 'used_sources': []}])
        dispatch = self.web.dispatch
        def observed(*args):
            try: return dispatch(*args)
            finally: finished.set()
        self.web.dispatch = observed
        client = HTTPConnection('127.0.0.1', self.server.server_port, timeout=5)
        client.request('POST', '/api/tasks', json.dumps({'snapshot':self.f.path.name,'goal':'断线场景回复'}),
                       {'Authorization':'Bearer '+self.web.token,'Content-Type':'application/json'})
        try:
            self.assertTrue(entered.wait(3)); client.close(); release.set()
            self.assertTrue(finished.wait(4))
        finally:
            release.set(); client.close()
        _, tasks = self.request('GET', '/api/tasks')
        self.assertEqual(len(tasks), 1)
        _, restored = self.request('GET', '/api/tasks/' + tasks[0]['task_id'])
        self.assertEqual(restored['status'], 'draft_ready')


if __name__ == '__main__': unittest.main()
