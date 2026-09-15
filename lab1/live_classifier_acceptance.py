"""Real-API 3B acceptance over a copied real snapshot; no IMAP or SMTP."""
import hashlib
import json
import os
import shutil
import sqlite3
import uuid
from pathlib import Path

from assistant import API, ROOT, load_config
from mail_classifier import CATEGORIES, Classifier
from mail_inbox import Inbox


class CountingClient:
    def __init__(self, client):
        self.client, self.calls, self.messages = client, 0, []

    def complete(self, messages):
        self.calls += 1
        self.messages.append(messages)
        return self.client.complete(messages)


def main():
    os.umask(0o077)
    run = ROOT/'data/classifier-acceptance'/uuid.uuid4().hex
    run.mkdir(parents=True)
    report = {'checks': {}, 'model_calls': 0, 'sent_messages': 0}
    checks = report['checks']
    try:
        mail_config = json.loads((ROOT/'mail.local.json').read_text())
        source = Inbox(ROOT/'data/monitor', mail_config)
        rows = [row for row in source.rows() if row['status'] == 'ready' and row['snapshot']]
        if not rows:
            raise RuntimeError('No collected mail sample')
        selected = rows[-1]
        isolated = Inbox(run, mail_config)
        destination = run/'mail'/isolated.stream/Path(selected['snapshot']).name
        destination.parent.mkdir(parents=True)
        shutil.copytree(selected['snapshot'], destination)
        with isolated.connect() as db:
            db.execute('''INSERT INTO inbox(stream,validity,uid,status,attempts,retry_at,error,snapshot)
                VALUES (?,?,?,?,?,?,?,?)''',
                (isolated.stream, selected['validity'], selected['uid'], 'ready', 1, 0, None,
                 str(destination.resolve())))

        config = load_config(ROOT/'config.local.json')
        client = CountingClient(API(config))
        app = Classifier(isolated, client)
        row = app.once(1)[0]
        payload = json.loads(row['payload'])
        current = payload['current']
        checks['classified'] = row['status'] == 'classified'
        checks['valid_category'] = current['category'] in CATEGORIES
        checks['reason_present'] = bool(current['reason'].strip())
        checks['source_bound'] = payload['source_hash'] == hashlib.sha256(
            (destination/'message.eml').read_bytes()).hexdigest()
        checks['model_result_preserved'] = payload['model_result'] == current
        checks['idempotent_second_pass'] = app.once(1) == [] and client.calls == 1
        serialized_prompt = json.dumps(client.messages, ensure_ascii=False)
        checks['no_personal_corpus_config'] = all(value not in serialized_prompt for value in
            ('personal_data_dir', config['api_key']))
        checks['no_mail_task_or_send_table'] = not any(
            table in {'mail_tasks', 'mail_sends'} for table, in isolated.connect().execute(
                "SELECT name FROM sqlite_master WHERE type='table'"))

        correction_category = 'user_review' if current['category'] != 'user_review' else 'no_reply'
        corrected = app.correct(selected['validity'], selected['uid'], correction_category,
                                '隔离验收中的临时人工纠正。',
                                decision_question='是否需要回复？' if correction_category == 'user_review' else '')
        corrected_payload = json.loads(corrected['payload'])
        checks['correction_preserves_original'] = (
            corrected['status'] == 'corrected' and
            corrected_payload['model_result'] == current and
            corrected_payload['current']['kind'] == 'user_correction' and
            len(corrected_payload['history']) == 1 and client.calls == 1)
        checks['restart_preserves_correction'] = (
            Classifier(isolated, CountingClient(API(config))).rows()[0]['payload']['current']['category']
            == correction_category)
        report.update(category=current['category'], confidence=current['confidence'],
                      sample_uid=selected['uid'], passed=all(checks.values()))
    except Exception as error:
        report.update(passed=False, error_type=type(error).__name__)
    report['model_calls'] = locals().get('client').calls if 'client' in locals() else 0
    (run/'acceptance.json').write_text(json.dumps(report, ensure_ascii=False, indent=2)+'\n')
    public = {key: value for key, value in report.items() if key != 'sample_uid'}
    print(json.dumps(public, ensure_ascii=False))
    print('Private report: ' + str((run/'acceptance.json').relative_to(ROOT)))
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
