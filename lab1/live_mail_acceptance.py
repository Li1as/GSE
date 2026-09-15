"""Read-only real IMAP acceptance; private evidence stays under data/mail."""
import hashlib
import imaplib
import json
import os
from datetime import datetime, timezone

from mail_reader import BASE, Reader, parse_message, save_snapshot, thread_candidates


def flags(reader, uid):
    status, rows = reader.conn.uid('FETCH', uid, '(FLAGS)')
    if status != 'OK':
        raise RuntimeError('FLAGS fetch failed')
    return sorted(flag.decode('ascii') for row in rows if isinstance(row, bytes)
                  for flag in imaplib.ParseFlags(row))


def main():
    os.umask(0o077)
    reader = None
    report = {'time': datetime.now(timezone.utc).isoformat(), 'checks': {}}
    checks = report['checks']
    try:
        reader = Reader(json.loads((BASE / 'mail.local.json').read_text()))
        checks['login'] = True
        reader.select('INBOX')
        headers = reader.recent(30)
        checks['headers'] = bool(headers)
        if not headers:
            raise RuntimeError('Empty inbox')
        # Prefer an unread message so preservation of the unread flag is observable.
        selected = headers[0]
        before = flags(reader, selected['uid'])
        for candidate in headers:
            candidate_flags = flags(reader, candidate['uid'])
            if '\\Seen' not in candidate_flags:
                selected, before = candidate, candidate_flags
                break
        parsed, raw = reader.fetch(selected['uid'])
        directory = save_snapshot(BASE / 'data' / 'mail', parsed, raw)
        checks['body_parsed'] = bool(parsed['body'].strip())
        checks['header_matches'] = all(parsed[k] == selected[k] for k in
                                       ('Subject', 'Message-ID', 'From', 'Date'))
        checks['snapshot_bytes'] = (directory / 'message.eml').read_bytes() == raw
        checks['snapshot_json'] = json.loads((directory / 'message.json').read_text()) == parsed
        again, raw_again = reader.fetch(selected['uid'])
        checks['repeat_identity'] = save_snapshot(BASE / 'data' / 'mail', again, raw_again) == directory
        checks['repeat_content'] = raw_again == raw
        after = flags(reader, selected['uid'])
        checks['flags_unchanged'] = before == after
        # Find an actual pair in the bounded window, independently of selected body.
        pair = None
        for header in headers:
            others = [h for h in thread_candidates(header, headers) if h['uid'] != header['uid']]
            if others:
                pair = [header['uid'], others[0]['uid']]
                break
        report.update(headers_count=len(headers), selected_uid=selected['uid'],
                      initially_unread='\\Seen' not in before, flags_before=before,
                      flags_after=after, snapshot=str(directory),
                      raw_sha256=hashlib.sha256(raw).hexdigest(),
                      attachments_count=len(parsed['attachments']),
                      body_characters=len(parsed['body']), thread_pair=pair,
                      thread_status='observed_header_pair' if pair else 'no_pair_in_window',
                      smtp_tested=False, sent_messages=0)
        # Also verify that parsing the saved raw snapshot gives the same body.
        checks['snapshot_reparse'] = parse_message((directory / 'message.eml').read_bytes())['body'] == parsed['body']
        report['passed'] = all(checks.values())
    except Exception as error:
        report['passed'] = False
        report['error_type'] = type(error).__name__  # Never print server text or credentials.
    finally:
        if reader:
            reader.close()
    path = BASE / 'data' / 'mail' / 'acceptance.json'
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
