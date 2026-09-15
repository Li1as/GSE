import base64
import copy
import threading
import unittest
from email import policy
from email.parser import BytesParser
from pathlib import Path
from unittest.mock import Mock

from assistant import AppError
from mail_send import SendService, deliver
import test_mail_tasks as fixtures


class SendTests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.MailTaskTests(); self.f.setUp(); self.addCleanup(self.f.doCleanups)
        self.task = self.f.finish()
        self.config = {'address': 'student@example.test', 'password': 'private', 'smtp_host': 'example.test', 'smtp_port': 465}
        self.transport = Mock(return_value={'status': 'accepted', 'stage': 'data', 'smtp_code': 250})
        self.sender = SendService(self.f.app, self.f.root, self.config, self.transport)

    def confirmed(self):
        record = self.sender.preview(self.task['task_id'], self.f.app.get(self.task['task_id'])['draft']['version'])
        return self.sender.confirm(self.task['task_id'], record['id'], record['fingerprint'])

    def test_no_confirmation_no_send(self):
        r = self.sender.preview(self.task['task_id'], 1)
        with self.assertRaises(AppError): self.sender.send(self.task['task_id'], r['id'])
        self.transport.assert_not_called()

    def test_exact_frozen_bytes_and_duplicate_click(self):
        r = self.confirmed()
        frozen = self.sender.get(self.task['task_id'], r['id'])
        first = self.sender.send(self.task['task_id'], r['id'])
        self.assertEqual(first['status'], 'accepted')
        self.sender.send(self.task['task_id'], r['id'])
        self.transport.assert_called_once()
        self.assertEqual(self.transport.call_args.args[2], base64.b64decode(frozen['wire_base64']))
        with self.assertRaises(AppError): self.sender.preview(self.task['task_id'], 1)

    def test_edit_invalidates_confirmation(self):
        r = self.confirmed()
        self.f.app.edit(self.task['task_id'], 1, '新正文', '新主题', ['teacher@example.test'])
        with self.assertRaises(AppError): self.sender.send(self.task['task_id'], r['id'])
        self.transport.assert_not_called()

    def test_stale_sources_invalidate_confirmation(self):
        r = self.confirmed()
        (Path(self.f.config['personal_data_dir']) / 'resume.md').write_text('# updated')
        with self.assertRaises(AppError): self.sender.send(self.task['task_id'], r['id'])
        self.transport.assert_not_called()

    def test_attachments_bcc_and_changed_blob(self):
        a = self.sender.upload('测试.txt', base64.b64encode(b'attachment bytes').decode())
        self.f.app.edit(self.task['task_id'], 1, '正文', '主题', ['teacher@example.test'],
                        cc=['copy@example.test'], bcc=['blind@example.test'], attachments=[a])
        r = self.confirmed(); record = self.sender.get(self.task['task_id'], r['id'])
        msg = BytesParser(policy=policy.default).parsebytes(base64.b64decode(record['wire_base64']))
        self.assertIsNone(msg['Bcc'])
        self.assertEqual(next(msg.iter_attachments()).get_payload(decode=True), b'attachment bytes')
        self.assertEqual(r['content']['bcc'], ['blind@example.test'])
        (self.f.root / 'outgoing-blobs' / a['sha256']).write_bytes(b'changed')
        with self.assertRaises(AppError): self.sender.send(self.task['task_id'], r['id'])
        self.transport.assert_not_called()

    def test_unknown_never_retries_or_previews_again(self):
        self.transport.return_value = {'status': 'unknown', 'stage': 'data'}
        r = self.confirmed()
        self.assertEqual(self.sender.send(self.task['task_id'], r['id'])['status'], 'unknown')
        self.sender.send(self.task['task_id'], r['id']); self.transport.assert_called_once()
        with self.assertRaises(AppError): self.sender.preview(self.task['task_id'], 1)

    def test_interrupted_sending_becomes_unknown(self):
        r = self.confirmed(); full = self.sender.get(self.task['task_id'], r['id'])
        full['status'] = 'sending'; full['stage'] = 'data_started'; self.sender.save(full)
        restarted = SendService(self.f.app, self.f.root, self.config, self.transport)
        restarted.recover()
        self.assertEqual(restarted.send(self.task['task_id'], r['id'])['status'], 'unknown')
        self.transport.assert_not_called()

    def test_explicit_failure_requires_new_confirmation(self):
        self.transport.return_value = {'status': 'failed', 'stage': 'rcpt', 'smtp_code': 550}
        r = self.confirmed(); self.sender.send(self.task['task_id'], r['id'])
        self.sender.send(self.task['task_id'], r['id']); self.transport.assert_called_once()
        new = self.sender.preview(self.task['task_id'], 1)
        self.assertNotEqual(new['id'], r['id'])
        self.assertEqual(new['status'], 'preview')

    def test_concurrent_send_cannot_duplicate(self):
        entered, release = threading.Event(), threading.Event()
        def transport(*args):
            entered.set(); release.wait(5)
            return {'status': 'accepted'}
        self.sender.transport = Mock(side_effect=transport)
        r = self.confirmed()
        thread = threading.Thread(target=self.sender.send, args=(self.task['task_id'], r['id']))
        thread.start()
        try:
            self.assertTrue(entered.wait(3))
            with self.assertRaises(AppError) as raised: self.sender.send(self.task['task_id'], r['id'])
            self.assertEqual(raised.exception.code, 'TASK_BUSY')
        finally:
            release.set(); thread.join()
        self.sender.transport.assert_called_once()

    def test_wrong_confirmation_hash_and_account_rejected(self):
        r = self.sender.preview(self.task['task_id'], 1)
        with self.assertRaises(AppError): self.sender.confirm(self.task['task_id'], r['id'], 'wrong')
        self.sender.config = dict(self.config, address='other@example.test')
        with self.assertRaises(AppError): self.sender.preview(self.task['task_id'], 1)

    def test_sent_folder_absence_is_not_failure(self):
        from unittest.mock import patch
        r = self.confirmed()
        self.transport.return_value = {'status': 'unknown'}
        self.sender.send(self.task['task_id'], r['id'])
        with patch('mail_send.Reader') as reader:
            reader.return_value.conn.uid.return_value = ('OK', [b''])
            checked = self.sender.reconcile(self.task['task_id'], r['id'])
        self.assertEqual(checked['status'], 'unknown')
        self.assertEqual(checked['sent_folder_check']['matches'], [])
        self.transport.assert_called_once()


