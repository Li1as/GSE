import json
import unittest
from pathlib import Path

import test_mail_tasks as fixtures
from assistant import AppError


class RealScenarioRegression(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.MailTaskTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)

    def test_personal_supplement_directly_available_to_continuation(self):
        task = self.f.waiting()
        def inspect_context(messages):
            data = json.loads(messages[1]['content'])
            self.assertTrue(any('93' in c['text'] and c['file'].startswith('supplement-')
                                for c in data['source_constraints']))
            return fixtures.SEARCH
        self.f.responses([{'relevant': True, 'conflict': False, 'quotes': [fixtures.REPLY], 'reason': '相关'},
                          inspect_context, fixtures.final])
        result = self.f.app.answer(task['task_id'], task['facts'][0]['request_id'], fixtures.REPLY, 'personal')
        self.assertEqual(result['facts'][0]['status'], 'completed')

    def test_replan_preserves_parent_supplement_and_history(self):
        task = self.f.finish()
        old_id = task['facts'][0]['child_id']
        self.f.responses([{'facts': ['只查询验收课程最终成绩'], 'decisions': [], 'blockers': [],
                           'notes': ['只询问后续测试，不需要读取附件']}, fixtures.SEARCH, fixtures.final, fixtures.draft])
        result = self.f.app.replan(task['task_id'])
        self.assertEqual(result['status'], 'draft_ready')
        self.assertNotEqual(result['facts'][0]['child_id'], old_id)
        self.assertEqual(result['plan_history'][-1]['facts'][0]['child_id'], old_id)
        self.assertEqual(result['decisions'][0]['answer'], '我确认参加。')
        child = self.f.assistant.get_task(result['facts'][0]['child_id'])
        self.assertEqual(child['parent_supplements'][0]['text'], fixtures.REPLY)
        self.assertFalse(list(Path(self.f.config['personal_data_dir']).glob('supplement-*')))

    def test_scope_decision_replans(self):
        task = self.f.waiting()
        self.f.responses([{'relevant': True, 'quote': '只提供成绩', 'reason': '范围确定', 'affects_plan': True},
                          {'facts': ['只查询验收课程成绩'], 'decisions': [], 'blockers': []}, fixtures.SEARCH,
                          {'action': 'search_personal_info', 'keywords': ['分数']}, fixtures.MISSING])
        changed = self.f.app.decide(task['task_id'], '1', '只提供成绩')
        self.assertEqual(changed['facts'][0]['question'], '只查询验收课程成绩')
        self.assertEqual(changed['decisions'][0]['answer'], '只提供成绩')
        self.assertEqual(changed['status'], 'waiting_input')

    def test_unresolved_scope_prevents_premature_personal_queries(self):
        self.f.responses([{'facts': ['GPA', '成绩排名'], 'decisions': [
            {'question': '允许提供哪些字段？', 'kind': 'scope'}], 'blockers': []}])
        task = self.f.app.create(self.f.path, '提供学术信息')
        self.assertEqual(task['status'], 'waiting_input')
        self.assertEqual(task['facts'], [])
        self.assertEqual(task['deferred_fact_questions'], ['GPA', '成绩排名'])

    def test_notes_do_not_become_conflicts(self):
        def result(messages):
            final = fixtures.final(messages)
            final['notes'] = ['据用户补充，原简历是历史快照。']
            return final
        task = self.f.waiting()
        self.f.responses([{'relevant': True, 'conflict': False, 'quotes': [fixtures.REPLY], 'reason': '相关'},
                          fixtures.SEARCH, result])
        changed = self.f.app.answer(task['task_id'], task['facts'][0]['request_id'], fixtures.REPLY, 'task')
        self.assertEqual(changed['facts'][0]['status'], 'completed')
        self.assertEqual(changed['facts'][0]['result']['notes'], ['据用户补充，原简历是历史快照。'])

    def test_network_io_not_mislabeled_corpus_error(self):
        def fail(messages):
            raise OSError('secret transport details')
        self.f.responses([fixtures.PLAN, fail])
        result = self.f.app.create(self.f.path, '准备回复')
        error = result['facts'][0]['error']
        self.assertEqual(error['code'], 'API_IO')
        self.assertEqual(error['stage'], 'model_call')
        self.assertNotIn('secret', json.dumps(error))

    def test_blocker_and_failure_both_visible(self):
        self.f.responses([{'facts': ['成绩'], 'decisions': [], 'blockers': [
            {'reason': '需要读取附件', 'resolution': '提供附件正文'}]}, {'action': 'invalid'}])
        task = self.f.app.create(self.f.path, '分析附件与成绩')
        self.assertEqual(task['status'], 'needs_review')
        self.assertEqual(task['issues'][0]['status'], 'failed')
        self.assertTrue(task['facts'][0]['error'])
        self.assertEqual(task['blockers'][0]['resolution'], '提供附件正文')

    def limited_task(self):
        task = self.f.finish()
        child = self.f.assistant.get_task(task['facts'][0]['child_id'])
        child['status'] = 'needs_review'
        child['result']['conflicts'] = ['来源属于存档，仅能按来源口径表述。']
        self.f.assistant.save(child)
        return task, child

    def test_mail_qualification_keeps_original_query_and_source(self):
        task, child = self.limited_task()
        self.f.responses([{'blocking': False, 'reason': '可按来源限定回答',
            'answers': child['result']['answers'], 'notes': ['根据已存档来源。']}, fixtures.draft])
        result = self.f.app.resume(task['task_id'])
        self.assertEqual(result['status'], 'draft_ready')
        self.assertEqual(self.f.assistant.get_task(child['task_id'])['status'], 'needs_review')
        self.assertTrue(result['facts'][0]['qualification'])
        self.assertEqual(result['draft']['sources'][0]['notes'], ['根据已存档来源。'])

    def test_real_conflict_still_blocks_after_review(self):
        task, child = self.limited_task()
        self.f.responses([{'blocking': True, 'reason': '同一事实存在矛盾，无法给出可靠值。'}])
        result = self.f.app.resume(task['task_id'])
        self.assertEqual(result['status'], 'needs_review')
        self.assertEqual(result['facts'][0]['status'], 'needs_review')

    def waiting_with_answer(self, missing):
        task, child = self.limited_task()
        child.update(status='waiting_input')
        child['result']['missing'] = missing
        self.f.assistant.save(child)
        self.f.assistant._ensure_request(child)
        return task, self.f.assistant.get_task(child['task_id'])

    def test_missing_source_note_qualifies_and_hides_old_request(self):
        task, child = self.waiting_with_answer(['需要证实当前状态'])
        ref = child['result']['evidence'][0]['id']
        self.f.responses([{'blocking': False, 'reason': '仅一般介绍，可保留来源说明',
            'answers': child['result']['answers'], 'notes': ['据存档来源'],
            'missing_review': [{'index': 0, 'kind': 'source_note', 'reason': '不要求当前身份认证', 'citations': [ref]}]}, fixtures.draft])
        result = self.f.app.resume(task['task_id'])
        self.assertEqual(result['status'], 'draft_ready')
        self.assertEqual(self.f.app.visible_requests(result), [])
        self.assertEqual(self.f.assistant.get_task(child['task_id'])['status'], 'waiting_input')
        self.assertEqual(self.f.assistant.get_request(child['request_id'])['status'], 'pending')

    def test_partial_missing_review_keeps_required_item(self):
        task, child = self.waiting_with_answer(['历史来源限定', '当前联系电话'])
        ref = child['result']['evidence'][0]['id']
        self.f.responses([{'blocking': True, 'reason': '电话确实缺失', 'missing_review': [
            {'index': 0, 'kind': 'source_note', 'reason': '可说明历史来源', 'citations': [ref]},
            {'index': 1, 'kind': 'required', 'reason': '目标要求而无资料', 'citations': []}]}])
        result = self.f.app.resume(task['task_id'])
        self.assertEqual(result['status'], 'waiting_input')
        self.assertEqual(self.f.app.visible_requests(result)[0]['missing'], ['当前联系电话'])

    def test_missing_review_cannot_skip_item_or_use_fake_citation(self):
        task, child = self.waiting_with_answer(['需要确认'])
        self.f.responses([{'blocking': False, 'reason': '声称可放行', 'answers': child['result']['answers'],
            'notes': ['说明'], 'missing_review': []}])
        self.assertEqual(self.f.app.resume(task['task_id'])['status'], 'failed')
        self.f.responses([{'blocking': False, 'reason': '声称可放行', 'answers': child['result']['answers'],
            'notes': ['说明'], 'missing_review': [{'index': 0, 'kind': 'covered', 'reason': '已知', 'citations': ['fake']}]}])
        self.assertEqual(self.f.app.resume(task['task_id'])['status'], 'failed')

    def test_changed_goal_invalidates_hidden_request(self):
        task, child = self.waiting_with_answer(['需要证实当前状态'])
        ref = child['result']['evidence'][0]['id']
        self.f.responses([{'blocking': False, 'reason': '仅一般介绍', 'answers': child['result']['answers'],
            'notes': ['历史来源'], 'missing_review': [{'index': 0, 'kind': 'source_note', 'reason': '仅说明', 'citations': [ref]}]}, fixtures.draft])
        result = self.f.app.resume(task['task_id'])
        result['goal'] = '必须验证当前在读身份'
        self.assertEqual(len(self.f.app.visible_requests(result)), 1)


if __name__ == '__main__':
    unittest.main()
