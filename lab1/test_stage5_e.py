"""Stage 5E isolated repeatable workflow and adapter-extension acceptance."""
import json
import sqlite3
import tempfile
import unittest
from email.message import EmailMessage
from pathlib import Path

from assistant import Assistant
from ehall_submit import EhallSubmitService, fingerprint
from ehall_tasks import EhallTasks
from experience import ExperienceService, ExperienceStore
from mail_classifier import Classifier
from mail_monitor import Monitor
from mail_pipeline import Pipeline
from mail_send import SendService
from mail_tasks import MailTasks
from persistence import AppError
from test_assistant import ScriptedClient
from test_mail_classifier import Client
from test_mail_monitor import CONFIG, FakeReader


RULE_ID = 'mail.notice.date-semantics'
BODY = '报名截止 9 月 20 日，材料提交截止 9 月 22 日，活动于 9 月 25 日开始。请回复确认收到。'


def candidate(rule_id=RULE_ID, domains=None):
    return {
        'rule_id': rule_id,
        'title': '区分通知中的日期角色',
        'domains': domains or ['mail.classify', 'mail.plan', 'mail.draft'],
        'instruction': '报名截止、材料提交截止和活动开始时间不得混用。',
        'examples': ['报名截止 9 月 20 日，活动于 9 月 25 日开始。'],
        'counterexamples': ['正文只有一个未说明用途的日期。'],
        'rationale': '避免把不同日期角色写错。',
    }


def rule_result(domain_conclusion):
    return [{
        'rule_id': RULE_ID,
        'version': 1,
        'applicable': True,
        'evidence_quotes': ['报名截止 9 月 20 日'],
        'conclusion': domain_conclusion,
    }]


def raw_mail():
    message = EmailMessage()
    message['From'] = 'organizer@example.test'
    message['To'] = CONFIG['address']
    message['Subject'] = '活动报名通知'
    message['Message-ID'] = '<stage5e@example.test>'
    message.set_content(BODY)
    return message.as_bytes()


