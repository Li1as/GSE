import imaplib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from mail_monitor import Monitor
from mail_reader import MailError, Reader, parse_message, save_snapshot
from persistence import AppError
from test_mail_reader import sample


CONFIG = {'address': 'student@example.test', 'password': 'fake',
          'imap_host': 'example.test', 'imap_port': 993}


class FakeReader:
    def __init__(self):
        self.validity = '1'
        self.upper = 0
        self.messages = {}
        self.fetches = []
        self.searches = []
        self.errors = {}
        self.closed = 0

    def select(self, folder):
        assert folder == 'INBOX'

    def uid_next(self):
        return self.upper + 1

    def uid_range(self, start, end):
        self.searches.append((start, end))
        return [uid for uid in self.messages if start <= uid <= end]

    def add(self, *uids):
        for uid in uids:
            self.messages[uid] = sample()
            self.upper = max(uid, self.upper)

    def fetch(self, uid):
        uid = int(uid)
        self.fetches.append(uid)
        if uid in self.errors:
            raise self.errors[uid]
        if uid not in self.messages:
            raise MailError('missing')
        raw = self.messages[uid]
        parsed = parse_message(raw)
        parsed.update(account=CONFIG['address'], folder='INBOX', uidvalidity=self.validity, uid=str(uid))
        return parsed, raw

    def close(self):
        self.closed += 1


class MonitorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.reader = FakeReader()
        self.time = 1000
        self.factory = Mock(side_effect=lambda config: self.reader)
        self.app = self.new()

    def new(self, hook=lambda event: None):
        return Monitor(CONFIG, self.root, self.factory, lambda: self.time, hook)

    def rows(self):
        return self.app.inbox.rows()

    def test_first_run_skips_history_then_collects_new(self):
        self.reader.add(1, 5)
        self.app.once()
        self.assertEqual(self.rows(), [])
        self.reader.add(6)
        self.app.once()
        self.assertEqual([(r['uid'], r['status']) for r in self.rows()], [(6, 'ready')])
        self.assertEqual(self.reader.fetches, [6])

    def test_empty_baseline_then_arrival(self):
        self.app.once()
        self.assertEqual(self.app.inbox.state()['cursor'], 0)
        self.reader.add(1)
        self.new().once()
        self.assertEqual(self.rows()[0]['status'], 'ready')

    def test_backlog_larger_than_batch_and_uid_holes(self):
        self.reader.add(1, 3, 5, 7, 9)
        for _ in range(5):
            self.new().once(batch=1, span=3, include_existing=True)
        self.assertEqual([r['uid'] for r in self.rows()], [1, 3, 5, 7, 9])
        self.assertTrue(all(r['status'] == 'ready' for r in self.rows()))
        self.assertEqual(self.reader.fetches, [1, 3, 5, 7, 9])
        self.new().once(include_existing=True)
        self.assertEqual(len(self.reader.fetches), 5)

    def test_empty_numeric_window_advances(self):
        self.reader.add(8)
        self.app.once(span=3, include_existing=True)
        self.assertEqual(self.app.inbox.state()['cursor'], 3)
        self.assertEqual(self.rows(), [])
        self.new().once(span=3)
        self.new().once(span=3)
        self.assertEqual(self.rows()[0]['uid'], 8)

    def test_out_of_range_server_results_are_filtered(self):
        self.reader.add(4)
        self.app.once()
        self.reader.add(5)
        self.reader.uid_range = lambda start, end: [4, 5, 5, 6]
        self.app.once()
        self.assertEqual([r['uid'] for r in self.rows()], [5])

    def crash(self, target):
        def hook(event):
            if event == target:
                raise SystemExit('injected')
        return hook

    def test_transaction_rollback_does_not_skip_discovered_uid(self):
        self.reader.add(1)
        with self.assertRaises(SystemExit):
            self.new(self.crash('before_cursor_commit')).once(include_existing=True)
        self.assertEqual(self.rows(), [])
        self.assertEqual(self.app.inbox.state()['cursor'], 0)
        self.new().once()
        self.assertEqual(self.rows()[0]['status'], 'ready')

    def test_restart_after_cursor_commit_drains_queue(self):
        self.reader.add(1)
        with self.assertRaises(SystemExit):
            self.new(self.crash('after_cursor_commit')).once(include_existing=True)
        self.assertEqual(self.rows()[0]['status'], 'pending')
        self.new().once()
        self.assertEqual(self.rows()[0]['status'], 'ready')

    def test_restart_after_fetch_claim(self):
        self.reader.add(1)
        with self.assertRaises(SystemExit):
            self.new(self.crash('before_fetch')).once(include_existing=True)
        self.assertEqual(self.rows()[0]['status'], 'fetching')
        self.new().once()
        self.assertEqual(self.rows()[0]['status'], 'ready')

    def test_complete_snapshot_recovers_without_refetch(self):
        self.reader.add(1)
        with self.assertRaises(SystemExit):
            self.new(self.crash('after_snapshot')).once(include_existing=True)
        self.new().once()
        self.assertEqual(self.reader.fetches, [1])
        self.assertEqual(self.rows()[0]['status'], 'ready')

    def test_partial_snapshot_is_refetched(self):
        self.reader.add(1)
        with self.assertRaises(SystemExit):
            self.new(self.crash('after_snapshot')).once(include_existing=True)
        next(self.app.snapshots.glob('*/message.json')).write_text('{}')
        self.new().once()
        self.assertEqual(self.reader.fetches, [1, 1])
        self.assertEqual(self.rows()[0]['status'], 'ready')

    def test_bad_messages_do_not_block_later_messages(self):
        self.reader.add(1, 2, 3, 4)
        self.reader.errors[1] = MailError('large', 'TOO_LARGE')
        self.reader.errors[2] = ValueError('bad encoding')
        self.reader.errors[3] = MailError('gone')
        self.app.once(include_existing=True)
        self.assertEqual([r['status'] for r in self.rows()], ['needs_review', 'needs_review', 'retry', 'ready'])
        for _ in range(2):
            self.time += 1000
            self.new().once()
        self.assertEqual(self.rows()[2]['status'], 'needs_review')
        self.assertEqual(self.reader.fetches.count(3), 3)

    def test_network_backoff_then_recovery(self):
        self.reader.add(1, 2)
        self.reader.errors[1] = OSError('private server detail')
        self.app.once(include_existing=True)
        self.assertEqual(self.app.inbox.state()['status'], 'retry_wait')
        calls = self.factory.call_count
        self.new().once()
        self.assertEqual(self.factory.call_count, calls)
        self.time += 31
        del self.reader.errors[1]
        self.new().once()
        self.assertTrue(all(r['status'] == 'ready' for r in self.rows()))
        self.assertNotIn('private server detail', json.dumps(self.app.inbox.summary()))

    def test_auth_pause_requires_explicit_resume(self):
        self.factory.side_effect = MailError('private credential', 'AUTH_FAILED')
        self.app.once()
        self.assertEqual(self.app.inbox.state()['status'], 'paused')
        self.time += 10000
        self.new().once()
        self.assertEqual(self.factory.call_count, 1)
        self.factory.side_effect = lambda config: self.reader
        self.new().once(resume=True)
        self.assertEqual(self.app.inbox.state()['status'], 'active')

    def test_uidvalidity_change_never_fetches_old_uid(self):
        self.reader.add(1, 2)
        self.app.once(batch=1, include_existing=True)
        self.reader.validity = '2'
        self.new().once()
        self.assertEqual(self.app.inbox.state()['status'], 'validity_changed')
        self.assertEqual(self.reader.fetches, [1])
        with self.assertRaises(ValueError):
            self.new().once(accept_validity='wrong')
        self.new().once(accept_validity='2')
        self.assertEqual(self.reader.fetches, [1])
        self.assertEqual(len(self.rows()), 2)
        self.reader.add(3)
        self.new().once()
        self.assertEqual([(r['validity'], r['uid']) for r in self.rows()], [('1', 1), ('1', 2), ('2', 3)])

    def test_backfill_explicit_and_idempotent(self):
        self.reader.add(1, 2, 3)
        self.app.once()
        self.new().once(accept_validity='1', backfill=[1, 2])
        self.new().once(accept_validity='1', backfill=[1, 2])
        self.assertEqual(self.reader.fetches, [1, 2])
        self.assertEqual(self.app.inbox.state()['cursor'], 3)
        with self.assertRaises(ValueError):
            self.app.once(backfill=[1, 2])

    def test_concurrent_runner_cannot_enter(self):
        other = self.new()
        with self.app.inbox.task_lock('monitor:' + self.app.inbox.stream):
            with self.assertRaises(AppError):
                other.once()
        self.assertEqual(self.factory.call_count, 0)
        other.once()

    def test_same_validity_uidnext_regression_pauses(self):
        self.reader.add(8)
        self.app.once()
        self.reader.upper = 2
        self.app.once()
        self.assertEqual(self.app.inbox.state()['status'], 'paused')
        self.assertEqual(self.app.inbox.state()['cursor'], 8)

    def test_long_body_preserves_snapshot_for_review(self):
        from email.message import EmailMessage
        message = EmailMessage()
        message.set_content('x'*24001)
        self.reader.add(1)
        self.reader.messages[1] = message.as_bytes()
        self.app.once(include_existing=True)
        row = self.rows()[0]
        self.assertEqual(row['error'], 'BODY_LIMIT')
        self.assertTrue(Path(row['snapshot']).is_dir())

    def test_queue_file_private_and_has_no_credentials(self):
        self.reader.add(1)
        self.app.once(include_existing=True)
        self.assertEqual(self.app.inbox.db_path.stat().st_mode & 0o777, 0o600)
        self.assertNotIn('password', json.dumps(self.app.inbox.summary()))

    def test_manual_retry_retains_identity_and_resets_attempt_budget(self):
        self.reader.add(1)
        self.reader.errors[1] = MailError('large', 'TOO_LARGE')
        self.app.once(include_existing=True)
        self.app.retry(1, '1')
        del self.reader.errors[1]
        self.new().once()
        self.assertEqual(len(self.rows()), 1)
        self.assertEqual(self.rows()[0]['status'], 'ready')
        with self.assertRaises(ValueError):
            self.app.retry(1, '1')
        with self.assertRaises(ValueError):
            self.app.retry(1, 'wrong')

    def test_killed_lock_owner_releases_stream_for_recovery(self):
        import multiprocessing
        parent, child = multiprocessing.Pipe()
        def hold():
            app = Monitor(CONFIG, self.root)
            with app.inbox.task_lock('monitor:' + app.inbox.stream):
                child.send('locked')
                child.recv()
        process = multiprocessing.Process(target=hold)
        process.start()
        try:
            self.assertTrue(parent.poll(5))
            self.assertEqual(parent.recv(), 'locked')
            with self.assertRaises(AppError):
                self.new().once()
        finally:
            process.terminate()
            process.join(5)
            parent.close()
            child.close()
        self.assertFalse(process.is_alive())
        self.assertEqual(self.new().once()['state']['status'], 'active')



class IncrementalReaderTests(unittest.TestCase):
    def test_uidnext_and_search_use_bounded_range(self):
        conn = Mock()
        conn.response.return_value = ('UIDNEXT', [b'11'])
        conn.uid.return_value = ('OK', [b'11 10 9 10 1'])
        reader = Reader(CONFIG, Mock(return_value=conn))
        self.assertEqual(reader.uid_next(), 11)
        self.assertEqual(reader.uid_range(9, 10), [9, 10])
        conn.uid.assert_called_once_with('SEARCH', None, 'UID', '9:10')

    def test_malformed_search_does_not_advance_as_empty(self):
        conn = Mock()
        conn.uid.return_value = ('OK', [b'garbage'])
        reader = Reader(CONFIG, Mock(return_value=conn))
        with self.assertRaises(MailError):
            reader.uid_range(1, 10)
        conn.response.return_value = ('UIDNEXT', [None])
        with self.assertRaises(MailError):
            reader.uid_next()


if __name__ == '__main__':
    unittest.main()
