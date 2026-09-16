"""Stage 3C bridge from classified inbox records to unique draft tasks."""
import argparse
import json
import os
import re
import time
from pathlib import Path

from assistant import API, AppError, Assistant, ROOT, load_config
from experience import ExperienceStore
from mail_classifier import Classifier
from mail_monitor import Monitor
from mail_tasks import MailTasks, digest, snapshot


def style_for(parsed, has_history):
    body = parsed['body']
    han = len(re.findall(r'[\u3400-\u9fff]', body))
    latin = len(re.findall(r'[A-Za-z]', body))
    ambiguous = ((han >= 10 and latin >= 10 and max(han, latin) < 4 * min(han, latin)) or
                 (han == 0 and latin == 0))
    if ambiguous:
        language = 'auto'
    elif han and han >= latin / 4:
        language = 'zh'
    elif latin:
        language = 'en'
    else:
        language = 'auto'
    style = {
        'language': language,
        'tone': 'neutral_formal',
        'length': 'brief',
        'salutation': 'explicit_only',
        'signature': 'none',
        'emoji': 'none',
        'quote_original': False,
        'context_basis': 'thread' if has_history else 'default_no_history',
        'rules': [
            'follow_explicit_style_decision',
            'use_only_explicit_names_and_titles',
            'do_not_infer_relationship',
            'answer_all_explicit_requests',
            'do_not_add_personal_information',
            'do_not_claim_attachment_or_send_result',
        ],
    }
    if ambiguous:
        style['decision_question'] = '请确认本次回复使用中文、英文或其他语言，以及希望采用的正式程度。'
    return style


