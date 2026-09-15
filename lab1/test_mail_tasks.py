import json
import tempfile
import unittest
from email.message import EmailMessage
from pathlib import Path

from assistant import Assistant, AppError
from mail_reader import parse_message, save_snapshot
from mail_tasks import MailTasks
from test_assistant import ScriptedClient


PLAN = {'facts': ['验收课程成绩是多少？'], 'decisions': ['是否参加本次活动？'], 'blockers': []}
SEARCH = {'action': 'search_personal_info', 'keywords': ['验收课程']}
MISSING = {'action': 'final', 'answers': [], 'missing': ['验收课程成绩'], 'conflicts': []}
REPLY = '验收课程成绩为 93 分。'


def final(messages):
    chunks = json.loads(messages[-1]['content'])['tool_result']['results']
    row = next(c for c in chunks if '93' in c['text'])
    return {'action': 'final', 'answers': [{'text': REPLY, 'citations': [row['id']]}],
            'missing': [], 'conflicts': []}


def draft(messages):
    data = json.loads(messages[-1]['content'])
    return {'body': '我确认参加。验收课程成绩为 93 分。',
            'used_sources': [data['facts'][0]['evidence'][0]['id']]}


def sample(root, uid='1'):
    msg = EmailMessage()
    msg['From'] = 'teacher@example.test'
    msg['To'] = 'student@example.test'
    msg['Subject'] = '虚构课程活动'
    msg['Message-ID'] = '<%s@example.test>' % uid
    msg.set_content('请回复验收课程成绩并确认是否参加活动。')
    raw = msg.as_bytes()
    parsed = parse_message(raw)
    parsed.update(account='student@example.test', folder='INBOX', uidvalidity='1', uid=uid)
    return save_snapshot(root / 'mail', parsed, raw)


class MailTaskTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        personal = self.root / 'personal'
        personal.mkdir()
        (personal / 'resume.md').write_text('# 简历\n\n喜欢读书。\n')
        self.config = {'model': 'test', 'personal_data_dir': str(personal), 'max_steps': 8}
        self.assistant = Assistant(self.config, ScriptedClient([]), self.root / 'tasks.sqlite')
        self.app = MailTasks(self.assistant)
        self.path = sample(self.root)

    def responses(self, values):
        self.assistant.client = ScriptedClient(values)

    def waiting(self):
        self.responses([PLAN, SEARCH, {'action': 'search_personal_info', 'keywords': ['分数']}, MISSING])
        return self.app.create(self.path, '查询成绩，询问我是否参加，准备回复')

    def supplement(self, task, scope='task'):
        self.responses([{'relevant': True, 'conflict': False, 'quotes': [REPLY], 'reason': '相关'}, SEARCH, final])
        return self.app.answer(task['task_id'], task['facts'][0]['request_id'], REPLY, scope)

    def finish(self):
        task = self.supplement(self.waiting())
        self.responses([{'relevant': True, 'quote': '我确认参加。', 'reason': '明确'}, draft])
        return self.app.decide(task['task_id'], '1', '我确认参加。')

    def test_missing_fact_and_decision_no_early_draft(self):
        task = self.waiting()
        self.assertEqual(task['status'], 'waiting_input')
        self.assertTrue(task['facts'][0]['request_id'])
        self.assertIsNone(task['decisions'][0]['answer'])
        self.assertNotIn('draft', task)

    def test_irrelevant_fact_and_decision_rejected(self):
        task = self.waiting()
        self.responses([{'relevant': False, 'conflict': False, 'quotes': [], 'reason': '无关'}])
        result = self.app.answer(task['task_id'], task['facts'][0]['request_id'], '天气好', 'personal')
        self.assertEqual(result['reply_feedback']['status'], 'irrelevant')
        self.responses([{'relevant': False, 'reason': '无关'}])
        result = self.app.decide(task['task_id'], '1', '天气好')
        self.assertIsNone(result['decisions'][0]['answer'])
        self.assertFalse(list(Path(self.config['personal_data_dir']).glob('supplement-*')))

    def test_supplement_then_decision_draft_and_reopen(self):
        task = self.finish()
        self.assertEqual(task['status'], 'draft_ready')
        self.assertEqual(task['freshness'], 'current')
        self.assertEqual(task['draft']['to'], ['teacher@example.test'])
        self.assertTrue(task['draft']['sources'][0]['evidence'][0]['file'].startswith('task:'))
        self.responses([])
        reopened = MailTasks(Assistant(self.config, self.assistant.client, self.assistant.db_path))
        self.assertEqual(reopened.resume(task['task_id'])['draft'], task['draft'])
        self.assertEqual(reopened.worker(), [])

    def test_temporary_isolation_and_request_ownership(self):
        first = self.supplement(self.waiting())
        self.path = sample(self.root, '2')
        second = self.waiting()
        self.assertNotEqual(first['facts'][0]['child_id'], second['facts'][0]['child_id'])
        self.assertEqual(second['facts'][0]['status'], 'waiting_input')
        self.assertFalse(list(Path(self.config['personal_data_dir']).glob('supplement-*')))
        with self.assertRaises(AppError):
            self.app.answer(second['task_id'], first['facts'][0]['request_id'], REPLY, 'task')

    def test_personal_archive_used_by_new_mail(self):
        self.supplement(self.waiting(), 'personal')
        self.assertEqual(len(list(Path(self.config['personal_data_dir']).glob('supplement-*'))), 1)
        self.responses([PLAN, SEARCH, final])
        task = self.app.create(sample(self.root, '2'), '查询成绩，询问是否参加')
        self.assertEqual(task['facts'][0]['status'], 'completed')
        self.assertEqual(task['status'], 'waiting_input')

    def test_duplicate_create_idle_no_model(self):
        task = self.waiting()
        self.responses([])
        again = self.app.create(self.path, task['goal'])
        self.assertEqual(task['task_id'], again['task_id'])
        self.app.worker()

    def test_external_supplement_worker_resumes_parent(self):
        task = self.waiting()
        self.responses([{'relevant': True, 'quote': '参加', 'reason': '明确'}])
        self.app.decide(task['task_id'], '1', '参加')
        self.responses([{'relevant': True, 'conflict': False, 'quotes': [REPLY], 'reason': '相关'}, SEARCH, final])
        self.assistant.answer_request(task['facts'][0]['request_id'], REPLY, 'task')
        self.responses([draft])
        self.assertEqual(self.app.worker()[0]['status'], 'draft_ready')

    def test_stale_draft_requires_new_query(self):
        task = self.finish()
        (Path(self.config['personal_data_dir']) / 'resume.md').write_text('# 更新\n\n变化。')
        self.assertEqual(self.app.get(task['task_id'])['freshness'], 'stale')
        self.responses([SEARCH, final, draft])
        updated = self.app.resume(task['task_id'])
        self.assertEqual(updated['draft']['version'], 2)
        self.assertEqual(len(updated['drafts']), 2)

    def test_bad_plan_and_failed_draft_recover(self):
        self.responses([{'facts': 'invalid'}])
        task = self.app.create(self.path, '目标')
        self.assertEqual(task['status'], 'failed')
        self.responses([{'facts': [], 'decisions': [], 'blockers': []}, {'body': '草稿', 'used_sources': ['invented']}])
        task = self.app.resume(task['task_id'])
        self.assertEqual(task['status'], 'failed')
        self.assertNotIn('draft', task)
        self.responses([{'body': '已收到通知，谢谢。', 'used_sources': []}])
        self.assertEqual(self.app.resume(task['task_id'])['status'], 'draft_ready')

    def test_blocker_does_not_draft(self):
        self.responses([{'facts': [], 'decisions': [], 'blockers': ['需要附件正文']}])
        task = self.app.create(self.path, '解释附件')
        self.assertEqual(task['status'], 'needs_review')
        self.assertNotIn('draft', task)

    def test_snapshot_mismatch_rejected_before_model(self):
        path = self.path / 'message.json'
        data = json.loads(path.read_text())
        data['body'] = 'changed'
        path.write_text(json.dumps(data))
        with self.assertRaises(AppError):
            self.app.create(self.path, '目标')

    def test_unrelated_history_rejected(self):
        with self.assertRaises(AppError):
            self.app.create(self.path, '目标', [sample(self.root, '2')])

    def test_conflict_child_blocks_draft(self):
        self.responses([PLAN, SEARCH, {'action': 'final', 'answers': [], 'missing': [],
                                     'conflicts': ['成绩有两个口径，需要核对']}])
        task = self.app.create(self.path, '准备回复')
        self.assertEqual(task['status'], 'needs_review')
        self.assertNotIn('draft', task)

    def test_failed_child_not_blindly_retried_by_worker(self):
        self.responses([PLAN, {'action': 'forbidden'}])
        task = self.app.create(self.path, '准备回复')
        self.assertEqual(task['status'], 'failed')
        self.responses([])
        self.assertEqual(self.app.worker(), [])

    def test_interrupted_drafting_recovers_same_children(self):
        task = self.finish()
        child_id = task['facts'][0]['child_id']
        task['status'] = 'drafting'
        del task['draft']
        task['drafts'] = []
        self.app.save(task)
        self.responses([draft])
        recovered = self.app.worker()[0]
        self.assertEqual(recovered['status'], 'draft_ready')
        self.assertEqual(recovered['facts'][0]['child_id'], child_id)

    def test_changed_decision_creates_new_draft_version(self):
        task = self.finish()
        self.responses([{'relevant': True, 'quote': '不参加', 'reason': '明确'},
                        lambda messages: dict(draft(messages), body='本次不参加。')])
        changed = self.app.decide(task['task_id'], '1', '不参加')
        self.assertEqual(changed['draft']['version'], 2)
        self.assertEqual(changed['drafts'][0]['decisions'][0]['answer'], '我确认参加。')
        self.assertEqual(changed['draft']['decisions'][0]['answer'], '不参加')


if __name__ == '__main__':
    unittest.main()
