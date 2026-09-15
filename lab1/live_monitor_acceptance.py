"""Explicit read-only 3A acceptance in isolated local state. No model or SMTP."""
import json
import os
import uuid
from pathlib import Path

from mail_monitor import Monitor
from mail_reader import BASE, Reader, MailError
from live_mail_acceptance import flags


def main():
    os.umask(0o077)
    directory = BASE / 'data/monitor-acceptance' / uuid.uuid4().hex
    directory.mkdir(parents=True)
    checks, fetches = {}, []
    report = {'checks': checks, 'sent_messages': 0, 'model_calls': 0}
    reader = None
    try:
        config = json.loads((BASE/'mail.local.json').read_text())

        class CountingReader(Reader):
            def fetch(self, uid, header=False):
                fetches.append(uid)
                return super().fetch(uid, header)

        def app(hook=lambda event: None):
            return Monitor(config, directory, CountingReader, hook=hook)

        monitor = app()
        result = monitor.once()
        checks['baseline_active'] = result['state']['status'] == 'active'
        if not checks['baseline_active']:
            report['error_code'] = result['state'].get('error')
            raise RuntimeError('Baseline unavailable')
        checks['default_no_history_fetched'] = not fetches and not monitor.inbox.rows()
        reader = Reader(config)
        reader.select('INBOX')
        validity = reader.validity
        upper = reader.uid_next()-1
        uids = reader.uid_range(max(1, upper-99), upper) if upper else []
        if not uids:
            raise RuntimeError('No sample in bounded window')
        # All messages between these sample UIDs are captured and checked.
        uids = uids[-3:]
        before = {str(uid): flags(reader, str(uid)) for uid in uids}

        def crash(event):
            if event == 'after_cursor_commit':
                raise SystemExit('Injected after durable discovery')

        try:
            app(crash).once(accept_validity=validity, backfill=[min(uids), max(uids)])
        except SystemExit:
            pass
        checks['discovery_durable_before_fetch'] = all(
            any(r['uid'] == uid and r['status'] == 'pending' for r in monitor.inbox.rows(validity)) for uid in uids)

        def snapshot_crash(event):
            if event == 'after_snapshot':
                raise SystemExit('Injected after snapshot publication')

        try:
            app(snapshot_crash).once(batch=100)
        except SystemExit:
            pass
        result = app().once(batch=100)
        checks['restart_drains_queue'] = all(r['status'] == 'ready' for r in monitor.inbox.rows(validity))
        checks['snapshot_recovery_no_duplicate_fetch'] = all(fetches.count(str(uid)) == 1 for uid in uids)
        checks['snapshot_content_and_identity'] = all(monitor._snapshot(validity, uid) is not None for uid in uids)
        counts_before = len(monitor.inbox.rows(validity))
        calls_before = len(fetches)
        app().once(accept_validity=validity, backfill=[min(uids), max(uids)], batch=100)
        checks['duplicate_backfill_no_new_rows'] = len(monitor.inbox.rows(validity)) == counts_before
        checks['duplicate_backfill_no_refetch'] = len(fetches) == calls_before
        reader.select('INBOX')
        checks['generation_unchanged'] = reader.validity == validity
        checks['flags_preserved'] = checks['generation_unchanged'] and all(
            flags(reader, str(uid)) == before[str(uid)] for uid in uids)
        checks['private_database_permissions'] = monitor.inbox.db_path.stat().st_mode & 0o777 == 0o600
        report.update(sample_count=len(uids), initially_unread=sum('\\Seen' not in v for v in before.values()),
                      sampled_uids=uids, new_arrival_tested=False)
        report['passed'] = all(checks.values())
    except Exception as error:
        report.update(passed=False, error_type=type(error).__name__)
        if isinstance(error, MailError):
            report['error_code'] = error.code
            report['error'] = str(error)
    finally:
        if reader:
            reader.close()
        (directory/'acceptance.json').write_text(json.dumps(report, ensure_ascii=False, indent=2)+'\n')
    print(json.dumps({k: v for k, v in report.items() if k != 'sampled_uids'}, ensure_ascii=False))
    print('Private report: ' + str(directory.relative_to(BASE)/'acceptance.json'))
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