class Pipeline:
    def __init__(self, classifier, tasks):
        self.classifier, self.tasks = classifier, tasks

    def source_id(self, row):
        return digest(['inbox', row['stream'], row['validity'], row['uid']])

    def _binding(self, row, current, payload):
        classification_digest = digest([
            current, payload['source_hash'], payload['policy_version'],
            payload.get('experience_snapshot', {}).get('digest')])
        return {
            'source_id': self.source_id(row),
            'stream': row['stream'],
            'validity': row['validity'],
            'uid': row['uid'],
            'category': current['category'],
            'classification_digest': classification_digest,
        }

    def once(self, limit=10, changes_only=False):
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError('limit 必须为 1—100。')
        results = []
        inbox_by_identity = {(row['validity'], row['uid']): row for row in self.classifier.inbox.rows()}
        for classified in self.classifier.rows():
            if len(results) >= limit or classified['status'] not in ('classified', 'corrected'):
                continue
            payload = classified['payload']
            current = payload['current']
            inbox_row = inbox_by_identity.get((classified['validity'], classified['uid']))
            if not inbox_row or inbox_row['status'] != 'ready':
                continue
            source = self._binding(classified, current, payload)
            if changes_only and self.tasks.dispatch_current(
                    source['source_id'], source['classification_digest']):
                continue
            existing = self.tasks.source_task(source['source_id'])
            parsed, _ = self.classifier._source(inbox_row)
            complete = snapshot(Path(inbox_row['snapshot']))
            related_task = None if existing else self.tasks.conversation_task(complete)
            if related_task:
                related = self.tasks.get(related_task)
                identity = tuple(complete.get(key) for key in ('account', 'folder', 'uidvalidity', 'uid'))
                related_identity = tuple(related['mail'].get(key) for key in
                                         ('account', 'folder', 'uidvalidity', 'uid'))
                if identity == related_identity:
                    task = self.tasks.register_conversation(related_task, source)
                    result = {'source_id': source['source_id'], 'category': current['category'],
                              'task_id': task['task_id'], 'task_status': task['status'],
                              'automation_paused': task.get('automation_paused', False)}
                    self.tasks.mark_dispatched(source['source_id'], source['classification_digest'], result)
                    results.append(result)
                    continue
                task = self.tasks.note_conversation_update(
                    related_task, Path(inbox_row['snapshot']), source)
                result = {'source_id': source['source_id'], 'category': current['category'],
                          'task_id': task['task_id'], 'task_status': task['status'],
                          'conversation_review_required': True}
                self.tasks.mark_dispatched(source['source_id'], source['classification_digest'], result)
                results.append(result)
                continue
            if current['category'] != 'reply_required':
                task = self.tasks.update_source(source['source_id'], current['category'],
                                                source['classification_digest']) if existing else None
                result = {'source_id': source['source_id'], 'category': current['category'],
                          'task_id': existing, 'task_status': task['status'] if task else None,
                          'automation_paused': bool(task)}
                self.tasks.mark_dispatched(source['source_id'], source['classification_digest'], result)
                results.append(result)
                continue
            related = self.classifier.related_rows(inbox_row, parsed)
            histories = [Path(item['snapshot']) for item in related]
            style = style_for(parsed, bool(histories))
            if existing:
                task = self.tasks.update_source(source['source_id'], 'reply_required',
                                                source['classification_digest'],
                                                current['suggested_goal'], style)
            else:
                task = self.tasks.create(Path(inbox_row['snapshot']), current['suggested_goal'],
                                         histories, source=source, style=style)
                task = self.tasks.register_conversation(task['task_id'], source)
            result = {'source_id': source['source_id'], 'category': current['category'],
                      'task_id': task['task_id'], 'task_status': task['status'],
                      'automation_paused': task.get('automation_paused', False)}
            self.tasks.mark_dispatched(source['source_id'], source['classification_digest'], result)
            results.append(result)
        return results


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['once', 'run', 'list', 'review-update'])
    parser.add_argument('--config', type=Path, default=ROOT/'config.local.json')
    parser.add_argument('--mail-config', type=Path, default=ROOT/'mail.local.json')
    parser.add_argument('--monitor-dir', type=Path, default=ROOT/'data/monitor')
    parser.add_argument('--tasks-db', type=Path, default=ROOT/'data/tasks.sqlite')
    parser.add_argument('--interval', type=int, default=60)
    parser.add_argument('--limit', type=int, default=10)
    parser.add_argument('--task-id')
    parser.add_argument('--source-id')
    parser.add_argument('--action', choices=['include', 'ignore'])
    args = parser.parse_args()
    try:
        config = load_config(args.config)
        mail_config = json.loads(args.mail_config.read_text())
        monitor = Monitor(mail_config, args.monitor_dir)
        assistant = Assistant(config, API(config), args.tasks_db)
        experience = ExperienceStore(assistant.db_path, ROOT/'experience/rules/mail')
        tasks = MailTasks(assistant, experience)
        classifier = Classifier(monitor.inbox, assistant.client, experience=experience)
        pipeline = Pipeline(classifier, tasks)
        if args.command == 'review-update':
            if not args.task_id or not args.source_id or not args.action:
                raise ValueError('review-update 需要 --task-id、--source-id 和 --action。')
            task = tasks.review_conversation_update(args.task_id, args.source_id, args.action)
            print(json.dumps({'task_id': task['task_id'], 'status': task['status'],
                              'draft_version': task.get('draft', {}).get('version')}, ensure_ascii=False))
            return 0
        if args.command == 'list':
            with tasks.assistant.db_path.open('rb'):
                pass
            import sqlite3
            with sqlite3.connect(str(tasks.db_path)) as db:
                rows = [json.loads(row[0]) for row in db.execute('SELECT payload FROM mail_sources')]
            print(json.dumps(rows, ensure_ascii=False))
            return 0
        while True:
            monitor.once()
            classifier.once(args.limit)
            print(json.dumps(pipeline.once(args.limit), ensure_ascii=False), flush=True)
            if args.command == 'once':
                return 0
            if args.interval < 5:
                raise ValueError('interval 至少为 5 秒。')
            time.sleep(args.interval)
    except (OSError, ValueError, KeyError, TypeError, AppError):
        print('3C 流程失败：请检查配置、分类、来源及任务状态。')
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