class Stage5EMailWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        personal = self.root / 'personal'
        personal.mkdir()
        (personal / 'profile.md').write_text('# 合成资料\n', encoding='utf-8')
        self.config = {'model': 'test', 'personal_data_dir': str(personal), 'max_steps': 8}
        self.db = self.root / 'tasks.sqlite'
        self.rules = self.root / 'rules'

    def test_repeatable_mail_workflow_survives_restart_and_preserves_audit(self):
        experience = ExperienceStore(self.db, self.rules)
        event = experience.create_event('manual', {'correction': '不同日期角色不能混用'})
        proposal = experience.create_proposal(event['id'], candidate())
        experience.approve(proposal['id'])

        reader = FakeReader()
        reader.messages = {1: raw_mail()}
        reader.upper = 1
        monitor = Monitor(CONFIG, self.root / 'monitor', lambda config: reader)
        monitor.once(include_existing=True)
        classification = {
            'category': 'reply_required', 'reason': '正文明确要求回复确认。',
            'confidence': 'high',
            'action_requests': [{'request': '回复确认收到', 'quote': '请回复确认收到。'}],
            'suggested_goal': '确认收到并准确复述三个日期角色',
            'decision_question': '', 'limitations': [],
            'rule_results': rule_result('分类时保留三个日期角色。'),
        }
        plan = {'facts': [], 'decisions': [], 'blockers': [],
                'rule_results': rule_result('规划时分别处理截止和开始时间。')}
        draft = {'body': '已收到：报名截止为 9 月 20 日，活动于 9 月 25 日开始。',
                 'used_sources': [],
                 'rule_results': rule_result('草稿没有把报名截止写成活动开始。')}
        client = Client([classification, plan, draft])
        classifier = Classifier(monitor.inbox, client, experience=experience)
        assistant = Assistant(self.config, client, self.db)
        tasks = MailTasks(assistant, experience)
        pipeline = Pipeline(classifier, tasks)

        self.assertEqual(len(classifier.once()), 1)
        dispatched = pipeline.once()
        task_id = dispatched[0]['task_id']
        task = tasks.get(task_id)
        self.assertEqual(task['status'], 'draft_ready')
        self.assertEqual(task['experience_snapshot']['mail.plan']['rules'][0]['version'], 1)
        self.assertEqual(len(experience.applications('mail_task', task_id)), 2)

        edited = tasks.edit(task_id, 1,
                            task['draft']['body'] + '\n材料提交截止为 9 月 22 日。',
                            task['draft']['subject'], task['draft']['to'])
        proposal_client = ScriptedClient([candidate('mail.draft.complete-dates', ['mail.draft'])])
        promoted = ExperienceService(experience, proposal_client).from_draft(
            tasks, task_id, edited['draft']['version'], '今后不要遗漏材料提交截止日期。')
        experience.approve(promoted['id'])

        transports = []
        def transport(config, envelope, raw, phase):
            transports.append({'envelope': envelope, 'raw': raw})
            phase('data_started')
            return {'status': 'accepted', 'stage': 'data', 'smtp_code': 250}

        sender = SendService(tasks, self.root, {'address': CONFIG['address']}, transport)
        preview = sender.preview(task_id, 2)
        with self.assertRaises(AppError):
            sender.send(task_id, preview['id'])
        sender.confirm(task_id, preview['id'], preview['fingerprint'])
        accepted = sender.send(task_id, preview['id'])
        self.assertEqual(accepted['status'], 'accepted')
        self.assertEqual(sender.send(task_id, preview['id'])['status'], 'accepted')
        self.assertEqual(len(transports), 1)
        tasks.archive(task_id, {'accepted_send_id': accepted['id']})

        # Repeat processing and rebuild every service around the same durable state.
        monitor.once()
        self.assertEqual(len(monitor.inbox.rows()), 1)
        self.assertEqual(classifier.once(), [])
        self.assertEqual(pipeline.once()[0]['task_id'], task_id)
        with sqlite3.connect(str(self.db)) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM mail_tasks').fetchone()[0], 1)
            self.assertEqual(db.execute('SELECT count(*) FROM mail_sources').fetchone()[0], 1)
            self.assertEqual(db.execute('SELECT count(*) FROM mail_sends').fetchone()[0], 1)

        restarted_experience = ExperienceStore(self.db, self.rules)
        restarted_tasks = MailTasks(
            Assistant(self.config, ScriptedClient([]), self.db), restarted_experience)
        restarted_sender = SendService(
            restarted_tasks, self.root, {'address': CONFIG['address']}, transport)
        restored = restarted_tasks.get(task_id)
        self.assertEqual(len(restored['drafts']), 2)
        self.assertIn(task_id, restarted_tasks.archived_task_ids())
        self.assertEqual(restarted_sender.history(task_id)[0]['status'], 'accepted')
        self.assertEqual(len(restarted_experience.applications('mail_task', task_id)), 2)
        promoted_rule = next(rule for rule in restarted_experience.rules()
                             if rule['id'] == 'mail.draft.complete-dates')
        self.assertEqual(promoted_rule['versions'][0]['source_event_id'], promoted['event_id'])
        self.assertEqual(len(transports), 1)


