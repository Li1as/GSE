import json
import tempfile
import unittest
from pathlib import Path

from assistant import AppError, Assistant, Corpus, read_reply_file
from test_assistant import ScriptedClient


def search(word):
    return {'action': 'search_personal_info', 'keywords': [word]}


def missing():
    return {'action': 'final', 'answers': [], 'missing': ['本次活动的饮品偏好'], 'conflicts': []}


def assess(text, relevant=True, conflict=False):
    return {'relevant': relevant, 'conflict': conflict, 'quotes': [text] if relevant else [], 'reason': '测试判断'}


def final(messages):
    chunks = json.loads(messages[-1]['content'])['tool_result']['results']
    row = next(c for c in chunks if '绿茶' in c['text'])
    return {'action': 'final', 'answers': [{'text': '饮品选择绿茶', 'citations': [row['id']]}], 'missing': [], 'conflicts': []}


class SupplementTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.personal = self.root/'personal'
        self.personal.mkdir()
        (self.personal/'resume.md').write_text('# 简历\n\n## 基本信息\n\n喜欢读书。\n', encoding='utf-8')
        self.config = {'model': 'test-model', 'personal_data_dir': str(self.personal), 'max_steps': 8}
        self.app = Assistant(self.config, ScriptedClient([search('饮品'), search('喝什么'), missing()]), self.root/'tasks.sqlite')
        self.task = self.app.query('本次活动准备什么饮品？')
        self.rid = self.task['request_id']
        self.reply = '本次活动饮品选择绿茶。'

    def set_responses(self, responses):
        self.app.client = ScriptedClient(responses)

    def test_request_projection_and_offline_read(self):
        text = (self.root/'requests'/(self.rid+'.md')).read_text()
        self.assertIn(self.task['task_id'], text)
        self.assertIn('饮品', text)
        self.assertIn('reply:start', text)
        self.assertEqual(Assistant({}, db_path=self.root/'tasks.sqlite').get_request(self.rid)['status'], 'pending')

    def test_irrelevant_does_not_unlock_or_archive(self):
        self.set_responses([assess('今天天气不错', False)])
        result = self.app.answer_request(self.rid, '今天天气不错', 'personal')
        self.assertEqual(result['reply_feedback']['status'], 'irrelevant')
        self.assertEqual(self.app.get_task(self.task['task_id'])['status'], 'waiting_input')
        self.assertEqual(self.app.get_request(self.rid)['status'], 'pending')
        self.assertFalse(list(self.personal.glob('supplement-*.md')))

    def test_temporary_resumes_same_task_without_leak(self):
        self.set_responses([assess(self.reply), search('饮品'), final])
        result = self.app.answer_request(self.rid, self.reply, 'task')
        self.assertEqual(result['task_id'], self.task['task_id'])
        self.assertEqual(result['status'], 'completed')
        self.assertTrue(result['result']['evidence'][0]['file'].startswith('task:'))
        self.assertFalse(list(self.personal.glob('supplement-*.md')))
        self.assertEqual(Corpus(self.personal, {}).search(['绿茶'])['total'], 0)
        self.set_responses([search('饮品'), search('喝什么'), missing()])
        other = self.app.query('另一个任务应准备什么饮品？')
        self.assertEqual(other['status'], 'waiting_input')
        self.assertNotIn('supplements', other)

    def test_personal_archive_and_new_query(self):
        self.set_responses([assess(self.reply), search('绿茶'), final])
        result = self.app.answer_request(self.rid, self.reply, 'personal')
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(len(list(self.personal.glob('supplement-*.md'))), 1)
        fresh = Assistant(self.config, ScriptedClient([search('绿茶'), final]), self.root/'tasks.sqlite')
        self.assertEqual(fresh.query('饮品偏好')['status'], 'completed')
        self.assertEqual((self.personal/'resume.md').read_text(), '# 简历\n\n## 基本信息\n\n喜欢读书。\n')

    def test_duplicate_accepted_reply_does_not_repeat(self):
        self.set_responses([assess(self.reply), search('绿茶'), final])
        first = self.app.answer_request(self.rid, self.reply, 'personal')
        self.set_responses([])
        again = self.app.answer_request(self.rid, self.reply, 'personal')
        self.assertEqual(first, again)
        self.assertEqual(len(again['supplements']), 1)
        self.assertEqual(len(list(self.personal.glob('supplement-*.md'))), 1)
        with self.assertRaises(AppError):
            self.app.answer_request(self.rid, '改成红茶', 'personal')

    def test_partial_answer_creates_followup(self):
        self.set_responses([assess(self.reply), search('饮品'), search('数量'), missing()])
        result = self.app.answer_request(self.rid, self.reply, 'task')
        self.assertEqual(result['status'], 'waiting_input')
        self.assertNotEqual(result['request_id'], self.rid)
        self.assertEqual(self.app.get_request(self.rid)['status'], 'answered')

    def test_conflict_requires_explicit_confirmation(self):
        self.set_responses([assess(self.reply, conflict=True)])
        result = self.app.answer_request(self.rid, self.reply, 'personal')
        self.assertEqual(result['reply_feedback']['status'], 'conflict')
        self.assertFalse(list(self.personal.glob('supplement-*.md')))
        self.set_responses([assess(self.reply, conflict=True), search('绿茶'), final])
        result = self.app.answer_request(self.rid, self.reply, 'personal', confirm_conflict=True)
        self.assertEqual(result['status'], 'completed')
        self.assertTrue(result['supplements'][0]['confirmed_conflict'])

    def test_fabricated_quote_not_archived(self):
        self.set_responses([assess('偏好咖啡')])
        with self.assertRaises(AppError) as error:
            self.app.answer_request(self.rid, self.reply, 'personal')
        self.assertEqual(error.exception.code, 'REPLY_PROTOCOL')
        self.assertEqual(self.app.get_request(self.rid)['status'], 'pending')

    def test_validation_timeout_keeps_request_pending(self):
        self.set_responses([AppError('API_TIMEOUT', '超时')])
        with self.assertRaises(AppError):
            self.app.answer_request(self.rid, self.reply, 'personal')
        self.assertEqual(self.app.get_request(self.rid)['status'], 'pending')
        self.assertFalse(list(self.personal.glob('supplement-*.md')))

    def test_resume_failure_retry_uses_accepted_receipt(self):
        self.set_responses([assess(self.reply), AppError('API_TIMEOUT', '超时')])
        failed = self.app.answer_request(self.rid, self.reply, 'personal')
        self.assertEqual(failed['status'], 'failed')
        self.set_responses([search('绿茶'), final])
        result = self.app.answer_request(self.rid, self.reply, 'personal')
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(len(result['supplements']), 1)

    def test_markdown_input_and_scope_validation(self):
        p = self.root/'reply.md'
        p.write_text('说明\n<!-- reply:start -->\n'+self.reply+'\n<!-- reply:end -->\n')
        self.assertEqual(read_reply_file(p), self.reply)
        p.write_text('# 活动\n\n'+self.reply)
        self.assertIn('# 活动', read_reply_file(p))
        with self.assertRaises(AppError):
            self.app.answer_request(self.rid, '', 'task')
        with self.assertRaises(AppError):
            self.app.answer_request(self.rid, self.reply, 'automatic')

    def test_short_answer_retains_field_context(self):
        self.set_responses([assess('绿茶'), search('饮品'), final])
        task = self.app.answer_request(self.rid, '绿茶', 'personal')
        self.assertEqual(task['status'], 'completed')
        chunks = Corpus(self.personal, {}).search(['饮品'])['results']
        self.assertTrue(any('绿茶' in c['text'] and '饮品' in c['text'] for c in chunks))


if __name__ == '__main__':
    unittest.main()
