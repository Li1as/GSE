"""Read-only cross-folder reply and MIME attachment acceptance."""
import argparse
import hashlib
import json
import os
from datetime import datetime, timezone
from email import policy
from email.parser import BytesParser
from email.utils import getaddresses

from live_mail_acceptance import flags
from mail_reader import BASE, Reader, ids, save_snapshot, thread_candidates


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('uid', help='Explicit INBOX reply UID selected for acceptance')
    parser.add_argument('--sent-folder', default='Sent Messages')
    args = parser.parse_args()
    os.umask(0o077)
    report = {'time': datetime.now(timezone.utc).isoformat(), 'checks': {}}
    checks = report['checks']
    reader = None
    try:
        config = json.loads((BASE / 'mail.local.json').read_text())
        reader = Reader(config)
        reader.select('INBOX')
        before = flags(reader, args.uid)
        reply, raw = reader.fetch(args.uid)
        reply_path = save_snapshot(BASE / 'data' / 'mail', reply, raw)
        checks['initially_unread'] = '\\Seen' not in before
        checks['reply_body'] = bool(reply['body'].strip())
        checks['reply_flags_unchanged'] = before == flags(reader, args.uid)
        checks['reply_snapshot'] = (reply_path / 'message.eml').read_bytes() == raw
        # Independently enumerate MIME attachment leaves from the saved EML.
        mime = BytesParser(policy=policy.default).parsebytes((reply_path / 'message.eml').read_bytes())
        decoded = []
        for part in mime.walk():
            if part.is_multipart():
                continue
            if part.get_content_disposition() == 'attachment' or part.get_filename():
                payload = part.get_payload(decode=True)
                if payload is None:
                    raise RuntimeError('Attachment has no decoded bytes')
                decoded.append({'filename': part.get_filename(),
                                'content_type': part.get_content_type(),
                                'size': len(payload),
                                'sha256': hashlib.sha256(payload).hexdigest()})
        checks['has_attachment'] = bool(decoded)
        checks['attachment_metadata_and_hashes'] = decoded == reply['attachments']
        checks['attachments_nonempty'] = bool(decoded) and all(a['size'] > 0 for a in decoded)
        reader.select(args.sent_folder)
        headers = reader.recent(30)
        candidates = thread_candidates(reply, headers)
        # Require an exact direct parent, not merely a common ancestor.
        direct = [h for h in candidates if ids(h['Message-ID']) & ids(reply['In-Reply-To'])]
        checks['direct_parent_found'] = len(direct) == 1
        if len(direct) != 1:
            raise RuntimeError('Expected one direct parent in bounded sent window')
        parent_before = flags(reader, direct[0]['uid'])
        parent, parent_raw = reader.fetch(direct[0]['uid'])
        parent_path = save_snapshot(BASE / 'data' / 'mail', parent, parent_raw)
        checks['parent_body'] = bool(parent['body'].strip())
        checks['parent_flags_unchanged'] = parent_before == flags(reader, parent['uid'])
        checks['references_contains_parent'] = bool(ids(parent['Message-ID']) & ids(reply['References']))
        checks['parent_snapshot'] = (parent_path / 'message.eml').read_bytes() == parent_raw
        addresses = lambda value: {address.lower() for _, address in getaddresses([value])}
        checks['parent_sent_by_account'] = config['address'].lower() in addresses(parent['From'])
        checks['reply_sender_was_recipient'] = bool(addresses(reply['From']) & addresses(parent['To']))
        checks['reply_to_account'] = config['address'].lower() in addresses(reply['To'])
        reader.select('INBOX')
        checks['reply_still_unread_at_end'] = flags(reader, args.uid) == before
        report.update(reply={'uid': args.uid, 'folder': 'INBOX', 'subject': reply['Subject'],
                             'snapshot': str(reply_path)},
                      parent={'uid': parent['uid'], 'folder': args.sent_folder,
                              'subject': parent['Subject'], 'snapshot': str(parent_path)},
                      sent_headers_count=len(headers), attachments=decoded,
                      flags_before=before, flags_after=flags(reader, args.uid),
                      smtp_tested=False, sent_messages=0)
        report['passed'] = all(checks.values())
    except Exception as error:
        report['passed'] = False
        report['error_type'] = type(error).__name__
    finally:
        if reader:
            reader.close()
    path = BASE / 'data' / 'mail' / 'thread-acceptance.json'
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
