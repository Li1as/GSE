import json
import multiprocessing
import sqlite3
import time
import unittest
from unittest.mock import patch

from assistant import AppError, Assistant, Corpus
from test_assistant import ScriptedClient
import test_supplements as samples


def hold_lock(app, task_id, conn):
    with app.task_lock(task_id):
        conn.send('locked')
        time.sleep(30)


class RecoveryTests(unittest.TestCase):
    setUp = samples.SupplementTests.setUp
    set_responses = samples.SupplementTests.set_responses

    def fresh(self, responses):
        return Assistant(self.config, ScriptedClient(responses), self.root/'tasks.sqlite')

    def accept(self):
        self.set_responses([samples.assess(self.reply), samples.search('绿茶'), samples.final])
        return self.app.answer_request(self.rid, self.reply, 'personal')

    def test_crash_after_receipt_before_file(self):
        self.set_responses([samples.assess(self.reply)])
        with patch.object(self.app, '_materialize', side_effect=SystemExit('crash')):
            with self.assertRaises(SystemExit):
                self.app.answer_request(self.rid, self.reply, 'personal')
        self.assertFalse(list(self.personal.glob('supplement-*.md')))
        fresh = self.fresh([samples.search('绿茶'), samples.final])
        report = fresh.recover()
        self.assertIn(self.rid, report['repaired'])
        self.assertEqual(fresh.get_task(self.task['task_id'])['status'], 'completed')
        self.assertEqual(len(list(self.personal.glob('supplement-*.md'))), 1)
        self.assertEqual(fresh.recover()['recovered'], [])

    def test_crash_after_rename_before_publish(self):
        self.set_responses([samples.assess(self.reply)])
        original = self.app._atomic_text
        def crash(path, text):
            original(path, text)
            raise SystemExit('after rename')
        with patch.object(self.app, '_atomic_text', side_effect=crash):
            with self.assertRaises(SystemExit):
                self.app.answer_request(self.rid, self.reply, 'personal')
        path = next(self.personal.glob('supplement-*.md'))
        stamp = path.stat().st_mtime_ns
        fresh = self.fresh([samples.search('绿茶'), samples.final])
        fresh.recover()
        self.assertEqual(path.stat().st_mtime_ns, stamp)
        with sqlite3.connect(str(fresh.db_path)) as db:
            self.assertEqual(db.execute('SELECT status FROM archives WHERE id=?', (self.rid,)).fetchone()[0], 'published')

    def test_user_edit_and_delete_not_undone(self):
        self.accept()
        path = next(self.personal.glob('supplement-*.md'))
        path.write_text('# 用户修改\n\n饮品偏好是红茶。\n')
        fresh = self.fresh([])
        fresh.recover()
        fresh.answer_request(self.rid, self.reply, 'personal')
        self.assertIn('红茶', path.read_text())
        self.assertEqual(Corpus(self.personal, {}).search(['绿茶'])['total'], 0)
        self.assertEqual(fresh.get_task(self.task['task_id'])['freshness']['status'], 'stale')
        path.unlink()
        fresh.recover()
        fresh.answer_request(self.rid, self.reply, 'personal')
        self.assertFalse(path.exists())

    def test_pending_archive_conflict_not_overwritten(self):
        self.set_responses([samples.assess(self.reply)])
        with patch.object(self.app, '_materialize', side_effect=SystemExit):
            with self.assertRaises(SystemExit):
                self.app.answer_request(self.rid, self.reply, 'personal')
        path = self.personal/('supplement-'+self.rid+'.md')
        path.write_text('用户自己写入的不同内容')
        report = self.fresh([]).recover()
        self.assertEqual(report['errors'][0]['code'], 'ARCHIVE_CONFLICT')
        self.assertEqual(path.read_text(), '用户自己写入的不同内容')

    def test_waiting_rechecks_after_markdown_added(self):
        (self.personal/'new.md').write_text('# 补充\n\n饮品选择绿茶。\n')
        fresh = self.fresh([samples.search('饮品'), samples.final])
        report = fresh.recover()
        self.assertEqual(report['recovered'][0]['task_id'], self.task['task_id'])
        self.assertEqual(fresh.get_request(self.rid)['status'], 'resolved_by_update')
        self.assertEqual(fresh.get_task(self.task['task_id'])['freshness']['status'], 'current')

    def test_unchanged_wait_does_not_call_model(self):
        fresh = self.fresh([])
        self.assertEqual(fresh.recover()['recovered'], [])

    def test_crash_before_request_creation_repaired(self):
        app = self.fresh([samples.search('水果'), samples.search('苹果'), samples.missing()])
        with patch.object(app, '_ensure_request', side_effect=SystemExit):
            with self.assertRaises(SystemExit):
                app.query('活动准备什么水果？')
        self.fresh([]).recover()
        with sqlite3.connect(str(app.db_path)) as db:
            tasks = [json.loads(r[0]) for r in db.execute('SELECT payload FROM tasks')]
        self.assertTrue(all(t.get('request_id') for t in tasks))
        self.assertEqual(len(list((self.root/'requests').glob('*.md'))), 2)

    def test_projection_repair_preserves_draft(self):
        path = self.root/'requests'/(self.rid+'.md')
        path.write_text(path.read_text().replace('<!-- reply:start -->', '<!-- reply:start -->\n尚未提交的回复'))
        self.fresh([]).recover()
        self.assertIn('尚未提交的回复', path.read_text())
        path.unlink()
        self.fresh([]).recover()
        self.assertTrue(path.exists())

    def test_mid_query_change_refuses_old_answer(self):
        (self.personal/'new.md').write_text('# 补充\n\n饮品绿茶。')
        def change(messages):
            result = samples.final(messages)
            (self.personal/'new.md').write_text('# 补充\n\n饮品红茶。')
            return result
        app = self.fresh([samples.search('绿茶'), change])
        result = app.query('饮品是什么？')
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['error']['code'], 'DATA_CHANGED')
        self.assertNotIn('result', result)

    def test_retry_budget_and_backoff(self):
        self.set_responses([AppError('API_TIMEOUT', '超时')])
        result = self.app.query('新查询')
        self.assertEqual(self.fresh([]).recover()['recovered'], [])
        result['retry_at'] = 0
        result['attempts'] = 3
        self.app.save(result)
        self.assertEqual(self.fresh([]).recover()['recovered'], [])

    def test_killed_process_releases_task_lock(self):
        task = self.app.get_task(self.task['task_id'])
        task['status'] = 'running'
        self.app.save(task)
        parent, child = multiprocessing.Pipe()
        process = multiprocessing.Process(target=hold_lock, args=(self.app, task['task_id'], child))
        process.start()
        try:
            self.assertTrue(parent.poll(5))
            self.assertEqual(parent.recv(), 'locked')
            report = self.fresh([]).recover()
            self.assertIn(task['task_id'], report['skipped_busy'])
        finally:
            process.terminate()
            process.join(5)
            parent.close()
            child.close()
        fresh = self.fresh([samples.search('饮品'), samples.search('喝什么'), samples.missing()])
        self.assertEqual(fresh.recover()['recovered'][0]['status'], 'waiting_input')

    def test_manual_resume_retains_task_identity(self):
        fresh = self.fresh([samples.search('饮品'), samples.search('喝什么'), samples.missing()])
        result = fresh.resume(self.task['task_id'])
        self.assertEqual(result['task_id'], self.task['task_id'])
        self.assertEqual(result['request_id'], self.rid)


if __name__ == '__main__':
    unittest.main()