class SecondExplicitAdapter:
    """A non-production adapter proving the bounded contract is reusable."""
    name = 'synthetic_confirmation'
    version = 1
    transaction_name = '合成确认事务'

    def matches(self, url):
        return '/synthetic-confirmation/' in url

    def inspect(self, snapshot):
        if snapshot != {'page_identity': 'synthetic_confirmation'}:
            raise AppError('PAGE_CHANGED', '合成页面身份不匹配。')
        return {
            'transaction_name': self.transaction_name,
            'page_identity': 'synthetic_confirmation',
            'page_summary': {'choice_count': 1},
            'page_structure': 'synthetic-confirmation-v1',
            'fields': [{
                'id': 'target', 'label': '目标', 'source': 'decision',
                'input': 'choice', 'required': True, 'question': '请选择目标。',
                'options': [{'value': 'one', 'label': '合成目标'}],
            }],
            'blockers': [],
        }

    def normalize_decision(self, field, answer):
        if answer != 'one':
            raise AppError('INPUT_ERROR', '目标无效。')
        return answer

    def fill(self, page, values, files):
        if files or values.get('target') != 'one':
            raise AppError('INPUT_ERROR', '合成试填无效。')
        page['target'] = 'one'
        return {'target': 'one'}

    def read_back(self, page, binding):
        if page.get('target') != binding.get('target'):
            raise AppError('READBACK_MISMATCH', '合成回读不一致。')
        return {'target': page['target']}

    def consequences(self, values):
        return {'action': '提交合成目标', 'warnings': ['仅用于隔离测试。'],
                'submission_mode': 'confirmed_once'}


class SyntheticOperation:
    def __init__(self, outcome='succeeded'):
        self.outcome = outcome
        self.calls = 0

    def prepare(self, content):
        return fingerprint(content)

    def open_confirmation(self):
        return {'prompt': '确认提交合成目标'}

    def submit_once(self):
        self.calls += 1
        result = {'status': self.outcome}
        if self.outcome == 'succeeded':
            result['evidence'] = {'kind': 'synthetic_receipt'}
        return result


class Stage5EAdapterContractTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        personal = root / 'personal'
        personal.mkdir()
        (personal / 'empty.md').write_text('# 合成资料\n', encoding='utf-8')
        assistant = Assistant({'model': 'test', 'personal_data_dir': str(personal),
                               'max_steps': 4}, ScriptedClient([]), root / 'tasks.sqlite')
        self.adapter = SecondExplicitAdapter()
        self.tasks = EhallTasks(assistant, {self.adapter.name: self.adapter})
        self.submit = EhallSubmitService(self.tasks)

    def ready(self):
        task = self.tasks.create(
            'https://ehallapp.nju.edu.cn/synthetic-confirmation/index.do')
        task = self.tasks.inspect(task['task_id'], {
            'page_identity': 'synthetic_confirmation'})
        task = self.tasks.decide(task['task_id'], 'target', 'one')
        page = {}
        binding = self.adapter.fill(page, task['prepared_values'], [])
        values = self.adapter.read_back(page, binding)
        return self.tasks.record_readback(
            task['task_id'], task['field_version'], task['page_structure'], values)

    def test_second_adapter_uses_same_confirm_submit_and_reconcile_boundaries(self):
        task = self.ready()
        preview = self.submit.preview(task['task_id'], task['field_version'])
        self.assertEqual(preview['content']['consequences']['submission_mode'],
                         'confirmed_once')
        with self.assertRaises(AppError):
            self.submit.confirm(task['task_id'], preview['id'], preview['fingerprint'], False)
        self.submit.confirm(task['task_id'], preview['id'], preview['fingerprint'], True)
        operation = SyntheticOperation()
        result = self.submit.execute(task['task_id'], preview['id'], operation)
        self.assertEqual(result['status'], 'succeeded')
        self.assertEqual(operation.calls, 1)
        with self.assertRaises(AppError):
            self.submit.execute(task['task_id'], preview['id'], operation)
        self.assertEqual(operation.calls, 1)

        unknown_task = self.ready()
        unknown_preview = self.submit.preview(
            unknown_task['task_id'], unknown_task['field_version'])
        self.submit.confirm(unknown_task['task_id'], unknown_preview['id'],
                            unknown_preview['fingerprint'], True)
        unknown = self.submit.execute(
            unknown_task['task_id'], unknown_preview['id'], SyntheticOperation('unknown'))
        reconciled = self.submit.reconcile(
            unknown_task['task_id'], unknown['id'],
            lambda submission, frozen: {
                'status': 'succeeded',
                'evidence': {'kind': 'synthetic_read_only_reconciliation'},
            })
        self.assertEqual(reconciled['status'], 'succeeded')


if __name__ == '__main__':
    unittest.main()
