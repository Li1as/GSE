"""Audit and export accepted mail locally; optionally inspect IMAP, never send."""
import argparse
import base64
import hashlib
import json
import os
import sqlite3
import tempfile
from email import policy
from email.parser import BytesParser
from email.utils import getaddresses

from assistant import Assistant, ROOT, load_config
from mail_tasks import MailTasks, digest
from mail_send import SendService


def atomic(path, data):
    fd, temporary = tempfile.mkstemp(dir=str(path.parent))
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data); stream.flush(); os.fsync(stream.fileno())
        os.replace(temporary, str(path))
    finally:
        if os.path.exists(temporary): os.unlink(temporary)


def audit(record):
    wire = base64.b64decode(record['wire_base64'], validate=True)
    content = record['content']
    message = BytesParser(policy=policy.default).parsebytes(wire)
    header_addresses = lambda header: [a for _, a in getaddresses(message.get_all(header, []))]
    normalize = lambda body: body.replace('\r\n', '\n').rstrip('\n')
    attachments = [{'name': p.get_filename(), 'size': len(p.get_payload(decode=True)),
                    'sha256': hashlib.sha256(p.get_payload(decode=True)).hexdigest()}
                   for p in message.iter_attachments()]
    checks = {'smtp_accepted': record['status'] == 'accepted' and record.get('smtp_code') == 250,
              'one_attempt': record['attempts'] == 1,
              'explicit_confirmation': bool(record.get('confirmed_at')),
              'content_fingerprint': digest(content) == record['fingerprint'],
              'wire_hash': hashlib.sha256(wire).hexdigest() == record['wire_sha256'],
              'message_id': str(message['Message-ID']) == record['message_id'],
              'from': header_addresses('From') == [content['from']],
              'to': header_addresses('To') == content['to'],
              'cc': header_addresses('Cc') == content['cc'],
              'bcc_not_exposed': message['Bcc'] is None,
              'subject': str(message['Subject']) == content['subject'],
              'body': normalize(message.get_body().get_content()) == normalize(content['body']),
              'attachments': attachments == content['attachments']}
    return checks, wire


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--send-id')
    parser.add_argument('--check-sent-folder', action='store_true')
    parser.add_argument('--user-confirmed-success', action='store_true', help='Record an explicitly reported successful real send')
    args = parser.parse_args()
    os.umask(0o077)
    tasks = MailTasks(Assistant(load_config(ROOT / 'config.local.json')))
    config = json.loads((ROOT / 'mail.local.json').read_text())
    sender = SendService(tasks, ROOT / 'data', config)
    with sqlite3.connect(str(tasks.db_path)) as db:
        rows = [json.loads(r[0]) for r in db.execute('SELECT payload FROM mail_sends ORDER BY rowid')]
    selected = [r for r in rows if r['status'] == 'accepted' and (not args.send_id or r['id'] == args.send_id)]
    if not selected: raise SystemExit('没有匹配的已接收发送记录。')
    record = selected[-1]
    if args.check_sent_folder:
        sender.reconcile(record['task_id'], record['id'])
        record = sender.get(record['task_id'], record['id'])
    checks, wire = audit(record)
    report = {'send_id': record['id'], 'task_id': record['task_id'], 'checks': checks,
              'passed': all(checks.values()), 'sent_folder_check': record.get('sent_folder_check'),
              'user_confirmed_success': args.user_confirmed_success,
              'delivery_evidence': '本脚本验证本地冻结内容及 SMTP 回执，不独立证明收件端投递。'}
    directory = ROOT / 'data' / 'mail-archive' / record['id']
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    atomic(directory / 'audit.json', json.dumps(report, ensure_ascii=False, indent=2).encode())
    if report['passed']:
        atomic(directory / 'message.eml', wire)
        atomic(directory / 'receipt.json', json.dumps(sender.public(record), ensure_ascii=False, indent=2).encode())
    print(json.dumps(dict(report, archive=str(directory)), ensure_ascii=False, indent=2))
    return 0 if report['passed'] else 1


if __name__ == '__main__': raise SystemExit(main())
