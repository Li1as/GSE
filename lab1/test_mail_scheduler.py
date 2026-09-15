import tempfile
import unittest
from pathlib import Path

from assistant import Assistant
from mail_classifier import Classifier
from mail_monitor import Monitor
from mail_pipeline import Pipeline
from mail_scheduler import Scheduler
from mail_tasks import MailTasks
from test_assistant import ScriptedClient
from test_mail_classifier import Client, message, result
from test_mail_monitor import CONFIG, FakeReader


class SchedulerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.reader = FakeReader()
        self.clock = [1000]
        self.monitor = Monitor(CONFIG, self.root/'monitor', lambda config: self.reader,
                               lambda: self.clock[0])
        personal = self.root/'personal'
        personal.mkdir()
        (personal/'empty.md').write_text('# empty\n')
        self.config = {'model': 'test', 'personal_data_dir': str(personal), 'max_steps': 8}

    def app(self, client):
        classifier = Classifier(self.monitor.inbox, client, lambda: self.clock[0])
        tasks = MailTasks(Assistant(self.config, client, self.root/'tasks.sqlite'))
        return Scheduler(self.monitor, classifier, Pipeline(classifier, tasks), lambda: self.clock[0])

    def test_idle_cycles_make_no_model_calls(self):
        client = Client([])
        app = self.app(client)
        app.cycle()
        self.clock[0] += 60
        result_value = app.cycle()
        self.assertEqual(client.calls, [])
        self.assertEqual((result_value['classified'], result_value['dispatched']), (0, 0))

    def test_bounded_cycles_drain_backlog_without_old_items_consuming_limit(self):
        self.reader.messages = {uid: message(uid, '通知，无需回复。') for uid in range(1, 4)}
        self.reader.upper = 3
        self.monitor.once(include_existing=True)
        client = Client([result('no_reply', '') for _ in range(3)])
        app = self.app(client)
        observed = []
        for _ in range(3):
            value = app.cycle(classify_limit=1, dispatch_limit=1)
            observed.append((value['classified'], value['dispatched']))
            self.clock[0] += 60
        self.assertEqual(observed, [(1, 1)] * 3)
        calls = len(client.calls)
        idle = app.cycle(classify_limit=1, dispatch_limit=1)
        self.assertEqual((idle['classified'], idle['dispatched']), (0, 0))
        self.assertEqual(len(client.calls), calls)

    def test_pause_is_persistent_and_force_cycle_does_not_resume(self):
        client = Client([])
        app = self.app(client)
        app.pause()
        self.assertEqual(app.cycle()['skipped'], 'paused')
        app.cycle(force=True)
        self.assertEqual(app.status()['scheduler']['status'], 'paused')
        rebuilt = self.app(client)
        self.assertEqual(rebuilt.status()['scheduler']['status'], 'paused')
        self.assertEqual(rebuilt.resume()['scheduler']['status'], 'active')

    def test_monitor_retry_is_reported_as_attention_not_success(self):
        client = Client([])
        app = self.app(client)
        self.monitor.once()
        self.reader.errors[1] = OSError('offline')
        self.reader.messages = {1: message(1, '通知')}
        self.reader.upper = 1
        value = app.cycle()
        self.assertEqual(value['scheduler']['status'], 'attention')
        self.assertIsNone(value['scheduler']['last_success'])

    def test_queue_status_exposes_classification_without_private_body(self):
        self.reader.messages = {1: message(1, '通知，无需回复。', '测试通知')}
        self.reader.upper = 1
        self.monitor.once(include_existing=True)
        app = self.app(Client([result('no_reply', '')]))
        app.cycle()
        item = app.items()[0]
        self.assertEqual(item['category'], 'no_reply')
        self.assertNotIn('body', item)
        self.assertIsNone(item['task_id'])


if __name__ == '__main__':
    unittest.main()
