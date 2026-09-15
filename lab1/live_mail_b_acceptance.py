"""Stage 2B live model acceptance on isolated fictional mail and personal data."""
import json
import os
import uuid
from email.message import EmailMessage

from assistant import Assistant, ROOT, load_config
from mail_reader import parse_message, save_snapshot
from mail_tasks import MailTasks


def main():
    os.umask(0o077)
    directory = ROOT / 'data' / 'mail-b-live' / uuid.uuid4().hex
    personal = directory / 'personal'
    personal.mkdir(parents=True)
    (personal / 'resume.md').write_text('# 虚构验收资料\n\n姓名：测试同学。\n')
    config = load_config(ROOT / 'config.local.json')
    config['personal_data_dir'] = str(personal)
    app = MailTasks(Assistant(config, db_path=directory / 'tasks.sqlite'))
    msg = EmailMessage()
    msg['From'] = 'teacher@example.test'
    msg['To'] = 'student@example.test'
    msg['Subject'] = '虚构验收活动邀请'
    msg['Message-ID'] = '<acceptance@example.test>'
    msg.set_content('请告知你在2026年春季的验收课程QX91的最终成绩，并明确回复是否参加本次虚构活动。无须提供其他个人信息。')
    raw = msg.as_bytes()
    parsed = parse_message(raw)
    parsed.update(account='student@example.test', folder='INBOX', uidvalidity='1', uid='1')
    path = save_snapshot(directory / 'mail', parsed, raw)
    report = {'checks': {}, 'directory': str(directory), 'fictional_data_only': True}
    checks = report['checks']
    def check(name, value):
        checks[name] = bool(value)
        print(name + ': ' + str(bool(value)), flush=True)
        if not value:
            raise RuntimeError(name)
    try:
        task = app.create(path, '准备回复这封邀请：只查询2026年春季验收课程QX91最终成绩，并询问我是否参加本次活动。')
        report['task_id'] = task['task_id']
        check('waiting_fact_and_decision', task['status'] == 'waiting_input' and len(task['facts']) == 1 and
              len(task['decisions']) == 1 and task['facts'][0]['status'] == 'waiting_input')
        rid = task['facts'][0]['request_id']
        task = app.answer(task['task_id'], rid, '今天天气晴朗，我刚吃过早餐。', 'personal')
        check('irrelevant_fact_rejected', task.get('reply_feedback', {}).get('status') == 'irrelevant' and
              not list(personal.glob('supplement-*')))
        task = app.decide(task['task_id'], '1', '我喜欢晴朗的天气。')
        check('irrelevant_decision_rejected', task.get('reply_feedback', {}).get('status') == 'irrelevant' and
              task['decisions'][0]['answer'] is None)
        task = app.answer(task['task_id'], rid, '2026年春季验收课程QX91最终成绩为93分。', 'task')
        check('fact_ready_waiting_decision', task['facts'][0]['status'] == 'completed' and
              task['status'] == 'waiting_input' and 'draft' not in task)
        # Reopen the database as after process restart.
        app = MailTasks(Assistant(config, db_path=directory / 'tasks.sqlite'))
        task = app.decide(task['task_id'], '1', '我决定参加本次虚构活动。')
        check('draft_ready_after_restart', task['status'] == 'draft_ready' and '93' in task['draft']['body'] and
              bool(task['draft']['used_sources']) and task['freshness'] == 'current')
        check('temporary_not_archived', not list(personal.glob('supplement-*')))
        check('duplicate_resume_keeps_version', app.resume(task['task_id'])['draft']['version'] == 1)
        other = app.create(path, '另一独立任务：查询2026年春季验收课程QX91最终成绩，并询问我是否参加本次活动。')
        check('other_task_cannot_use_temporary', other['status'] == 'waiting_input' and
              any(f['status'] == 'waiting_input' for f in other['facts']))
        report['passed'] = True
    except Exception as error:
        report.update(passed=False, error_type=type(error).__name__)
    (directory / 'acceptance.json').write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
