"""Persistent stage-3E scheduler for collection, classification and task dispatch."""
import argparse
import hashlib
import json
import os
import sqlite3
import time
from pathlib import Path

from assistant import API, AppError, Assistant, ROOT, load_config
from experience import ExperienceStore
from mail_classifier import Classifier, public
from mail_monitor import Monitor
from mail_pipeline import Pipeline
from mail_tasks import MailTasks


def utc_now():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


class Scheduler:
    def __init__(self, monitor, classifier, pipeline, clock=time.time):
        self.monitor, self.classifier, self.pipeline, self.clock = monitor, classifier, pipeline, clock
        self.inbox = monitor.inbox
        with self.inbox.connect() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS mail_scheduler (
                stream TEXT PRIMARY KEY, payload TEXT NOT NULL)''')
            db.execute('''CREATE TABLE IF NOT EXISTS mail_queue_actions (
                stream TEXT NOT NULL, validity TEXT NOT NULL, uid INTEGER NOT NULL,
                action TEXT NOT NULL, classification_digest TEXT NOT NULL,
                payload TEXT NOT NULL, PRIMARY KEY(stream,validity,uid))''')

    def _state(self):
        with self.inbox.connect() as db:
            row = db.execute('SELECT payload FROM mail_scheduler WHERE stream=?',
                             (self.inbox.stream,)).fetchone()
        return json.loads(row[0]) if row else {
            'status': 'active', 'cycles': 0, 'failures': 0, 'retry_at': 0,
            'last_cycle': None, 'last_success': None, 'last_error': None,
        }

    def _save(self, state):
        with self.inbox.connect() as db:
            db.execute('INSERT OR REPLACE INTO mail_scheduler VALUES (?,?)',
                       (self.inbox.stream, json.dumps(state, ensure_ascii=False)))

    def pause(self):
        with self.inbox.task_lock('scheduler-state:' + self.inbox.stream):
            state = self._state()
            state.update(status='paused', paused_at=utc_now())
            self._save(state)
        return self.status()

    def resume(self):
        # Resume is an explicit recovery action: verify IMAP and clear the
        # monitor's durable pause before reporting automation as healthy.
        with self.inbox.task_lock('scheduler:' + self.inbox.stream):
            monitor = self.monitor.once(resume=True)
            monitor_state = monitor.get('state') or {}
            operational = monitor_state.get('status') == 'active'
            with self.inbox.task_lock('scheduler-state:' + self.inbox.stream):
                state = self._state()
                state.update(status='active' if operational else 'attention',
                             failures=0, retry_at=0,
                             last_error=monitor_state.get('error'),
                             resumed_at=utc_now())
                if operational:
                    state['last_success'] = utc_now()
                self._save(state)
        return self.status()

    def cycle(self, batch=50, classify_limit=20, dispatch_limit=20, force=False):
        if not all(type(value) is int and 1 <= value <= 100 for value in
                   (batch, classify_limit, dispatch_limit)):
            raise ValueError('每轮上限须为 1—100。')
        with self.inbox.task_lock('scheduler:' + self.inbox.stream):
            state = self._state()
            if state['status'] == 'paused' and not force:
                return {'skipped': 'paused', **self.status()}
            remain_paused = state['status'] == 'paused' and force
            if state.get('retry_at', 0) > self.clock() and not force:
                return {'skipped': 'backoff', **self.status()}
            started = utc_now()
            try:
                monitor_before = self.inbox.state() or {}
                retry_transient = force and monitor_before.get('status') == 'retry_wait'
                monitor_result = self.monitor.once(batch=batch, resume=retry_transient)
                classified = self.classifier.once(classify_limit)
                dispatched = self.pipeline.once(dispatch_limit, changes_only=True)
                monitor_state = monitor_result.get('state') or {}
                operational = monitor_state.get('status') == 'active'
                state.update(status='paused' if remain_paused else ('active' if operational else 'attention'),
                             cycles=state.get('cycles', 0) + 1, failures=0, retry_at=0,
                             last_cycle=started, last_success=utc_now() if operational else state.get('last_success'),
                             last_error=monitor_state.get('error'))
                self._save(state)
                return {'monitor': monitor_result, 'classified': len(classified),
                        'dispatched': len(dispatched), **self.status()}
            except (AppError, OSError, ValueError, TypeError, KeyError) as error:
                failures = state.get('failures', 0) + 1
                state.update(status='retry_wait', cycles=state.get('cycles', 0) + 1,
                             failures=failures, retry_at=self.clock() + min(900, 30 * 2 ** min(failures-1, 5)),
                             last_cycle=started, last_error=getattr(error, 'code', type(error).__name__))
                self._save(state)
                return {'error': state['last_error'], **self.status()}

    def status(self):
        state = self._state()
        monitor = self.inbox.summary()
        classifications = self.classifier.rows()
        classification_counts = {}
        categories = {}
        for row in classifications:
            classification_counts[row['status']] = classification_counts.get(row['status'], 0) + 1
            if row.get('payload') and row['status'] in ('classified', 'corrected'):
                category = row['payload']['current']['category']
                categories[category] = categories.get(category, 0) + 1
        actions = self._actions()
        handled = sum(1 for row in classifications
                      if (row['validity'], row['uid']) in actions and
                      actions[(row['validity'], row['uid'])]['action'] == 'dismissed' and
                      actions[(row['validity'], row['uid'])]['classification_digest'] ==
                      self.classification_digest(row))
        return {'scheduler': state, 'monitor': monitor, 'handled_count': handled,
                'classification_counts': classification_counts, 'category_counts': categories}

    @staticmethod
    def classification_digest(classification):
        payload = classification.get('payload') or {}
        value = [payload.get('source_hash'), payload.get('policy_version'), payload.get('current')]
        return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()

    def _actions(self):
        with self.inbox.connect() as db:
            return {(row['validity'], row['uid']): dict(row) for row in db.execute(
                'SELECT * FROM mail_queue_actions WHERE stream=?', (self.inbox.stream,))}

    def items(self, include_dismissed=False, limit=None):
        if limit is not None and (type(limit) is not int or not 1 <= limit <= 100):
            raise ValueError('队列列表上限无效。')
        inbox_rows = {(row['validity'], row['uid']): row for row in self.inbox.rows()}
        classified = {(row['validity'], row['uid']): row for row in self.classifier.rows()}
        actions = self._actions()
        archived_tasks = self.pipeline.tasks.archived_task_ids()
        result = []
        for identity, row in sorted(inbox_rows.items(), reverse=True):
            item = {'validity': row['validity'], 'uid': row['uid'], 'collection_status': row['status'],
                    'collection_error': row['error'], 'subject': '', 'from': '', 'category': None,
                    'classification_status': None, 'classification_reason': None,
                    'decision_question': None, 'task_id': None, 'task_status': None}
            if row.get('snapshot'):
                try:
                    saved = json.loads((Path(row['snapshot'])/'message.json').read_text())
                    item.update(subject=saved.get('Subject', ''), **{'from': saved.get('From', '')})
                except (OSError, ValueError, TypeError):
                    item['collection_error'] = item['collection_error'] or 'SNAPSHOT_INVALID'
            classification = classified.get(identity)
            if classification:
                item['classification_status'] = classification['status']
                if classification.get('payload'):
                    current = classification['payload']['current']
                    item.update(category=current['category'], classification_reason=current['reason'],
                                decision_question=current.get('decision_question'))
                    source_id = self.pipeline.source_id(classification)
                    task_id = self.pipeline.tasks.source_task(source_id)
                    item['task_id'] = task_id
                    if task_id:
                        item['task_status'] = self.pipeline.tasks.get(task_id)['status']
                        if task_id in archived_tasks:
                            continue
                action = actions.get(identity)
                item['dismissed'] = bool(action and action['action'] == 'dismissed' and
                                         action['classification_digest'] ==
                                         self.classification_digest(classification))
                if item['dismissed']:
                    try:
                        item['dismissed_at'] = json.loads(action['payload'])['dismissed_at']
                    except (ValueError, TypeError, KeyError):
                        item['dismissed_at'] = ''
            else:
                item['dismissed'] = False
            if item['dismissed'] != include_dismissed:
                continue
            result.append(item)
        if include_dismissed:
            result.sort(key=lambda item: (item.get('dismissed_at', ''), item['uid']), reverse=True)
        if limit is not None:
            result = result[:limit]
        return result

    def dismiss(self, validity, uid):
        if not isinstance(validity, str) or type(uid) is not int or uid < 1:
            raise ValueError('邮件身份无效。')
        row = next((item for item in self.classifier.rows()
                    if item['validity'] == validity and item['uid'] == uid), None)
        if not row or row['status'] not in ('classified', 'corrected') or not row.get('payload'):
            raise ValueError('只能隐藏已经完成分类的邮件。')
        if row['payload']['current']['category'] != 'no_reply':
            raise ValueError('只有无需回复的邮件可以直接标记为已处理。')
        payload = {'dismissed_at': utc_now(), 'category': 'no_reply'}
        with self.inbox.connect() as db:
            db.execute('INSERT OR REPLACE INTO mail_queue_actions VALUES (?,?,?,?,?,?)',
                       (self.inbox.stream, validity, uid, 'dismissed',
                        self.classification_digest(row), json.dumps(payload, ensure_ascii=False)))
        return next(item for item in self.items(include_dismissed=True)
                    if item['validity'] == validity and item['uid'] == uid)

    def restore(self, validity, uid):
        if not isinstance(validity, str) or type(uid) is not int or uid < 1:
            raise ValueError('邮件身份无效。')
        with self.inbox.connect() as db:
            changed = db.execute('DELETE FROM mail_queue_actions WHERE stream=? AND validity=? AND uid=?',
                                 (self.inbox.stream, validity, uid)).rowcount
        if not changed:
            raise ValueError('邮件没有被标记为已处理。')
        return next(item for item in self.items()
                    if item['validity'] == validity and item['uid'] == uid)

    def correct(self, validity, uid, category, reason, suggested_goal='', decision_question=''):
        self.classifier.correct(validity, uid, category, reason, suggested_goal, decision_question)
        self.pipeline.once(100, changes_only=True)
        return next(item for item in self.items()
                    if item['validity'] == validity and item['uid'] == uid)


def build(config_path, mail_config_path, monitor_dir, tasks_db):
    config = load_config(config_path)
    mail_config = json.loads(mail_config_path.read_text())
    monitor = Monitor(mail_config, monitor_dir)
    assistant = Assistant(config, API(config), tasks_db)
    experience = ExperienceStore(assistant.db_path, ROOT/'experience/rules/mail')
    tasks = MailTasks(assistant, experience)
    classifier = Classifier(monitor.inbox, assistant.client, experience=experience)
    return Scheduler(monitor, classifier, Pipeline(classifier, tasks))


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['once', 'run', 'status', 'pause', 'resume'])
    parser.add_argument('--config', type=Path, default=ROOT/'config.local.json')
    parser.add_argument('--mail-config', type=Path, default=ROOT/'mail.local.json')
    parser.add_argument('--monitor-dir', type=Path, default=ROOT/'data/monitor')
    parser.add_argument('--tasks-db', type=Path, default=ROOT/'data/tasks.sqlite')
    parser.add_argument('--interval', type=int, default=60)
    parser.add_argument('--batch', type=int, default=50)
    parser.add_argument('--limit', type=int, default=20)
    args = parser.parse_args()
    try:
        if args.interval < 5:
            raise ValueError('interval 至少为 5 秒。')
        app = build(args.config, args.mail_config, args.monitor_dir, args.tasks_db)
        if args.command == 'status':
            result = app.status()
        elif args.command == 'pause':
            result = app.pause()
        elif args.command == 'resume':
            result = app.resume()
        else:
            while True:
                result = app.cycle(args.batch, args.limit, args.limit, force=args.command == 'once')
                print(json.dumps(result, ensure_ascii=False), flush=True)
                if args.command == 'once':
                    return 0
                time.sleep(args.interval)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except KeyboardInterrupt:
        return 0
    except (AppError, OSError, ValueError, TypeError, KeyError):
        print('调度器操作失败：请检查配置、状态和是否已有实例运行。')
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
