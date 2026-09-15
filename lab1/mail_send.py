"""Immutable previews, explicit confirmation and conservative SMTP delivery state."""
import base64
import hashlib
import json
import re
import smtplib
import sqlite3
import ssl
import uuid
from email.message import EmailMessage
from email.policy import SMTP
from email.utils import formatdate, make_msgid

from assistant import AppError
from mail_tasks import digest, now, require, text
from mail_reader import Reader, ids

LIMIT = 5 * 1024 * 1024


def addresses(values, required=False):
    require(isinstance(values, list) and len(values) <= 20 and (bool(values) or not required), '收件人列表无效。')
    for a in values:
        require(isinstance(a, str) and len(a) <= 254 and bool(re.fullmatch(r'[A-Za-z0-9.!#$%&\x27*+/=?^_`{|}~-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}', a)),
                '仅支持完整 ASCII 邮箱地址，不能包含显示名或换行。')
    return values


def deliver(config, envelope, raw, phase, factory=smtplib.SMTP_SSL):
    client = None
    in_data = False
    try:
        client = factory(config['smtp_host'], config['smtp_port'], timeout=25, context=ssl.create_default_context())
        client.ehlo_or_helo_if_needed()
        client.login(config['address'], config['password'])
        code, _ = client.mail(envelope['from'])
        if code != 250:
            return {'status': 'failed', 'stage': 'mail', 'smtp_code': code}
        for recipient in envelope['recipients']:
            code, _ = client.rcpt(recipient)
            if code not in (250, 251):
                # No DATA if even one recipient fails: never partially deliver.
                return {'status': 'failed', 'stage': 'rcpt', 'smtp_code': code}
        phase('data_started')  # Durable marker BEFORE the ambiguous operation.
        in_data = True
        code, _ = client.data(raw)
        if code == 250:
            return {'status': 'accepted', 'stage': 'data', 'smtp_code': code}
        return {'status': 'failed' if 400 <= code < 600 else 'unknown', 'stage': 'data', 'smtp_code': code}
    except smtplib.SMTPResponseException as error:
        return {'status': 'failed' if not in_data or 400 <= error.smtp_code < 600 else 'unknown',
                'stage': 'data' if in_data else 'before_data', 'smtp_code': error.smtp_code,
                'error_type': type(error).__name__}
    except (OSError, smtplib.SMTPException) as error:
        return {'status': 'unknown' if in_data else 'failed', 'stage': 'data' if in_data else 'before_data',
                'error_type': type(error).__name__}
    finally:
        if client:
            try:
                client.close()
            except Exception:
                pass  # Never undo SMTP acceptance because QUIT/close failed.