class TransportTests(unittest.TestCase):
    def setup_transport(self):
        smtp = Mock()
        smtp.mail.return_value = (250, b'ok'); smtp.rcpt.return_value = (250, b'ok'); smtp.data.return_value = (250, b'ok')
        return smtp, Mock(return_value=smtp)

    def run_transport(self, factory, phase=None):
        return deliver({'address': 'a@example.test', 'password': 'secret', 'smtp_host': 'example.test', 'smtp_port': 465},
                       {'from': 'a@example.test', 'recipients': ['b@example.test', 'c@example.test']}, b'wire\r\n', phase or Mock(), factory)

    def test_partial_rcpt_refusal_does_not_submit_data(self):
        smtp, factory = self.setup_transport(); smtp.rcpt.side_effect = [(250,b'ok'),(550,b'no')]
        self.assertEqual(self.run_transport(factory)['status'], 'failed')
        smtp.data.assert_not_called()

    def test_disconnect_after_data_is_unknown(self):
        smtp, factory = self.setup_transport(); smtp.data.side_effect = OSError('secret')
        r = self.run_transport(factory)
        self.assertEqual(r['status'], 'unknown'); self.assertNotIn('secret', str(r))

    def test_auth_failure_and_explicit_data_failure_are_known(self):
        import smtplib
        smtp, factory = self.setup_transport(); smtp.login.side_effect = smtplib.SMTPAuthenticationError(535,b'secret')
        self.assertEqual(self.run_transport(factory)['status'], 'failed'); smtp.data.assert_not_called()
        smtp.login.side_effect = None; smtp.data.side_effect = smtplib.SMTPDataError(554,b'no')
        self.assertEqual(self.run_transport(factory)['status'], 'failed')

    def test_acceptance_survives_close_failure_and_marker_precedes_data(self):
        smtp, factory = self.setup_transport(); marked=[]
        smtp.data.side_effect=lambda raw: (self.assertEqual(marked,['data_started']) or (250,b'ok'))
        smtp.close.side_effect=OSError()
        self.assertEqual(self.run_transport(factory,marked.append)['status'],'accepted')


if __name__ == '__main__': unittest.main()
