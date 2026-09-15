"""Read-only incremental IMAP collection with durable recovery. Stage 3A only."""
import argparse
import hashlib
import imaplib
import json
import os
import time
from pathlib import Path

from mail_inbox import Inbox
from mail_reader import BASE, Reader, MailError, parse_message, save_snapshot, MAX_BYTES
from persistence import AppError


class Monitor:
    def __init__(self, config, directory=None, reader_factory=Reader, clock=time.time,
                 hook=lambda event: None):
        self.config = config
        self.inbox = Inbox(directory or BASE / 'data/monitor', config)
        self.factory, self.clock, self.hook = reader_factory, clock, hook
        self.snapshots = self.inbox.directory / 'mail' / self.inbox.stream

    def _failure(self, state, error):
        code = getattr(error, 'code', 'CONNECT_FAILED')
        failures = state.get('failures', 0) + 1
        paused = code in ('AUTH_FAILED', 'PROTOCOL')
        state.update(status='paused' if paused else 'retry_wait', error=code,
                     failures=failures, retry_at=self.clock() + min(900, 30 * 2 ** min(failures-1, 5)))
        self.inbox.save_state(state)

    def _snapshot(self, validity, uid):
        identity = [self.config['address'], 'INBOX', validity, str(uid)]
        directory = self.snapshots / hashlib.sha256(json.dumps(identity).encode()).hexdigest()
        try:
            raw_path = directory / 'message.eml'
            if raw_path.stat().st_size > MAX_BYTES:
                return None
            raw = raw_path.read_bytes()
            saved = json.loads((directory / 'message.json').read_text())
            parsed = parse_message(raw)
            if any(saved.get(k) != v for k, v in parsed.items()):
                return None
            if [saved.get(k) for k in ('account', 'folder', 'uidvalidity', 'uid')] != identity:
                return None
            return directory, parsed
        except (OSError, ValueError, TypeError, LookupError):
            return None

    def _ready(self, validity, uid, directory, parsed):
        too_long = len(parsed['body']) > 24000
        self.inbox.update(validity, uid, status='needs_review' if too_long else 'ready',
                          error='BODY_LIMIT' if too_long else None, snapshot=str(directory.resolve()), retry_at=0)

    def _collect(self, reader, state, limit):
        now = self.clock()
        rows = [r for r in self.inbox.rows(state['validity'])
                if r['status'] in ('pending', 'fetching', 'retry') and r['retry_at'] <= now]
        # All selected jobs run under the stream lock. fetching means a prior owner died.
        for row in rows[:limit]:
            uid, validity = row['uid'], row['validity']
            recovered = self._snapshot(validity, uid)
            if recovered:
                self._ready(validity, uid, *recovered)
                continue
            if row['attempts'] >= 3:
                self.inbox.update(validity, uid, status='needs_review', error='RETRY_LIMIT')
                continue
            attempts = row['attempts'] + 1
            self.inbox.update(validity, uid, status='fetching', attempts=attempts, retry_at=0)
            self.hook('before_fetch')
            try:
                parsed, raw = reader.fetch(str(uid))
                expected = [self.config['address'], 'INBOX', validity, str(uid)]
                if [parsed.get(k) for k in ('account', 'folder', 'uidvalidity', 'uid')] != expected:
                    raise MailError('采集身份不匹配。', 'PROTOCOL')
                directory = save_snapshot(self.snapshots, parsed, raw)
                self.hook('after_snapshot')
                self._ready(validity, uid, directory, parsed)
            except MailError as error:
                permanent = error.code in ('TOO_LARGE', 'PROTOCOL', 'INPUT_ERROR')
                self.inbox.update(validity, uid, status='needs_review' if permanent or attempts >= 3 else 'retry',
                                  error=error.code, retry_at=now+30*2**(attempts-1))
                if error.code == 'AUTH_FAILED':
                    raise
            except (ValueError, TypeError, LookupError):
                self.inbox.update(validity, uid, status='needs_review', error='PARSE_FAILED')
            except (OSError, EOFError, imaplib.IMAP4.error):
                self.inbox.update(validity, uid, status='needs_review' if attempts >= 3 else 'retry',
                                  error='IO_FAILED', retry_at=now+30*2**(attempts-1))
                raise

    def retry(self, uid, validity):
        if not uid or uid < 1 or not validity:
            raise ValueError('重试须指定正整数 UID 和当前 UIDVALIDITY。')
        with self.inbox.task_lock('monitor:' + self.inbox.stream):
            state = self.inbox.state()
            if not state or state.get('validity') != validity or state['status'] == 'validity_changed':
                raise ValueError('只能重试当前已核对代的采集记录。')
            row = next((r for r in self.inbox.rows(validity) if r['uid'] == uid), None)
            if row is None or row['status'] not in ('retry', 'needs_review'):
                raise ValueError('没有可重试的失败记录。')
            self.inbox.update(validity, uid, status='pending', attempts=0, retry_at=0)
            return self.inbox.summary()

    def once(self, batch=50, span=1000, include_existing=False, resume=False,
             accept_validity=None, backfill=None):
        if not (type(batch) is int and 1 <= batch <= 100 and type(span) is int and 1 <= span <= 10000):
            raise ValueError('batch 必须为 1—100，span 必须为 1—10000。')
        if backfill and not (accept_validity and 1 <= backfill[0] <= backfill[1] and
                             backfill[1]-backfill[0]+1 <= span):
            raise ValueError('历史补录须指定当前 UIDVALIDITY 和一个不超过 span 的有效 UID 区间。')
        with self.inbox.task_lock('monitor:' + self.inbox.stream):
            state = self.inbox.state() or {'status': 'new', 'failures': 0, 'retry_at': 0}
            if not resume and not accept_validity and (
                    state['status'] in ('paused', 'validity_changed') or state.get('retry_at', 0) > self.clock()):
                return self.inbox.summary()
            reader = None
            try:
                reader = self.factory(self.config)
                reader.select('INBOX')
                validity, upper = reader.validity, reader.uid_next()-1
                if accept_validity and accept_validity != validity:
                    raise ValueError('指定的 UIDVALIDITY 与服务器不一致。')
                if 'validity' in state and state['validity'] != validity and accept_validity != validity:
                    state.update(status='validity_changed', observed_validity=validity, error='UIDVALIDITY_CHANGED')
                    self.inbox.save_state(state)
                    return self.inbox.summary()
                if 'validity' not in state or state['validity'] != validity:
                    state = dict(status='active', validity=validity, cursor=0 if include_existing else upper,
                                 baseline=upper, initialized_at=self.clock(), failures=0, retry_at=0)
                    self.inbox.save_state(state)
                if upper < state['cursor']:
                    raise MailError('相同代的 UIDNEXT 回退，需人工核对。', 'PROTOCOL')
                end = min(upper, state['cursor'] + span)
                if end > state['cursor']:
                    start = state['cursor']+1
                    uids = reader.uid_range(start, end)
                    # Defense in depth even with an alternate Reader implementation.
                    uids = sorted({u for u in uids if type(u) is int and start <= u <= end})
                    state = self.inbox.discover(state, uids, end, self.hook)
                    self.hook('after_cursor_commit')
                if backfill:
                    uids = reader.uid_range(*backfill)
                    uids = sorted({u for u in uids if backfill[0] <= u <= backfill[1]})
                    state = self.inbox.discover(state, uids, state['cursor'], self.hook)
                    self.hook('after_cursor_commit')
                state.update(status='active', error=None, failures=0, retry_at=0, last_scan=self.clock())
                self.inbox.save_state(state)
                self._collect(reader, state, batch)
            except (MailError, OSError, EOFError, imaplib.IMAP4.error) as error:
                self._failure(state, error)
            finally:
                if reader:
                    reader.close()
            return self.inbox.summary()


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['once', 'run', 'status', 'list', 'retry'])
    parser.add_argument('--config', type=Path, default=BASE/'mail.local.json')
    parser.add_argument('--data-dir', type=Path, default=BASE/'data/monitor')
    parser.add_argument('--batch', type=int, default=50)
    parser.add_argument('--span', type=int, default=1000)
    parser.add_argument('--interval', type=int, default=60)
    parser.add_argument('--include-existing', action='store_true', help='Only affects initial baseline or explicit validity reset')
    parser.add_argument('--resume', action='store_true', help='Explicitly retry a paused connection')
    parser.add_argument('--accept-validity', help='Explicitly accept current UID generation; preserves old queue rows')
    parser.add_argument('--uid', type=int, help='UID of a failed collection to retry')
    parser.add_argument('--backfill', nargs=2, type=int, metavar=('START_UID', 'END_UID'))
    args = parser.parse_args()
    try:
        if args.interval < 5:
            raise ValueError('interval 至少为 5 秒。')
        config = json.loads(args.config.read_text())
        if not all(config.get(k) for k in ('address', 'password', 'imap_host', 'imap_port')):
            raise ValueError('邮箱配置不完整。')
        app = Monitor(config, args.data_dir)
        if args.command == 'list':
            print(json.dumps(app.inbox.rows(), ensure_ascii=False))
            return 0
        if args.command == 'retry':
            print(json.dumps(app.retry(args.uid, args.accept_validity), ensure_ascii=False))
            return 0
        if args.command == 'status':
            print(json.dumps(app.inbox.summary(), ensure_ascii=False))
            return 0
        while True:
            result = app.once(args.batch, args.span, args.include_existing, args.resume,
                              args.accept_validity, args.backfill)
            print(json.dumps(result, ensure_ascii=False), flush=True)
            if args.command == 'once':
                return 0 if result['state']['status'] == 'active' else 2
            # Explicit overrides apply only once, never defeat automatic backoff.
            args.resume, args.accept_validity, args.backfill = False, None, None
            time.sleep(args.interval)
    except KeyboardInterrupt:
        return 0
    except (OSError, ValueError, KeyError, TypeError, AppError):
        print('监控操作失败：请检查配置、参数、磁盘或是否已有进程运行。')
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