class SendService:
    def __init__(self, tasks, data, config, transport=deliver):
        self.tasks, self.data, self.config, self.transport = tasks, data, config, transport
        with sqlite3.connect(str(tasks.db_path)) as db:
            db.execute('CREATE TABLE IF NOT EXISTS mail_sends (id TEXT PRIMARY KEY, task_id TEXT NOT NULL, payload TEXT NOT NULL)')

    def save(self, record):
        with sqlite3.connect(str(self.tasks.db_path)) as db:
            db.execute('INSERT OR REPLACE INTO mail_sends VALUES (?,?,?)',
                       (record['id'], record['task_id'], json.dumps(record, ensure_ascii=False)))

    def get(self, task_id, send_id):
        with sqlite3.connect(str(self.tasks.db_path)) as db:
            row = db.execute('SELECT payload FROM mail_sends WHERE id=? AND task_id=?', (send_id, task_id)).fetchone()
        require(row is not None, '发送记录不属于此任务。')
        return json.loads(row[0])

    @staticmethod
    def public(record):
        return {k: v for k, v in record.items() if k != 'wire_base64'}

    def history(self, task_id):
        with sqlite3.connect(str(self.tasks.db_path)) as db:
            return [self.public(json.loads(r[0])) for r in db.execute(
                'SELECT payload FROM mail_sends WHERE task_id=? ORDER BY rowid', (task_id,))]

    def recover(self):
        with sqlite3.connect(str(self.tasks.db_path)) as db:
            pending = [json.loads(r[0]) for r in db.execute('SELECT payload FROM mail_sends')]
        for record in pending:
            if record['status'] != 'sending':
                continue
            try:
                with self.tasks.assistant.task_lock('mail:' + record['task_id']):
                    latest = self.get(record['task_id'], record['id'])
                    if latest['status'] == 'sending':
                        latest.update(status='unknown', recovery='重启发现未完成发送记录；未自动重发。')
                        self.save(latest)
            except AppError as error:
                if error.code != 'TASK_BUSY': raise

    def blob(self, item):
        require(isinstance(item, dict) and text(item.get('name'), 200) and
                not any(c in item['name'] for c in '\r\n/\\') and
                bool(re.fullmatch('[a-f0-9]{64}', item.get('sha256', ''))), '附件信息无效。')
        path = self.data / 'outgoing-blobs' / item['sha256']
        require(path.is_file() and not path.is_symlink() and path.stat().st_size <= LIMIT, '附件不存在或过大。')
        raw = path.read_bytes()
        require(hashlib.sha256(raw).hexdigest() == item['sha256'] and len(raw) == item.get('size'), '附件内容已变化，请重新上传并确认。')
        return raw

    def upload(self, name, encoded):
        require(text(name, 200) and not any(c in name for c in '\r\n/\\') and isinstance(encoded, str), '附件名或数据无效。')
        try:
            raw = base64.b64decode(encoded, validate=True)
        except ValueError:
            raise AppError('INPUT_ERROR', '附件编码无效。') from None
        require(0 < len(raw) <= LIMIT, '附件需为 1 字节到 5 MiB。')
        key = hashlib.sha256(raw).hexdigest()
        directory = self.data / 'outgoing-blobs'
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = directory / key
        if not path.exists():
            # Content addressed and atomically published; no caller-controlled paths.
            import tempfile, os
            fd, tmp = tempfile.mkstemp(dir=str(directory))
            try:
                with os.fdopen(fd, 'wb') as stream:
                    stream.write(raw); stream.flush(); os.fsync(stream.fileno())
                os.replace(tmp, str(path))
            finally:
                if os.path.exists(tmp): os.unlink(tmp)
        return {'name': name, 'size': len(raw), 'sha256': key}

    def content(self, task):
        require(self.config is not None, '未配置 SMTP。')
        require(not task.get('automation_paused'), '来源邮件当前分类不允许发送，请先核对分类。')
        require(not task.get('conversation_review_required'), '检测到新的相关往来，请先选择纳入或忽略。')
        require(task['status'] == 'draft_ready' and task.get('freshness') == 'current', '草稿尚未就绪或来源已过时。')
        d = task['draft']
        require(self.config['address'].lower() == task['mail']['account'].lower(), '发件账号与邮件任务不一致。')
        addresses([self.config['address']], True)
        require(text(d['subject'], 500) and not any(c in d['subject'] for c in '\r\n') and text(d['body'], 16000), '主题或正文无效。')
        attached = d.get('attachments', [])
        require(isinstance(attached, list) and len(attached) <= 5, '最多五个附件，总计不超过 5 MiB。')
        payloads = [self.blob(item) for item in attached]
        require(sum(map(len, payloads)) <= LIMIT, '附件总大小超过 5 MiB。')
        return {'version': d['version'], 'from': self.config['address'], 'to': addresses(d['to'], True),
                'cc': addresses(d.get('cc', [])), 'bcc': addresses(d.get('bcc', [])),
                'subject': d['subject'], 'body': d['body'], 'attachments': attached,
                'dependencies': d['dependencies']}, payloads

    def check_terminal(self, task_id):
        require(not any(r['status'] in ('sending', 'unknown', 'accepted') for r in self.history(task_id)),
                '此任务已有发送中、结果不明或已接收记录，不能再次发送。请先核对记录。')
        task = self.tasks.get(task_id)
        thread_id = task.get('thread_id')
        if not thread_id:
            return
        with sqlite3.connect(str(self.tasks.db_path)) as db:
            related = [row[0] for row in db.execute('SELECT id FROM mail_tasks WHERE id<>?', (task_id,))]
        for other_id in related:
            other = self.tasks.get(other_id)
            if other.get('thread_id') != thread_id:
                continue
            require(not any(r['status'] in ('sending', 'unknown') for r in self.history(other_id)),
                    '同一往来存在发送中或结果不明记录，不能从另一任务发送。')

    def preview(self, task_id, version):
        with self.tasks.assistant.task_lock('mail:' + task_id):
            self.check_terminal(task_id)
            task = self.tasks.get(task_id)
            content, payloads = self.content(task)
            require(type(version) is int and version == content['version'], '草稿版本已变化，请刷新。')
            fingerprint = digest(content)
            for r in self.history(task_id):
                if r['fingerprint'] == fingerprint and r['status'] in ('preview', 'confirmed'):
                    return r
            msg = EmailMessage(policy=SMTP)
            msg['From'], msg['To'] = content['from'], ', '.join(content['to'])
            if content['cc']: msg['Cc'] = ', '.join(content['cc'])
            msg['Subject'], msg['Date'] = content['subject'], formatdate(localtime=False)
            msg['Message-ID'] = make_msgid(domain=content['from'].split('@')[1])
            parent_ids = ids(task['mail'].get('Message-ID', ''))
            if len(parent_ids) == 1:
                parent = next(iter(parent_ids))
                msg['In-Reply-To'] = parent
                msg['References'] = ' '.join(sorted(ids(task['mail'].get('References', '')) - {parent}) + [parent])
            msg.set_content(content['body'], cte='quoted-printable')
            for item, raw in zip(content['attachments'], payloads):
                msg.add_attachment(raw, maintype='application', subtype='octet-stream', filename=item['name'])
            wire = msg.as_bytes()
            record = {'id': uuid.uuid4().hex, 'task_id': task_id, 'status': 'preview', 'content': content,
                      'fingerprint': fingerprint, 'message_id': str(msg['Message-ID']), 'created_at': now(),
                      'wire_sha256': hashlib.sha256(wire).hexdigest(), 'wire_base64': base64.b64encode(wire).decode(),
                      'attempts': 0}
            self.save(record)
            return self.public(record)

    def validate_current(self, record):
        content, _ = self.content(self.tasks.get(record['task_id']))
        require(digest(content) == record['fingerprint'], '内容、来源或附件已变化，原确认失效；请重新预览。')

    def confirm(self, task_id, send_id, fingerprint):
        with self.tasks.assistant.task_lock('mail:' + task_id):
            self.check_terminal(task_id)
            record = self.get(task_id, send_id)
            require(record['status'] in ('preview', 'confirmed') and record['fingerprint'] == fingerprint, '确认版本不匹配。')
            self.validate_current(record)
            record.update(status='confirmed', confirmed_at=record.get('confirmed_at', now()))
            self.save(record)
            return self.public(record)

    def send(self, task_id, send_id):
        with self.tasks.assistant.task_lock('mail:' + task_id):
            record = self.get(task_id, send_id)
            if record['status'] in ('accepted', 'unknown', 'failed'):
                return self.public(record)
            if record['status'] == 'sending':
                record.update(status='unknown', recovery='发送进程曾中断，不能自动重发。')
                self.save(record)
                return self.public(record)
            require(record['status'] == 'confirmed', '尚未确认具体内容，不能发送。')
            self.check_terminal(task_id)
            self.validate_current(record)
            raw = base64.b64decode(record['wire_base64'])
            require(hashlib.sha256(raw).hexdigest() == record['wire_sha256'], '冻结邮件校验失败。')
            record.update(status='sending', started_at=now(), attempts=1, stage='before_data')
            self.save(record)
            def phase(value):
                record['stage'] = value
                self.save(record)
            content = record['content']
            envelope = {'from': content['from'], 'recipients': list(dict.fromkeys(content['to'] + content['cc'] + content['bcc']))}
            try:
                outcome = self.transport(self.config, envelope, raw, phase)
            except Exception as error:
                outcome = {'status': 'unknown', 'stage': record['stage'], 'error_type': type(error).__name__}
            require(outcome.get('status') in ('accepted', 'unknown', 'failed'), '发送器状态无效。')
            record.update(outcome, finished_at=now())
            self.save(record)
            return self.public(record)

    def reconcile(self, task_id, send_id, folder='Sent Messages'):
        with self.tasks.assistant.task_lock('mail:' + task_id):
            record = self.get(task_id, send_id)
            if record['status'] == 'sending':
                record.update(status='unknown', recovery='发送进程已中断，等待外部核对。')
                self.save(record)
            require(record['status'] in ('unknown', 'accepted'), '仅核对结果不明或已接收的记录。')
            reader = Reader(self.config)
            try:
                reader.select(folder)
                status, values = reader.conn.uid('SEARCH', None, 'HEADER', 'Message-ID', '"' + record['message_id'] + '"')
                require(status == 'OK', '无法查询已发送目录。')
                matches = (values[0] or b'').split()
                observed = []
                for uid in matches[:10]:
                    m, _ = reader.fetch(uid.decode(), header=True)
                    if record['message_id'] in ids(m['Message-ID']):
                        observed.append({'uid': m['uid'], 'uidvalidity': m['uidvalidity'], 'folder': folder})
                record['sent_folder_check'] = {'at': now(), 'matches': observed,
                    'meaning': '仅证明该 Message-ID 在目录中存在；未证明投递。未找到也不证明发送失败。'}
                self.save(record)
            finally:
                reader.close()
            return self.public(record)
