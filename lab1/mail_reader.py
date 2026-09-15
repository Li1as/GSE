"""Stage 2A: bounded, read-only IMAP access. No model or SMTP calls."""
import argparse
import hashlib
import imaplib
import json
import os
from pathlib import Path
import re
import socket
import ssl
import sys
from email import policy
from email.parser import BytesParser
from html.parser import HTMLParser

BASE = Path(__file__).resolve().parent
MAX_BYTES = 10 * 1024 * 1024


class MailError(Exception):
    pass


class TimedIMAP(imaplib.IMAP4_SSL):
    def _create_socket(self, *args, **kwargs):
        sock = socket.create_connection((self.host, self.port), timeout=20)
        try:
            return self.ssl_context.wrap_socket(sock, server_hostname=self.host)
        except Exception:
            sock.close()
            raise


class PlainHTML(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in ('script', 'style'):
            self.hidden += 1
        if tag in ('br', 'p', 'div', 'li', 'tr'):
            self.parts.append('\n')

    def handle_endtag(self, tag):
        if tag in ('script', 'style'):
            self.hidden = max(0, self.hidden - 1)

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def parse_message(raw):
    msg = BytesParser(policy=policy.default).parsebytes(raw)
    result = {key: str(msg.get(key, '')) for key in
              ('Subject', 'From', 'To', 'Cc', 'Reply-To', 'Date',
               'Message-ID', 'In-Reply-To', 'References')}
    body = msg.get_body(preferencelist=('plain', 'html'))
    text = body.get_content(errors='replace') if body else ''
    if body and body.get_content_type() == 'text/html':
        parser = PlainHTML()
        parser.feed(text)
        text = ''.join(parser.parts)
    result['body'] = text
    result['attachments'] = []
    for part in msg.iter_attachments():
        payload = part.get_payload(decode=True)
        if payload is None:
            payload = part.as_bytes()
        result['attachments'].append({
            'filename': part.get_filename(), 'content_type': part.get_content_type(),
            'size': len(payload), 'sha256': hashlib.sha256(payload).hexdigest()})
    return result


def ids(value):
    return set(re.findall(r'<[^<>\s]+>', value))


def thread_candidates(selected, headers):
    """Conservative header-based candidates; subject alone is insufficient."""
    anchors = ids(selected['Message-ID'] + ' ' + selected['References'] +
                  ' ' + selected['In-Reply-To'])
    return [h for h in headers if anchors & ids(
        h['Message-ID'] + ' ' + h['References'] + ' ' + h['In-Reply-To'])]


class Reader:
    def __init__(self, config, factory=TimedIMAP):
        self.config = config
        self.conn = None
        try:
            self.conn = factory(config['imap_host'], config['imap_port'],
                                ssl_context=ssl.create_default_context())
            self.conn.login(config['address'], config['password'])
        except imaplib.IMAP4.error:
            self.close()
            raise MailError('AUTH_FAILED：请检查客户端服务与客户端专用密码；不自动重试。') from None
        except (OSError, EOFError):
            self.close()
            raise MailError('CONNECT_FAILED：无法建立或维持 IMAP TLS 连接。') from None

    def close(self):
        if self.conn:
            try:
                self.conn.logout()
            except Exception:
                pass

    def select(self, folder):
        if not folder or any(ord(c) < 32 or ord(c) > 126 for c in folder):
            raise MailError('2A 仅支持 ASCII 文件夹名，例如 INBOX。')
        quoted = '"' + folder.replace('\\', '\\\\').replace('"', '\\"') + '"'
        status, _ = self.conn.select(quoted, readonly=True)
        if status != 'OK':
            raise MailError('无法以只读方式打开文件夹。')
        _, values = self.conn.response('UIDVALIDITY')
        if not values or not values[0] or not values[0].isdigit():
            raise MailError('服务器没有返回有效 UIDVALIDITY。')
        self.folder = folder
        self.validity = values[0].decode('ascii')

    def fetch(self, uid, header=False):
        uid = str(uid)
        if not re.fullmatch(r'[1-9][0-9]*', uid):
            raise MailError('UID 必须是正整数。')
        status, rows = self.conn.uid('FETCH', uid, '(RFC822.SIZE)')
        sizes = [re.search(rb'RFC822.SIZE\s+(\d+)', row) for row in rows or []
                 if isinstance(row, bytes)]
        sizes = [int(m.group(1)) for m in sizes if m]
        if status != 'OK' or len(sizes) != 1:
            raise MailError('邮件不存在或无法读取大小。')
        if sizes[0] > MAX_BYTES:
            raise MailError('邮件超过 10 MiB，2A 暂不读取。')
        query = '(BODY.PEEK[HEADER])' if header else '(BODY.PEEK[])'
        status, rows = self.conn.uid('FETCH', uid, query)
        chunks = [row[1] for row in rows or [] if isinstance(row, tuple)]
        if status != 'OK' or len(chunks) != 1 or len(chunks[0]) > MAX_BYTES:
            raise MailError('邮件读取失败或超过大小限制。')
        raw = chunks[0]
        result = parse_message(raw)
        result.update(account=self.config['address'], folder=self.folder,
                      uidvalidity=self.validity, uid=uid)
        return result, raw

    def recent(self, limit):
        if not 1 <= limit <= 100:
            raise MailError('limit 应在 1 到 100 之间。')
        status, rows = self.conn.uid('SEARCH', None, 'ALL')
        if status != 'OK':
            raise MailError('无法列出邮件。')
        uids = (rows[0] or b'').split() if rows else []
        return [self.fetch(uid.decode('ascii'), header=True)[0]
                for uid in reversed(uids[-limit:])]


def save_snapshot(root, parsed, raw):
    identity = [parsed[k] for k in ('account', 'folder', 'uidvalidity', 'uid')]
    key = hashlib.sha256(json.dumps(identity).encode()).hexdigest()
    directory = root / key
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    # Publish metadata last. Re-reading the same IMAP identity reuses the same path.
    for name, data in [('message.eml', raw), ('message.json',
                       json.dumps(parsed, ensure_ascii=False, indent=2).encode())]:
        path = directory / name
        import tempfile
        fd, tmp = tempfile.mkstemp(dir=str(directory), prefix='.snapshot-')
        try:
            with os.fdopen(fd, 'wb') as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(tmp, str(path))
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
    return directory


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, default=BASE / 'mail.local.json')
    parser.add_argument('--folder', default='INBOX')
    parser.add_argument('--limit', type=int, default=10)
    parser.add_argument('command', choices=['probe', 'list', 'read', 'thread'])
    parser.add_argument('uid', nargs='?')
    args = parser.parse_args()
    reader = None
    try:
        config = json.loads(args.config.read_text())
        if not all(config.get(k) for k in ('address', 'password', 'imap_host', 'imap_port')):
            raise MailError('请填写本地邮件配置及客户端专用密码。')
        if args.command in ('read', 'thread') and not args.uid:
            raise MailError('read/thread 需要 UID，请先 list。')
        reader = Reader(config)
        if args.command == 'probe':
            print('IMAP TLS 登录成功；未读取邮件、未验证 SMTP。')
            return 0
        reader.select(args.folder)
        if args.command == 'list':
            result = reader.recent(args.limit)
        else:
            selected, raw = reader.fetch(args.uid)
            path = save_snapshot(BASE / 'data' / 'mail', selected, raw)
            result = {'snapshot': str(path), 'message': selected}
            if args.command == 'thread':
                result['candidate_headers'] = thread_candidates(selected, reader.recent(args.limit))
                result['scope'] = '仅当前文件夹最近 limit 封邮件的头部候选；不是完整会话。'
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (MailError, OSError, ValueError, KeyError, TypeError, LookupError,
            imaplib.IMAP4.error, EOFError):
        # Server text can echo credentials or message content; never print it.
        error = sys.exc_info()[1]
        print(str(error) if isinstance(error, MailError) else
              '邮件操作失败：请检查配置、网络和服务状态；未自动重试。', file=sys.stderr)
        return 2
    finally:
        if reader:
            reader.close()


if __name__ == '__main__':
    sys.exit(main())
