"""Real-API 3C acceptance on an isolated copy; never sends mail."""
import json
import os
import shutil
import sqlite3
import uuid
from pathlib import Path
from unittest.mock import Mock

from assistant import API, AppError, Assistant, ROOT, load_config
from mail_classifier import Classifier
from mail_inbox import Inbox
from mail_pipeline import Pipeline
from mail_send import SendService
from mail_tasks import MailTasks


class CountingClient:
    def __init__(self, client):
        self.client, self.calls = client, 0

    def complete(self, messages):
        self.calls += 1
        return self.client.complete(messages)


def main():
    os.umask(0o077)
    run = ROOT/'data/pipeline-acceptance'/uuid.uuid4().hex
    run.mkdir(parents=True)
    checks = {}
    report = {'checks': checks, 'sent_messages': 0}
    try:
        mail_config = json.loads((ROOT/'mail.local.json').read_text())
        source_inbox = Inbox(ROOT/'data/monitor', mail_config)
        available = [row for row in source_inbox.rows() if row['status'] == 'ready' and row['snapshot']]
        if not available:
            raise RuntimeError('No real collected sample')
        selected = available[-1]
        inbox = Inbox(run/'monitor', mail_config)
        destination = inbox.directory/'mail'/inbox.stream/Path(selected['snapshot']).name
        destination.parent.mkdir(parents=True)
        shutil.copytree(selected['snapshot'], destination)
        with inbox.connect() as db:
            db.execute('''INSERT INTO inbox(stream,validity,uid,status,attempts,retry_at,error,snapshot)
                VALUES (?,?,?,?,?,?,?,?)''',
                (inbox.stream, selected['validity'], selected['uid'], 'ready', 1, 0, None,
                 str(destination.resolve())))

        config = load_config(ROOT/'config.local.json')
        client = CountingClient(API(config))
        classifier = Classifier(inbox, client)
        classification = classifier.once(1)[0]
        checks['real_classification_completed'] = classification['status'] == 'classified'
        # Explicitly create a reply-required correction only inside the isolated copy.
        classifier.correct(selected['validity'], selected['uid'], 'reply_required',
                           '隔离验收要求准备一封简短确认。',
                           suggested_goal='仅简短确认收到对方回复，不披露额外个人信息')
        assistant = Assistant(config, client, run/'tasks.sqlite')
        tasks = MailTasks(assistant)
        pipeline = Pipeline(classifier, tasks)
        result = pipeline.once(1)[0]
        task = tasks.get(result['task_id'])
        if task['status'] == 'waiting_input':
            unanswered = [decision for decision in task['decisions'] if decision['answer'] is None]
            style_question = task.get('style', {}).get('decision_question')
            if len(unanswered) == 1 and unanswered[0]['question'] == style_question:
                task = tasks.decide(task['task_id'], unanswered[0]['id'],
                                    '本次使用简洁、礼貌、偏正式的中文，不添加具体称谓和签名。')
        checks['draft_ready'] = task['status'] == 'draft_ready'
        checks['source_bound'] = tasks.source_task(result['source_id']) == task['task_id']
        checks['style_persisted'] = task['style']['tone'] == 'neutral_formal' and task['style']['signature'] == 'none'
        checks['no_unanswered_decisions'] = all(d['answer'] is not None for d in task['decisions'])
        body = task.get('draft', {}).get('body', '')
        checks['draft_nonempty_and_bounded'] = bool(body.strip()) and len(body) <= 1000
        checks['no_inferred_relationship'] = all(term not in body for term in ('老师', '学长', '学姐'))
        checks['no_emoji_or_contact_leak'] = all(term not in body for term in ('😀', '😊', mail_config['address']))
        checks['no_automatic_signature'] = not any(line.strip().lower() in ('此致', 'best regards', 'sincerely')
                                                   for line in body.splitlines())
        checks['reply_subject_not_duplicated'] = bool(task.get('draft')) and not task['draft']['subject'].lower().startswith('re: re:')
        task_count_before = len([1 for _ in sqlite3.connect(tasks.db_path).execute('SELECT 1 FROM mail_tasks')])
        calls_before = client.calls
        repeated = pipeline.once(1)[0]
        checks['repeat_reuses_task'] = repeated['task_id'] == task['task_id']
        checks['repeat_no_model_call'] = client.calls == calls_before
        checks['one_task_one_binding'] = task_count_before == 1 and len(
            list(sqlite3.connect(tasks.db_path).execute('SELECT 1 FROM mail_sources'))) == 1

        classifier.correct(selected['validity'], selected['uid'], 'no_reply',
                           '隔离验收改判为无需回复。')
        paused = pipeline.once(1)[0]
        checks['correction_pauses_same_task'] = (paused['task_id'] == task['task_id'] and
                                                  tasks.get(task['task_id'])['automation_paused'])
        sender = SendService(tasks, run, {'address': mail_config['address']}, Mock())
        try:
            sender.preview(task['task_id'], task.get('draft', {}).get('version', 0))
            checks['paused_source_blocks_preview'] = False
        except AppError:
            checks['paused_source_blocks_preview'] = True
        checks['no_send_records'] = sender.history(task['task_id']) == []
        report.update(model_calls=client.calls, category_before_correction=
                      json.loads(classification['payload'])['current']['category'],
                      task_status_before_pause=task['status'], draft_characters=len(body),
                      sample_uid=selected['uid'], passed=all(checks.values()))
    except Exception as error:
        report.update(passed=False, error_type=type(error).__name__)
    (run/'acceptance.json').write_text(json.dumps(report, ensure_ascii=False, indent=2)+'\n')
    print(json.dumps({key: value for key, value in report.items() if key != 'sample_uid'}, ensure_ascii=False))
    print('Private report: ' + str((run/'acceptance.json').relative_to(ROOT)))
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
