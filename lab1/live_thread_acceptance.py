"""Stage 3D acceptance using saved real thread snapshots; never connects or sends."""
import json
import os
import uuid
from pathlib import Path
from unittest.mock import Mock

from assistant import AppError, Assistant, ROOT
from mail_reader import ids
from mail_send import SendService
from mail_tasks import MailTasks, digest


class Client:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []

    def complete(self, messages):
        self.calls.append(messages)
        return json.dumps(next(self.responses), ensure_ascii=False)


PLAN = {'facts': [], 'decisions': [], 'blockers': []}
DRAFT = {'body': '已收到，谢谢。', 'used_sources': []}


def related_real_snapshots():
    snapshots = []
    for path in (ROOT/'data/mail').glob('*/message.json'):
        value = json.loads(path.read_text())
        anchors = ids(' '.join(value.get(k, '') for k in ('Message-ID', 'References', 'In-Reply-To')))
        snapshots.append((path.parent, value, anchors))
    incoming = sorted((item for item in snapshots if item[1].get('folder') == 'INBOX'),
                      key=lambda item: int(item[1]['uid']))
    for later in reversed(incoming):
        for earlier in reversed(incoming):
            if int(earlier[1]['uid']) < int(later[1]['uid']) and earlier[2] & later[2]:
                history = next((item for item in snapshots
                                if item[1].get('folder') != 'INBOX' and item[2] & earlier[2]), None)
                return earlier[0], later[0], history[0] if history else None
    raise RuntimeError('没有找到连续的真实往来快照。')


def source_for(path, label):
    value = json.loads((path/'message.json').read_text())
    return {'source_id': digest(['3d-acceptance', label, value['account'], value['folder'],
                                 value['uidvalidity'], value['uid']]),
            'category': 'reply_required', 'classification_digest': digest([label, 'reply_required'])}


def build(run, initial, history):
    personal = run/'personal'
    personal.mkdir(parents=True)
    (personal/'empty.md').write_text('# 隔离验收\n')
    client = Client([PLAN, DRAFT])
    assistant = Assistant({'model': 'test', 'personal_data_dir': str(personal), 'max_steps': 8},
                          client, run/'tasks.sqlite')
    tasks = MailTasks(assistant)
    source = source_for(initial, 'initial')
    task = tasks.create(initial, '准备简短确认回复', [history] if history else [], source=source)
    return tasks, client, tasks.register_conversation(task['task_id'], source)


def main():
    os.umask(0o077)
    run = ROOT/'data/thread-acceptance'/uuid.uuid4().hex
    run.mkdir(parents=True)
    checks = {}
    report = {'checks': checks, 'sent_messages': 0}
    try:
        initial, update, history = related_real_snapshots()

        tasks, _, task = build(run/'ignore', initial, history)
        task = tasks.edit(task['task_id'], task['draft']['version'], '用户编辑内容保持不变。',
                          task['draft']['subject'], task['draft']['to'])
        sender = SendService(tasks, run/'ignore', {'address': task['mail']['account']}, Mock())
        preview = sender.preview(task['task_id'], task['draft']['version'])
        update_source = source_for(update, 'update-ignore')
        changed = tasks.note_conversation_update(task['task_id'], update, update_source)
        checks['real_thread_detected'] = changed['conversation_review_required'] is True
        checks['draft_preserved_while_waiting'] = changed['draft']['body'] == '用户编辑内容保持不变。'
        checks['old_draft_stale'] = changed['freshness'] == 'stale'
        try:
            sender.confirm(task['task_id'], preview['id'], preview['fingerprint'])
            checks['old_preview_invalidated'] = False
        except AppError:
            checks['old_preview_invalidated'] = True
        ignored = tasks.review_conversation_update(task['task_id'], update_source['source_id'], 'ignore')
        checks['ignore_preserves_user_edit'] = (ignored['draft']['body'] == '用户编辑内容保持不变.' or
                                                ignored['draft']['body'] == '用户编辑内容保持不变。')
        checks['ignore_creates_current_version'] = ignored['freshness'] == 'current'

        tasks2, client2, task2 = build(run/'include', initial, history)
        task2 = tasks2.edit(task2['task_id'], task2['draft']['version'], '请保留我的措辞。',
                            task2['draft']['subject'], task2['draft']['to'])
        include_source = source_for(update, 'update-include')
        tasks2.note_conversation_update(task2['task_id'], update, include_source)
        client2.responses = iter([PLAN, {'body': '请保留我的措辞，并回应最新来信。', 'used_sources': []}])
        included = tasks2.review_conversation_update(task2['task_id'], include_source['source_id'], 'include')
        checks['include_uses_latest_mail'] = included['mail']['raw_sha256'] != task2['mail']['raw_sha256']
        checks['include_keeps_old_mail_as_history'] = any(
            item['raw_sha256'] == task2['mail']['raw_sha256'] for item in included['history'])
        checks['include_passes_user_edit_to_model'] = 'prior_user_draft' in client2.calls[-1][-1]['content']
        checks['include_generates_reviewable_draft'] = included['status'] == 'draft_ready'
        checks['no_send_records'] = (sender.history(task['task_id']) == [preview] and
                                     not any(item['status'] in ('sending', 'accepted', 'unknown')
                                             for item in sender.history(task['task_id'])))
        report['passed'] = all(checks.values())
    except Exception as error:
        report.update(passed=False, error_type=type(error).__name__)
    target = run/'acceptance.json'
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2)+'\n')
    print(json.dumps(report, ensure_ascii=False))
    print('Private report: ' + str(target.relative_to(ROOT)))
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
