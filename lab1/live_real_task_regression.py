"""Replan a copy of the real browser task; never mutate its original DB or corpus."""
import json
import argparse
import os
import re
import shutil
import sqlite3
import uuid
from pathlib import Path

from assistant import Assistant, ROOT, load_config
from mail_tasks import MailTasks


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser()
    parser.add_argument('--expectations', type=Path, default=ROOT / 'data/regression-expectations.json')
    parser.add_argument('--resume-directory', type=Path)
    parser.add_argument('--continue-current', action='store_true', help='Resume the copied current plan without replanning')
    args = parser.parse_args()
    expected = json.loads(args.expectations.read_text(encoding='utf-8'))
    root = ROOT / 'data' / 'real-regression' / uuid.uuid4().hex
    root.mkdir(parents=True)
    config = load_config(ROOT / 'config.local.json')
    original = config['personal_data_dir']
    personal = root / 'personal'
    shutil.copytree(original, personal)
    config['personal_data_dir'] = str(personal)
    database = root / 'tasks.sqlite'
    with sqlite3.connect(str(ROOT / 'data' / 'tasks.sqlite')) as source:
        with sqlite3.connect(str(database)) as dest:
            source.backup(dest)
    with sqlite3.connect(str(database)) as db:
        for table in ('tasks', 'requests', 'archives', 'mail_tasks'):
            for rowid, payload in db.execute('SELECT rowid,payload FROM ' + table).fetchall():
                db.execute('UPDATE ' + table + ' SET payload=? WHERE rowid=?',
                           (payload.replace(original, str(personal)), rowid))
    app = MailTasks(Assistant(config, db_path=database))
    key = expected['task_id']
    before = app.get(key)
    if args.resume_directory:
        previous = args.resume_directory.resolve()
        if previous.parent != (ROOT / 'data' / 'real-regression').resolve():
            raise ValueError('Only an isolated regression directory can be resumed')
        # Copy the previous regression DB into this NEW run, preserving prior evidence.
        with sqlite3.connect(str(previous / 'tasks.sqlite')) as source:
            with sqlite3.connect(str(database)) as dest:
                source.backup(dest)
        with sqlite3.connect(str(database)) as db:
            for table in ('tasks', 'requests', 'archives', 'mail_tasks'):
                for rowid, payload in db.execute('SELECT rowid,payload FROM ' + table).fetchall():
                    db.execute('UPDATE ' + table + ' SET payload=? WHERE rowid=?',
                               (payload.replace(str(previous / 'personal'), str(personal)), rowid))
        app = MailTasks(Assistant(config, db_path=database))
        result = app.resume(key)
    elif args.continue_current:
        result = app.resume(key)
    else:
        result = app.replan(key)
    questions = [f['question'] for f in result['facts']]
    body = result.get('draft', {}).get('body', '')
    checks = {'draft_ready': result['status'] == 'draft_ready',
              'no_blockers': not result['blockers'],
              'decisions_preserved': all(any(d['question'] == old['question'] and d['answer'] == old['answer']
                                           for d in result['decisions']) for old in before['decisions'] if old['answer']),
              'old_plan_retained': bool(result.get('plan_history')),
              'no_unanswered_decisions': all(d['answer'] is not None for d in result['decisions']),
              'all_required_facts_complete': all(f['status'] == 'completed' for f in result['facts']),
              'requested_grade_present': expected['grade'] in body,
              'requested_graduation_present': bool(re.search(expected['graduation_pattern'], body)) and '毕业' in body,
              'requested_school_present': expected['school'] in body,
              'requested_major_present': expected['major'] in body,
              'requested_gpa_present': expected['gpa'] in body,
              'no_visible_supplement_requests': not app.visible_requests(result),
              'requested_languages_present': expected['language'] in body,
              'asks_for_attachment': '附件' in body,
              'no_unrequested_doctoral_details': expected['excluded_year'] not in body and '博士毕业' not in body}
    report = {'directory': str(root), 'task_id': key, 'checks': checks,
              'status': result['status'], 'questions': questions,
              'issues': result.get('issues'), 'error': result.get('error'), 'passed': all(checks.values())}
    (root / 'acceptance.json').write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
