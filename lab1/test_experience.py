import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from experience import ExperienceService, ExperienceStore
from persistence import AppError


def candidate(rule_id='mail.notice.date-semantics', instruction=None):
    return {
        'rule_id': rule_id,
        'title': '区分通知中的不同时间',
        'domains': ['mail.plan', 'mail.draft'],
        'instruction': instruction or '报名截止时间和活动开始时间是不同语义，不得相互替代。',
        'examples': ['报名截至 9 月 20 日；活动于 9 月 25 日开始。'],
        'counterexamples': ['邮件只给出一个没有角色说明的时间。'],
        'rationale': '避免把截止时间误写成活动开始时间。',
    }


class Client:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def complete(self, messages):
        self.calls.append(messages)
        value = self.responses.pop(0)
        return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)


class Tasks:
    def __init__(self, task):
        self.task = task

    def get(self, task_id):
        if task_id != self.task['task_id']:
            raise AppError('MAIL_NOT_FOUND', '不存在')
        return self.task


class Inbox:
    def rows(self, validity=None):
        return [{'stream': 'stream', 'validity': '1', 'uid': 7, 'status': 'ready'}]


class Classifier:
    def __init__(self, corrected=True):
        self.inbox = Inbox()
        self.corrected = corrected

    def rows(self):
        current = {'category': 'no_reply', 'reason': '这是通知。', 'suggested_goal': '',
                   'decision_question': '', 'kind': 'user_correction'}
        if not self.corrected:
            current.pop('kind')
        return [{'stream': 'stream', 'validity': '1', 'uid': 7,
                 'status': 'corrected' if self.corrected else 'classified',
                 'payload': {'model_result': {'category': 'reply_required'}, 'current': current}}]

    def _source(self, row):
        return {'Subject': '活动通知', 'body': '报名截止 9 月 20 日，活动 9 月 25 日开始。'}, 'a' * 64


class Scheduler:
    def __init__(self, corrected=True):
        self.classifier = Classifier(corrected)


class ExperienceStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.db = self.root/'tasks.sqlite'
        self.rules = self.root/'rules'
        self.store = ExperienceStore(self.db, self.rules)

    def event_and_proposal(self, value=None):
        event = self.store.create_event('classification_correction', {
            'source_id': 'synthetic-1',
            'correction': '截止时间不是开始时间',
        })
        proposal = self.store.create_proposal(event['id'], value or candidate())
        return event, proposal

    def test_event_alone_never_creates_or_publishes_rule(self):
        event = self.store.create_event('draft_edit', {'task_id': 'one', 'diff': '本次改短'})
        self.assertEqual(self.store.event(event['id'])['payload']['diff'], '本次改短')
        self.assertEqual(self.store.proposals(), [])
        self.assertEqual(self.store.rules(), [])

    def test_approve_publishes_readable_rule_and_audit(self):
        event, proposal = self.event_and_proposal()
        rule = self.store.approve(proposal['id'])
        self.assertTrue(rule['enabled'])
        self.assertEqual(rule['active_version'], 1)
        self.assertEqual(rule['versions'][0]['source_event_id'], event['id'])
        path = self.rules/'mail.notice.date-semantics.json'
        self.assertEqual(json.loads(path.read_text()), rule)
        self.assertEqual(self.store.proposal(proposal['id'])['status'], 'published')
        self.assertEqual([item['action'] for item in self.store.actions(rule['id'])], ['approve'])

    def test_second_approval_appends_version_without_losing_history(self):
        _, first = self.event_and_proposal()
        self.store.approve(first['id'])
        _, second = self.event_and_proposal(candidate(
            instruction='截止、开始和材料提交时间必须分别保留其原文角色。'))
        rule = self.store.approve(second['id'])
        self.assertEqual(rule['active_version'], 2)
        self.assertEqual([item['version'] for item in rule['versions']], [1, 2])
        self.assertIn('不得相互替代', rule['versions'][0]['instruction'])
        self.assertIn('分别保留', rule['versions'][1]['instruction'])

    def test_reject_disable_and_restore_are_explicit_and_audited(self):
        _, rejected = self.event_and_proposal(candidate('mail.notice.never-promote'))
        self.store.reject(rejected['id'], '这只是一次性要求。')
        self.assertEqual(self.store.proposal(rejected['id'])['status'], 'rejected')
        self.assertEqual(self.store.rules(), [])

        _, proposal = self.event_and_proposal()
        rule_id = self.store.approve(proposal['id'])['id']
        self.assertFalse(self.store.disable(rule_id, '范围需要重新核对。')['enabled'])
        self.assertEqual(self.store.rules(enabled=True), [])
        self.assertTrue(self.store.restore(rule_id, '已完成人工核对。')['enabled'])
        self.assertEqual([item['action'] for item in self.store.actions()],
                         ['reject', 'approve', 'disable', 'restore'])

    def test_restart_preserves_events_proposals_and_rules(self):
        event, proposal = self.event_and_proposal()
        self.store.approve(proposal['id'])
        fresh = ExperienceStore(self.db, self.rules)
        self.assertEqual(fresh.event(event['id'])['source_hash'], event['source_hash'])
        self.assertEqual(fresh.proposal(proposal['id'])['status'], 'published')
        self.assertEqual(fresh.rules()[0]['active_version'], 1)
        self.assertEqual(fresh.recover(), {'published': [], 'errors': []})

    def test_new_process_reuses_rule_and_disable_only_affects_new_snapshots(self):
        _, proposal = self.event_and_proposal()
        self.store.approve(proposal['id'])
        first_process = ExperienceStore(self.db, self.rules)
        snapshot = first_process.snapshot('mail.plan')
        results = first_process.validate_rule_results(snapshot, [{
            'rule_id': 'mail.notice.date-semantics', 'version': 1,
            'applicable': True, 'evidence_quotes': ['报名截止 9 月 20 日'],
            'conclusion': '在新进程中按截止时间处理。',
        }], ['另一封通知：报名截止 9 月 20 日。'])
        first_process.record_application(
            'mail_task', 'different-task', 'mail.plan', snapshot, results)
        first_process.disable('mail.notice.date-semantics', '跨会话停用测试。')

        second_process = ExperienceStore(self.db, self.rules)
        self.assertEqual(second_process.snapshot('mail.plan')['rules'], [])
        applications = second_process.applications('mail_task', 'different-task')
        self.assertEqual(applications[0]['payload']['snapshot'], snapshot)
        self.assertEqual(applications[0]['payload']['rule_results'], results)

    def test_crash_after_rule_write_is_recovered_without_rewrite(self):
        _, proposal = self.event_and_proposal()

        def crash(event):
            if event == 'after_rule_write':
                raise SystemExit('crash after rename')

        crashing = ExperienceStore(self.db, self.rules, crash)
        with self.assertRaises(SystemExit):
            crashing.approve(proposal['id'])
        path = self.rules/'mail.notice.date-semantics.json'
        stamp = path.stat().st_mtime_ns
        fresh = ExperienceStore(self.db, self.rules)
        self.assertEqual(len(fresh.recover()['published']), 1)
        self.assertEqual(path.stat().st_mtime_ns, stamp)
        self.assertEqual(fresh.proposal(proposal['id'])['status'], 'published')

    def test_pending_publication_does_not_overwrite_manual_conflict(self):
        _, proposal = self.event_and_proposal()
        with patch.object(self.store, '_materialize', side_effect=SystemExit('before write')):
            with self.assertRaises(SystemExit):
                self.store.approve(proposal['id'])
        self.rules.mkdir(parents=True)
        path = self.rules/'mail.notice.date-semantics.json'
        path.write_text('{"manual": true}\n')
        report = ExperienceStore(self.db, self.rules).recover()
        self.assertEqual(report['errors'][0]['code'], 'RULE_CONFLICT')
        self.assertEqual(json.loads(path.read_text()), {'manual': True})
        self.assertEqual(self.store.proposal(proposal['id'])['status'], 'approved_pending')

    def test_manual_rule_edit_is_validated_before_update(self):
        _, proposal = self.event_and_proposal()
        self.store.approve(proposal['id'])
        path = self.rules/'mail.notice.date-semantics.json'
        path.write_text('{"unexpected": true}\n')
        _, second = self.event_and_proposal(candidate(instruction='新的规则文本。'))
        with self.assertRaises(AppError) as raised:
            self.store.approve(second['id'])
        self.assertEqual(raised.exception.code, 'RULE_INVALID')
        self.assertEqual(self.store.proposal(second['id'])['status'], 'pending')

    def test_invalid_scope_and_path_are_rejected(self):
        event = self.store.create_event('manual', {'note': 'test'})
        invalid = candidate('../outside')
        with self.assertRaises(AppError):
            self.store.create_proposal(event['id'], invalid)
        invalid = candidate()
        invalid['domains'] = ['ehall.submit']
        with self.assertRaises(AppError):
            self.store.create_proposal(event['id'], invalid)
        self.assertFalse((self.root/'outside.json').exists())

    def test_approval_state_prevents_duplicate_publication(self):
        _, proposal = self.event_and_proposal()
        self.store.approve(proposal['id'])
        with self.assertRaises(AppError) as raised:
            self.store.approve(proposal['id'])
        self.assertEqual(raised.exception.code, 'EXPERIENCE_STATE')
        with sqlite3.connect(str(self.db)) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM experience_publications').fetchone()[0], 1)

    def test_rule_snapshot_and_application_record_are_deterministic(self):
        _, proposal = self.event_and_proposal()
        self.store.approve(proposal['id'])
        snapshot = self.store.snapshot('mail.plan')
        self.assertEqual(snapshot['rules'][0]['version'], 1)
        results = [{'rule_id': 'mail.notice.date-semantics', 'version': 1,
                    'applicable': True, 'evidence_quotes': ['报名截止 9 月 20 日'],
                    'conclusion': '截止时间与活动时间分别处理。'}]
        checked = self.store.validate_rule_results(
            snapshot, results, ['报名截止 9 月 20 日，活动 9 月 25 日开始。'])
        first = self.store.record_application(
            'mail_task', 'task-one', 'mail.plan', snapshot, checked)
        second = self.store.record_application(
            'mail_task', 'task-one', 'mail.plan', snapshot, checked)
        self.assertEqual(first['id'], second['id'])
        self.assertEqual(len(self.store.applications('mail_task', 'task-one')), 1)
        with self.assertRaises(AppError) as raised:
            self.store.validate_rule_results(snapshot, [dict(
                results[0], evidence_quotes=['不存在的原文'])], ['实际正文'])
        self.assertEqual(raised.exception.code, 'RULE_RESULT_INVALID')


class ExperienceServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = ExperienceStore(self.root/'tasks.sqlite', self.root/'rules')

    def test_selected_draft_edit_creates_one_pending_proposal(self):
        task = {
            'task_id': 'a' * 64,
            'mail': {'raw_sha256': 'b' * 64},
            'drafts': [
                {'version': 1, 'subject': '回复', 'body': '报名时间是 9 月 20 日。',
                 'to': ['a@example.test'], 'cc': [], 'bcc': [], 'attachments': []},
                {'version': 2, 'subject': '回复', 'body': '报名截止时间是 9 月 20 日。',
                 'to': ['a@example.test'], 'cc': [], 'bcc': [], 'attachments': [],
                 'editor': 'user'},
            ],
        }
        client = Client([candidate()])
        service = ExperienceService(self.store, client)
        first = service.from_draft(Tasks(task), task['task_id'], 2, '以后区分截止时间和开始时间。')
        second = service.from_draft(Tasks(task), task['task_id'], 2, '以后区分截止时间和开始时间。')
        self.assertEqual(first['id'], second['id'])
        self.assertEqual(len(client.calls), 1)
        self.assertIn('不可信数据', client.calls[0][0]['content'])
        self.assertEqual(first['status'], 'pending')
        self.assertEqual(self.store.rules(), [])
        event = self.store.event(first['event_id'])
        self.assertEqual(event['kind'], 'draft_edit')
        self.assertNotIn('a@example.test', json.dumps(event, ensure_ascii=False))

    def test_classification_requires_user_correction_and_explicit_guidance(self):
        client = Client([candidate()])
        service = ExperienceService(self.store, client)
        with self.assertRaises(AppError):
            service.from_classification(Scheduler(False), '1', 7, '今后复用')
        with self.assertRaises(AppError):
            service.from_classification(Scheduler(True), '1', 7, '')
        proposal = service.from_classification(
            Scheduler(True), '1', 7, '通知中的截止时间不能被当作活动开始时间。')
        self.assertEqual(proposal['status'], 'pending')
        event = self.store.event(proposal['event_id'])
        self.assertEqual(event['payload']['source']['source_hash'], 'a' * 64)

    def test_model_protocol_failure_keeps_event_without_proposal(self):
        task = {'task_id': 'a' * 64, 'mail': {'raw_sha256': 'b' * 64}, 'drafts': [
            {'version': 1, 'subject': 's', 'body': 'before'},
            {'version': 2, 'subject': 's', 'body': 'after', 'editor': 'user'},
        ]}
        service = ExperienceService(self.store, Client([{'instruction': 'missing fields'}]))
        with self.assertRaises(AppError) as raised:
            service.from_draft(Tasks(task), task['task_id'], 2, '这是明确的长期规则。')
        self.assertEqual(raised.exception.code, 'EXPERIENCE_PROTOCOL')
        self.assertEqual(len(self.store.events()), 1)
        self.assertEqual(self.store.proposals(), [])

    def test_only_user_saved_draft_can_be_promoted(self):
        task = {'task_id': 'a' * 64, 'mail': {'raw_sha256': 'b' * 64}, 'drafts': [
            {'version': 1, 'subject': 's', 'body': 'model output'},
        ]}
        service = ExperienceService(self.store, Client([]))
        with self.assertRaises(AppError) as raised:
            service.from_draft(Tasks(task), task['task_id'], 1, '作为规则')
        self.assertEqual(raised.exception.code, 'EXPERIENCE_STATE')
        self.assertEqual(self.store.events(), [])


if __name__ == '__main__':
    unittest.main()
