import json
import tempfile
import unittest
from email.message import EmailMessage
from pathlib import Path
from unittest.mock import Mock

from assistant import Assistant, AppError
from mail_classifier import Classifier
from mail_monitor import Monitor
from mail_pipeline import Pipeline, style_for
from mail_send import SendService
from mail_tasks import MailTasks
from test_assistant import ScriptedClient
from test_mail_classifier import Client, result
from test_mail_monitor import CONFIG, FakeReader


PLAN = {'facts': [], 'decisions': [], 'blockers': []}
DRAFT = {'body': '您好，\n\n已收到来信，谢谢。', 'used_sources': []}


class RecordingClient(ScriptedClient):
    def __init__(self, responses):
        super().__init__(responses)
        self.calls = []

    def complete(self, messages):
        self.calls.append(messages)
        return super().complete(messages)


def raw(uid, body='请回复确认收到。', subject='新邮件', parent=None):
    message = EmailMessage()
    message['From'] = 'sender@example.test'
    message['To'] = CONFIG['address']
    message['Subject'] = subject
    message['Message-ID'] = '<p{}@example.test>'.format(uid)
    if parent:
        message['In-Reply-To'] = '<p{}@example.test>'.format(parent)
        message['References'] = '<p{}@example.test>'.format(parent)
    message.set_content(body)
    return message.as_bytes()


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.reader = FakeReader()
        self.reader.messages = {1: raw(1)}
        self.reader.upper = 1
        self.monitor = Monitor(CONFIG, self.root/'monitor', lambda config: self.reader)
        self.monitor.once(include_existing=True)
        self.classifier = Classifier(self.monitor.inbox, Client([
            result('reply_required', '请回复确认收到。', suggested_goal='确认收到邮件')]))
        self.classifier.once()
        personal = self.root/'personal'
        personal.mkdir()
        (personal/'facts.md').write_text('# 空资料\n\n无本次所需事实。')
        self.assistant = Assistant({'model': 'test', 'personal_data_dir': str(personal), 'max_steps': 8},
                                   RecordingClient([PLAN, DRAFT]), self.root/'tasks.sqlite')
        self.tasks = MailTasks(self.assistant)
        self.pipeline = Pipeline(self.classifier, self.tasks)

    def test_reply_classification_creates_one_bound_task(self):
        first = self.pipeline.once()
        self.assertEqual(len(first), 1)
        task = self.tasks.get(first[0]['task_id'])
        self.assertEqual(task['status'], 'draft_ready')
        self.assertEqual(task['source_id'], first[0]['source_id'])
        self.assertEqual(task['style']['language'], 'zh')
        self.assertEqual(task['style']['tone'], 'neutral_formal')
        self.assertEqual(task['style']['context_basis'], 'default_no_history')
        self.assertEqual(self.pipeline.once()[0]['task_id'], task['task_id'])
        with self.tasks.assistant.db_path.parent.joinpath('tasks.sqlite').open('rb'):
            pass
        import sqlite3
        with sqlite3.connect(self.tasks.db_path) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM mail_tasks').fetchone()[0], 1)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM mail_sources').fetchone()[0], 1)

    def test_draft_prompt_contains_style_and_no_inferred_relation(self):
        self.pipeline.once()
        draft_call = self.assistant.client.calls[-1]
        self.assertIn('use_only_explicit_names_and_titles', draft_call[-1]['content'])
        self.assertIn('不推断老师、学长、同事', draft_call[0]['content'])
        self.assertIn('默认不使用表情', draft_call[0]['content'])

    def test_no_reply_and_user_review_do_not_create_tasks(self):
        for category in ('no_reply', 'user_review'):
            with self.subTest(category=category):
                root = self.root/category
                reader = FakeReader(); reader.messages = {1: raw(1)}; reader.upper = 1
                monitor = Monitor(CONFIG, root/'monitor', lambda config: reader)
                monitor.once(include_existing=True)
                classifier = Classifier(monitor.inbox, Client([result(category, '')]))
                classifier.once()
                tasks = MailTasks(Assistant(self.assistant.config, ScriptedClient([]), root/'tasks.sqlite'))
                output = Pipeline(classifier, tasks).once()
                self.assertIsNone(output[0]['task_id'])
                import sqlite3
                with sqlite3.connect(tasks.db_path) as db:
                    self.assertEqual(db.execute('SELECT COUNT(*) FROM mail_tasks').fetchone()[0], 0)

    def test_repeated_source_with_changed_goal_reuses_task(self):
        first = self.pipeline.once()[0]
        row = self.classifier.rows()[0]
        payload = row['payload']
        payload['current']['suggested_goal'] = '另一个自动目标'
        payload['history'].append(payload['model_result'])
        payload['current']['kind'] = 'user_correction'
        self.classifier._save('1', 1, 'corrected', row['attempts'], payload=payload)
        self.assistant.client = ScriptedClient([PLAN, DRAFT])
        second = self.pipeline.once()[0]
        self.assertEqual(second['task_id'], first['task_id'])
        task = self.tasks.get(first['task_id'])
        self.assertEqual(task['goal'], '另一个自动目标')
        self.assertTrue(task['plan_history'])
        self.assertGreaterEqual(len(task['drafts']), 2)

    def test_manual_entry_after_source_binding_reuses_same_task(self):
        created = self.pipeline.once()[0]
        self.assistant.client = ScriptedClient([])
        manual = self.tasks.create(Path(self.monitor.inbox.rows()[0]['snapshot']), '不同的手动目标')
        self.assertEqual(manual['task_id'], created['task_id'])

    def test_correction_to_no_reply_pauses_existing_task_and_send(self):
        created = self.pipeline.once()[0]
        row = self.classifier.rows()[0]
        payload = row['payload']
        payload['history'].append(payload['current'])
        payload['current'] = {'category': 'no_reply', 'reason': '用户确认无需回复',
                              'kind': 'user_correction', 'suggested_goal': '',
                              'decision_question': '', 'corrected_at': 'now'}
        self.classifier._save('1', 1, 'corrected', row['attempts'], payload=payload)
        updated = self.pipeline.once()[0]
        self.assertEqual(updated['task_id'], created['task_id'])
        self.assertTrue(self.tasks.get(created['task_id'])['automation_paused'])
        sender = SendService(self.tasks, self.root, {'address': CONFIG['address']}, Mock())
        with self.assertRaises(AppError):
            sender.preview(created['task_id'], 1)

    def test_crash_after_atomic_link_recovers_same_task(self):
        row = self.classifier.rows()[0]
        payload = row['payload']; current = payload['current']
        source = self.pipeline._binding(row, current, payload)
        style = style_for(self.classifier._source(self.monitor.inbox.rows()[0])[0], False)
        original = self.tasks.resume
        self.tasks.resume = Mock(side_effect=SystemExit('injected'))
        with self.assertRaises(SystemExit):
            self.tasks.create(Path(self.monitor.inbox.rows()[0]['snapshot']), current['suggested_goal'],
                              source=source, style=style)
        linked = self.tasks.source_task(source['source_id'])
        self.assertIsNotNone(linked)
        self.tasks.resume = original
        task = self.tasks.create(Path(self.monitor.inbox.rows()[0]['snapshot']), current['suggested_goal'],
                                 source=source, style=style)
        self.assertEqual(task['task_id'], linked)

    def test_adopts_existing_manual_task_with_same_imap_identity(self):
        snapshot = Path(self.monitor.inbox.rows()[0]['snapshot'])
        manual = self.tasks.create(snapshot, '手动目标')
        self.assistant.client = ScriptedClient([])
        linked = self.pipeline.once()[0]
        self.assertEqual(linked['task_id'], manual['task_id'])
        self.assertEqual(self.tasks.source_task(linked['source_id']), manual['task_id'])
        self.assertFalse(self.tasks.get(manual['task_id']).get('conversation_review_required', False))

    def test_distinct_uid_with_same_content_gets_distinct_source(self):
        first = self.pipeline.once()[0]
        self.reader.messages[2] = self.reader.messages[1]
        self.reader.upper = 2
        self.monitor.once()
        self.classifier.client = Client([result('reply_required', '请回复确认收到。',
                                                suggested_goal='确认第二封')])
        self.classifier.once()
        self.assistant.client = ScriptedClient([PLAN, DRAFT])
        outputs = self.pipeline.once()
        second = next(item for item in outputs if item['source_id'] != first['source_id'])
        self.assertNotEqual(second['task_id'], first['task_id'])

    def test_thread_context_changes_style_basis(self):
        self.reader.messages = {1: raw(1, '此前邮件。'), 2: raw(2, '请回复确认收到。', parent=1)}
        self.reader.upper = 2
        root = self.root/'thread'
        monitor = Monitor(CONFIG, root/'monitor', lambda config: self.reader)
        monitor.once(include_existing=True)
        classifier = Classifier(monitor.inbox, Client([
            result('no_reply', ''),
            result('reply_required', '请回复确认收到。', suggested_goal='回复确认')]))
        classifier.once()
        tasks = MailTasks(Assistant(self.assistant.config, ScriptedClient([PLAN, DRAFT]), root/'tasks.sqlite'))
        output = Pipeline(classifier, tasks).once()
        task = tasks.get(next(item['task_id'] for item in output if item['task_id']))
        self.assertEqual(task['style']['context_basis'], 'thread')
        self.assertEqual(len(task['history']), 1)

    def test_ambiguous_language_requires_one_time_decision(self):
        parsed = {'body': '请确认 this mixed language reply style please 确认语言和正式程度'}
        style = style_for(parsed, False)
        self.assertEqual(style['language'], 'auto')
        self.assertIn('decision_question', style)

    def test_model_style_paraphrase_is_replaced_by_exact_single_question(self):
        mixed = '请确认这是一封中英文混合语言邮件 please confirm this mixed language response style'
        self.reader.messages = {1: raw(1, mixed)}
        self.reader.upper = 1
        root = self.root/'ambiguous'
        monitor = Monitor(CONFIG, root/'monitor', lambda config: self.reader)
        monitor.once(include_existing=True)
        classifier = Classifier(monitor.inbox, Client([
            result('reply_required', mixed,
                   suggested_goal='回复确认')]))
        classifier.once()
        assistant = Assistant(self.assistant.config, ScriptedClient([{
            'facts': [],
            'decisions': [{'question': '请确认用中文还是英文以及正式程度。', 'kind': 'choice'}],
            'blockers': []}]), root/'tasks.sqlite')
        tasks = MailTasks(assistant)
        task = Pipeline(classifier, tasks).once()[0]
        stored = tasks.get(task['task_id'])
        self.assertEqual(len(stored['decisions']), 1)
        self.assertEqual(stored['decisions'][0]['question'], stored['style']['decision_question'])

    def test_english_and_empty_language_defaults(self):
        self.assertEqual(style_for({'body': 'Please confirm receipt of this message.'}, False)['language'], 'en')
        self.assertEqual(style_for({'body': '12345'}, False)['language'], 'auto')
        self.assertIn('decision_question', style_for({'body': '12345'}, False))

    def add_reply(self, category='no_reply', goal=''):
        self.reader.messages[2] = raw(2, '补充说明：请以这封邮件为准。', 'Re: 新邮件', parent=1)
        self.reader.upper = 2
        self.monitor.once()
        self.classifier.client = Client([result(category, '补充说明：请以这封邮件为准。',
                                                suggested_goal=goal)])
        self.classifier.once()
        return self.pipeline.once()

    def test_new_related_mail_invalidates_preview_and_ignore_preserves_edit(self):
        created = self.pipeline.once()[0]
        task = self.tasks.get(created['task_id'])
        task = self.tasks.edit(task['task_id'], task['draft']['version'], '用户亲自修改的正文',
                               task['draft']['subject'], task['draft']['to'])
        sender = SendService(self.tasks, self.root,
                             {'address': CONFIG['address'], 'password': 'x'}, Mock())
        preview = sender.preview(task['task_id'], task['draft']['version'])
        self.add_reply()
        changed = self.tasks.get(task['task_id'])
        self.assertEqual(changed['status'], 'needs_review')
        self.assertEqual(changed['freshness'], 'stale')
        self.assertEqual(changed['draft']['body'], '用户亲自修改的正文')
        self.assertEqual(len([x for x in changed['conversation_updates'] if x['status'] == 'pending']), 1)
        with self.assertRaises(AppError):
            sender.confirm(task['task_id'], preview['id'], preview['fingerprint'])
        update = changed['conversation_updates'][0]
        ignored = self.tasks.review_conversation_update(task['task_id'], update['source_id'], 'ignore')
        self.assertEqual(ignored['status'], 'draft_ready')
        self.assertEqual(ignored['freshness'], 'current')
        self.assertEqual(ignored['draft']['body'], '用户亲自修改的正文')
        self.assertGreater(ignored['draft']['version'], task['draft']['version'])

    def test_include_new_mail_replans_with_prior_user_draft(self):
        created = self.pipeline.once()[0]
        task = self.tasks.get(created['task_id'])
        task = self.tasks.edit(task['task_id'], task['draft']['version'], '请保留这句用户措辞。',
                               task['draft']['subject'], task['draft']['to'])
        self.add_reply('reply_required', '按最新补充重新回复')
        changed = self.tasks.get(task['task_id'])
        update = changed['conversation_updates'][0]
        client = RecordingClient([PLAN, {'body': '请保留这句用户措辞，并确认最新补充。', 'used_sources': []}])
        self.assistant.client = client
        included = self.tasks.review_conversation_update(task['task_id'], update['source_id'], 'include')
        self.assertEqual(included['status'], 'draft_ready')
        self.assertEqual(included['mail']['uid'], '2')
        self.assertEqual(included['history'][0]['uid'], '1')
        self.assertEqual(included['prior_user_draft']['body'], '请保留这句用户措辞。')
        self.assertIn('prior_user_draft', client.calls[-1][-1]['content'])

    def test_related_update_reuses_one_task_without_model_call(self):
        created = self.pipeline.once()[0]
        calls = len(self.assistant.client.calls)
        outputs = self.add_reply('reply_required', '处理补充邮件')
        self.assertTrue(any(item['task_id'] == created['task_id'] for item in outputs))
        self.assertEqual(len(self.assistant.client.calls), calls)
        import sqlite3
        with sqlite3.connect(self.tasks.db_path) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM mail_tasks').fetchone()[0], 1)

    def test_unknown_delivery_in_related_closed_task_blocks_new_task(self):
        first = self.pipeline.once()[0]
        sender = SendService(self.tasks, self.root,
                             {'address': CONFIG['address'], 'password': 'x'},
                             Mock(return_value={'status': 'accepted', 'stage': 'data'}))
        task = self.tasks.get(first['task_id'])
        preview = sender.preview(task['task_id'], task['draft']['version'])
        sender.confirm(task['task_id'], preview['id'], preview['fingerprint'])
        sender.send(task['task_id'], preview['id'])
        self.assistant.client = RecordingClient([PLAN, DRAFT])
        outputs = self.add_reply('reply_required', '回复最新邮件')
        second_id = next(item['task_id'] for item in outputs if item['task_id'] != first['task_id'])
        second = self.tasks.get(second_id)
        self.assertEqual(second['thread_id'], self.tasks.get(first['task_id'])['thread_id'])
        record = sender.get(task['task_id'], preview['id'])
        record['status'] = 'unknown'
        sender.save(record)
        # Even a distinct later task in the same thread cannot bypass an unknown result.
        with self.assertRaises(AppError):
            sender.preview(second_id, second['draft']['version'])


if __name__ == '__main__':
    unittest.main()
