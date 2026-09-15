"""Stages 4B/4C: durable ehall tasks, preparation, editing and readback."""
import base64
import hashlib
import json
import os
import re
import sqlite3
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path

from ehall_adapters import default_adapters
from ehall_browser import normalize_url, public_url
from persistence import AppError


MAX_ATTACHMENT_BYTES = 5 * 1024 * 1024


def now():
    return datetime.now(timezone.utc).isoformat()


def require(ok, code, message):
    if not ok:
        raise AppError(code, message)


def digest(value):
    return hashlib.sha256(json.dumps(
        value, ensure_ascii=False, sort_keys=True,
        separators=(',', ':')).encode()).hexdigest()


def validate_attachments(items):
    if items is None:
        return []
    require(isinstance(items, list) and len(items) <= 10, 'INPUT_ERROR', '附件元数据无效。')
    result = []
    for item in items:
        require(isinstance(item, dict), 'INPUT_ERROR', '附件元数据无效。')
        name, size, sha256 = item.get('name'), item.get('size'), item.get('sha256')
        require(isinstance(name, str) and name.strip() and len(name) <= 255 and
                '/' not in name and '\\' not in name and name not in ('.', '..'),
                'INPUT_ERROR', '附件名无效。')
        require(type(size) is int and 0 < size <= MAX_ATTACHMENT_BYTES,
                'INPUT_ERROR', '附件大小无效。')
        require(isinstance(sha256, str) and re.fullmatch(r'[0-9a-f]{64}', sha256) is not None,
                'INPUT_ERROR', '附件 SHA-256 无效。')
        value = {'name': name.strip(), 'size': size, 'sha256': sha256}
        content_type = item.get('content_type')
        if content_type is not None:
            require(isinstance(content_type, str) and len(content_type) <= 200,
                    'INPUT_ERROR', '附件类型无效。')
            value['content_type'] = content_type
        result.append(value)
    return result


class EhallAttachmentStore:
    """Private, content-addressed uploads; task rows only contain metadata."""

    def __init__(self, directory):
        self.directory = Path(directory)

    def upload(self, name, encoded, content_type=None):
        require(isinstance(name, str) and name.strip() and len(name) <= 255 and
                '/' not in name and '\\' not in name and name not in ('.', '..') and
                isinstance(encoded, str), 'INPUT_ERROR', '附件名或数据无效。')
        try:
            raw = base64.b64decode(encoded, validate=True)
        except (ValueError, TypeError):
            raise AppError('INPUT_ERROR', '附件编码无效。') from None
        require(0 < len(raw) <= MAX_ATTACHMENT_BYTES, 'INPUT_ERROR',
                '附件需为 1 字节到 5 MiB。')
        key = hashlib.sha256(raw).hexdigest()
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = self.directory / key
        if not path.exists():
            fd, temporary = tempfile.mkstemp(dir=str(self.directory), prefix='.upload-')
            try:
                with os.fdopen(fd, 'wb') as stream:
                    stream.write(raw)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, str(path))
                path.chmod(0o600)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
        item = {'name': name.strip(), 'size': len(raw), 'sha256': key}
        if content_type is not None:
            require(isinstance(content_type, str) and len(content_type) <= 200,
                    'INPUT_ERROR', '附件类型无效。')
            item['content_type'] = content_type
        return item

    def verify(self, item):
        value = validate_attachments([item])[0]
        path = self.directory / value['sha256']
        require(path.is_file() and not path.is_symlink(), 'ATTACHMENT_CHANGED',
                '附件不存在或已被替换。')
        raw = path.read_bytes()
        require(len(raw) == value['size'] and hashlib.sha256(raw).hexdigest() == value['sha256'],
                'ATTACHMENT_CHANGED', '附件内容已变化，请重新上传。')
        return path


class EhallTasks:
    """Owns ehall parents; Assistant continues to own personal-info children."""

    def __init__(self, assistant, adapters=None, attachment_store=None):
        self.assistant = assistant
        self.db_path = assistant.db_path
        self.adapters = adapters or default_adapters()
        self.attachment_store = attachment_store
        with sqlite3.connect(str(self.db_path)) as db:
            db.execute('CREATE TABLE IF NOT EXISTS ehall_tasks '
                       '(id TEXT PRIMARY KEY, adapter TEXT NOT NULL, payload TEXT NOT NULL)')
            db.execute('CREATE INDEX IF NOT EXISTS ehall_tasks_adapter ON ehall_tasks(adapter)')
            db.execute('''CREATE TABLE IF NOT EXISTS ehall_task_actions (
                id TEXT PRIMARY KEY, task_id TEXT NOT NULL, action TEXT NOT NULL,
                acted_at TEXT NOT NULL, payload TEXT NOT NULL)''')
            db.execute('CREATE INDEX IF NOT EXISTS ehall_actions_task '
                       'ON ehall_task_actions(task_id, acted_at)')

    def save(self, task):
        with sqlite3.connect(str(self.db_path)) as db:
            db.execute('INSERT OR REPLACE INTO ehall_tasks VALUES (?,?,?)',
                       (task['task_id'], task['adapter'], json.dumps(task, ensure_ascii=False)))

    def get(self, task_id):
        with sqlite3.connect(str(self.db_path)) as db:
            row = db.execute('SELECT payload FROM ehall_tasks WHERE id=?', (task_id,)).fetchone()
        if not row:
            raise AppError('EHALL_NOT_FOUND', 'ehall 任务不存在。')
        return json.loads(row[0])

    @staticmethod
    def public(task):
        value = json.loads(json.dumps(task, ensure_ascii=False))
        value.pop('url', None)
        return value

    def actions(self, task_id):
        self.get(task_id)
        with sqlite3.connect(str(self.db_path)) as db:
            return [json.loads(row[0]) for row in db.execute(
                'SELECT payload FROM ehall_task_actions WHERE task_id=? ORDER BY acted_at,rowid',
                (task_id,))]

    def is_archived(self, task_id):
        actions = self.actions(task_id)
        return bool(actions and actions[-1]['action'] == 'archive')

    def archived_task_ids(self):
        with sqlite3.connect(str(self.db_path)) as db:
            rows = db.execute('SELECT task_id,payload FROM ehall_task_actions '
                              'ORDER BY acted_at,rowid').fetchall()
        latest = {}
        for task_id, payload in rows:
            latest[task_id] = json.loads(payload)['action']
        return {task_id for task_id, action in latest.items() if action == 'archive'}

    def summaries(self, archived=False, limit=None):
        with sqlite3.connect(str(self.db_path)) as db:
            rows = [json.loads(row[0]) for row in db.execute(
                'SELECT payload FROM ehall_tasks ORDER BY rowid DESC')]
        archived_ids = self.archived_task_ids()
        rows = [task for task in rows if (task['task_id'] in archived_ids) == archived]
        if limit is not None:
            rows = rows[:limit]
        return [{'task_id': task['task_id'], 'transaction_name': task['transaction_name'],
                 'status': task['status'], 'created_at': task['created_at'],
                 'public_url': task['public_url']} for task in rows]

    def _record_action(self, task_id, action):
        record = {'id': uuid.uuid4().hex, 'task_id': task_id,
                  'action': action, 'acted_at': now()}
        with sqlite3.connect(str(self.db_path)) as db:
            db.execute('INSERT INTO ehall_task_actions VALUES (?,?,?,?,?)',
                       (record['id'], task_id, action, record['acted_at'],
                        json.dumps(record, ensure_ascii=False)))
        return record

    def archive(self, task_id):
        with self.assistant.task_lock('ehall:' + task_id):
            task = self.get(task_id)
            require(task['status'] == 'succeeded', 'STATE_CONFLICT',
                    '只能归档有可验证成功回执的 ehall 任务。')
            if not self.is_archived(task_id):
                self._record_action(task_id, 'archive')
            return self.get(task_id)

    def restore(self, task_id):
        with self.assistant.task_lock('ehall:' + task_id):
            task = self.get(task_id)
            require(self.is_archived(task_id), 'STATE_CONFLICT', '该 ehall 任务未归档。')
            self._record_action(task_id, 'restore')
            return task

    def _adapter_for_url(self, url):
        matches = [adapter for adapter in self.adapters.values() if adapter.matches(url)]
        require(len(matches) == 1, 'UNSUPPORTED_TRANSACTION', '该 URL 尚无明确的 ehall 事务适配器。')
        return matches[0]

    def create(self, raw_url, description='', attachments=None):
        url = normalize_url(raw_url)
        require(isinstance(description, str) and len(description) <= 4000,
                'INPUT_ERROR', '任务简要描述须为 0—4000 字符。')
        adapter = self._adapter_for_url(url)
        attachment_values = validate_attachments(attachments)
        if self.attachment_store:
            for item in attachment_values:
                self.attachment_store.verify(item)
        task = {
            'task_id': uuid.uuid4().hex,
            'url': url,
            'public_url': public_url(url),
            'description': description.strip(),
            'attachments': attachment_values,
            'adapter': adapter.name,
            'adapter_version': adapter.version,
            'transaction_name': adapter.transaction_name,
            'status': 'new',
            'fields': [],
            'facts': [],
            'decisions': [],
            'blockers': [],
            'field_version': 0,
            'created_at': now(),
        }
        self.save(task)
        return self.get(task['task_id'])

    @staticmethod
    def _validate_plan(plan):
        require(isinstance(plan, dict) and isinstance(plan.get('fields'), list) and
                len(plan['fields']) <= 50 and isinstance(plan.get('blockers'), list),
                'ADAPTER_PROTOCOL', '适配器字段模型无效。')
        ids = set()
        for field in plan['fields']:
            require(isinstance(field, dict) and isinstance(field.get('id'), str) and
                    re.fullmatch(r'[a-z][a-z0-9_]{0,63}', field['id']) is not None and
                    field['id'] not in ids and field.get('source') in ('personal', 'decision', 'page') and
                    type(field.get('required')) is bool and isinstance(field.get('label'), str),
                    'ADAPTER_PROTOCOL', '适配器字段定义无效。')
            ids.add(field['id'])
            if field['source'] == 'personal':
                require(isinstance(field.get('query'), str) and field['query'].strip(),
                        'ADAPTER_PROTOCOL', '个人资料字段缺少独立查询。')
            if field['source'] == 'decision':
                require(isinstance(field.get('question'), str) and field['question'].strip() and
                        field.get('input') in ('choice', 'text'),
                        'ADAPTER_PROTOCOL', '任务决定字段无效。')
                if field['input'] == 'choice':
                    options = field.get('options')
                    require(isinstance(options, list) and len(options) <= 200 and all(
                        isinstance(option, dict) and isinstance(option.get('value'), str) and
                        isinstance(option.get('label'), str) for option in options),
                        'ADAPTER_PROTOCOL', '选项字段无效。')
            if field['source'] == 'page':
                require('value' in field, 'ADAPTER_PROTOCOL', '页面字段缺少回读值。')
        require(isinstance(plan.get('page_structure'), str) and plan['page_structure'],
                'ADAPTER_PROTOCOL', '适配器缺少页面结构摘要。')

    def inspect(self, task_id, snapshot):
        """Record an already-read structured snapshot; performs no navigation."""
        with self.assistant.task_lock('ehall:' + task_id):
            task = self.get(task_id)
            require(task['status'] in ('new', 'inspecting', 'waiting_input', 'ready_to_fill',
                                       'preview_ready', 'failed', 'needs_review'),
                    'STATE_CONFLICT', '当前状态不能重新记录页面结构。')
            adapter = self.adapters.get(task['adapter'])
            require(adapter is not None and adapter.version == task['adapter_version'],
                    'ADAPTER_CHANGED', '任务适配器已变化，需要人工核对。')
            task['status'] = 'inspecting'
            self.save(task)
            try:
                plan = adapter.inspect(snapshot)
                self._validate_plan(plan)
            except AppError as error:
                task.update(status='needs_review', pause_reason={
                    'code': error.code, 'message': str(error)})
                self.save(task)
                raise
            facts, decisions, page_values, children = [], [], {}, []
            for field in plan['fields']:
                if field['source'] == 'personal':
                    child_id = uuid.uuid4().hex
                    facts.append({'field_id': field['id'], 'question': field['query'],
                                  'child_id': child_id, 'status': 'running'})
                    children.append({'task_id': child_id, 'query': field['query'], 'status': 'running',
                                     'model': self.assistant.config['model'], 'created_at': now(),
                                     'trace': [], 'parent_supplements': []})
                elif field['source'] == 'decision':
                    decisions.append({'field_id': field['id'], 'question': field['question'],
                                      'input': field['input'], 'options': field.get('options', []),
                                      'required': field['required'], 'answer': None})
                else:
                    page_values[field['id']] = field['value']
            task.update(transaction_name=plan.get('transaction_name', task['transaction_name']),
                        page_identity=plan.get('page_identity'), page_summary=plan.get('page_summary', {}),
                        page_structure=plan['page_structure'], fields=plan['fields'], facts=facts,
                        decisions=decisions, page_values=page_values, blockers=plan['blockers'],
                        status='preparing', field_version=task['field_version'] + 1,
                        inspected_at=now())
            task.pop('error', None)
            task.pop('pause_reason', None)
            task.pop('user_values', None)
            task.pop('prepared_values', None)
            task.pop('fill_readback', None)
            with sqlite3.connect(str(self.db_path)) as db:
                db.execute('BEGIN IMMEDIATE')
                for child in children:
                    db.execute('INSERT INTO tasks VALUES (?,?)',
                               (child['task_id'], json.dumps(child, ensure_ascii=False)))
                db.execute('UPDATE ehall_tasks SET adapter=?,payload=? WHERE id=?',
                           (task['adapter'], json.dumps(task, ensure_ascii=False), task_id))
        return self.resume(task_id)

    def _dependencies(self, task, children):
        return digest({'adapter': task['adapter'], 'adapter_version': task['adapter_version'],
                       'page_structure': task.get('page_structure'), 'attachments': task['attachments'],
                       'children': children, 'decisions': task['decisions']})

    def resume(self, task_id, retry=True):
        with self.assistant.task_lock('ehall:' + task_id):
            task = self.get(task_id)
            require(task['status'] != 'new', 'STATE_CONFLICT', '请先完成页面只读检查。')
            task['status'] = 'preparing'
            task.pop('error', None)
            self.save(task)
            children = []
            try:
                for fact in task['facts']:
                    child = self.assistant.get_task(fact['child_id'])
                    if child['status'] == 'running':
                        child = self.assistant._run(child)
                    elif child.get('freshness', {}).get('status') == 'stale' or (
                            retry and child['status'] == 'failed'):
                        child = self.assistant.resume(child['task_id'])
                    fact.update(status=child['status'], request_id=child.get('request_id'),
                                result=child.get('result'), error=child.get('error'))
                    children.append(child)
                task['issues'] = [{'child_id': child['task_id'], 'status': child['status'],
                                   'missing': child.get('result', {}).get('missing', []),
                                   'conflicts': child.get('result', {}).get('conflicts', []),
                                   'error': child.get('error')}
                                  for child in children if child['status'] != 'completed']
                if task['blockers'] or any(child['status'] == 'needs_review' for child in children):
                    task['status'] = 'needs_review'
                elif any(child['status'] == 'failed' for child in children):
                    task['status'] = 'failed'
                    task['error'] = {'code': 'CHILD_FAILED',
                                     'message': '个人资料子任务失败，未继续准备。'}
                elif any(child['status'] != 'completed' for child in children) or any(
                        decision['required'] and decision['answer'] is None
                        for decision in task['decisions']):
                    task['status'] = 'waiting_input'
                else:
                    values = dict(task.get('page_values', {}))
                    for fact, child in zip(task['facts'], children):
                        answers = child['result']['answers']
                        values[fact['field_id']] = answers[0]['text'] if len(answers) == 1 else [
                            answer['text'] for answer in answers]
                    values.update({decision['field_id']: decision['answer']
                                   for decision in task['decisions'] if decision['answer'] is not None})
                    values.update(task.get('user_values', {}))
                    task['prepared_values'] = values
                    task['preparation_fingerprint'] = self._dependencies(task, children)
                    task['status'] = 'ready_to_fill'
                self.save(task)
            except AppError as error:
                task.update(status='failed', error={'code': error.code, 'message': str(error)})
                self.save(task)
            return self.get(task_id)

    def edit(self, task_id, version, values, attachments=None):
        require(type(version) is int and isinstance(values, dict), 'INPUT_ERROR',
                '字段版本或编辑内容无效。')
        with self.assistant.task_lock('ehall:' + task_id):
            task = self.get(task_id)
            require(task['status'] in ('ready_to_fill', 'preview_ready', 'confirmed'), 'STATE_CONFLICT',
                    '字段尚未准备完成，不能编辑。')
            require(task['field_version'] == version, 'VERSION_CONFLICT',
                    '表单已有新版本，未覆盖；请读取最新字段。')
            fields = {field['id']: field for field in task['fields'] if field['source'] != 'page'}
            require(set(values) <= set(fields), 'INPUT_ERROR', '编辑包含未知或只读字段。')
            adapter = self.adapters[task['adapter']]
            normalized = dict(task.get('user_values', {}))
            for field_id, answer in values.items():
                field = fields[field_id]
                if field.get('input') == 'choice' and hasattr(adapter, 'normalize_decision'):
                    normalized[field_id] = adapter.normalize_decision(field, answer)
                else:
                    require(isinstance(answer, str) and answer.strip() and len(answer) <= 4000,
                            'INPUT_ERROR', '可编辑字段须为 1—4000 字符。')
                    normalized[field_id] = answer.strip()
            next_attachments = task['attachments'] if attachments is None else validate_attachments(attachments)
            if self.attachment_store:
                for item in next_attachments:
                    self.attachment_store.verify(item)
            task.update(user_values=normalized, attachments=next_attachments,
                        field_version=task['field_version'] + 1, status='preparing')
            task.pop('prepared_values', None)
            task.pop('preparation_fingerprint', None)
            task.pop('fill_readback', None)
            task.pop('confirmed_preview_id', None)
            task.pop('confirmed_fingerprint', None)
            self.save(task)
        return self.resume(task_id)

    def record_readback(self, task_id, version, page_structure, values):
        """Publish a non-submitting browser trial only if it still matches the task."""
        with self.assistant.task_lock('ehall:' + task_id):
            task = self.get(task_id)
            require(task['status'] == 'ready_to_fill' and task['field_version'] == version,
                    'VERSION_CONFLICT', '浏览器试填期间字段已变化。')
            require(task.get('page_structure') == page_structure, 'PAGE_CHANGED',
                    '页面结构已变化，未发布试填结果。')
            require(isinstance(values, dict) and values == task.get('prepared_values'),
                    'READBACK_MISMATCH', '页面回读值与待填字段不一致。')
            task['fill_readback'] = {'values': values, 'field_version': version,
                                     'page_structure': page_structure, 'read_at': now(),
                                     'submitted': False}
            task['status'] = 'preview_ready'
            self.save(task)
            return self.get(task_id)

    def decide(self, task_id, field_id, answer):
        with self.assistant.task_lock('ehall:' + task_id):
            task = self.get(task_id)
            decision = next((item for item in task['decisions'] if item['field_id'] == field_id), None)
            require(decision is not None, 'DECISION_NOT_FOUND', '当前任务没有该决定字段。')
            field = next(field for field in task['fields'] if field['id'] == field_id)
            adapter = self.adapters[task['adapter']]
            if hasattr(adapter, 'normalize_decision'):
                value = adapter.normalize_decision(field, answer)
            elif decision['input'] == 'choice':
                values = {option['value'] for option in decision['options']}
                require(isinstance(answer, str) and answer in values, 'INPUT_ERROR', '请选择有效选项。')
                value = answer
            else:
                require(isinstance(answer, str) and answer.strip() and len(answer) <= 4000,
                        'INPUT_ERROR', '请提供有效的当前任务决定。')
                value = answer.strip()
            decision['answer'] = value
            decision['answered_at'] = now()
            task['field_version'] += 1
            task.pop('prepared_values', None)
            task.pop('preparation_fingerprint', None)
            self.save(task)
        return self.resume(task_id)

    def answer(self, task_id, request_id, text, scope, confirm_conflict=False):
        task = self.get(task_id)
        fact = next((item for item in task['facts'] if item.get('request_id') == request_id), None)
        require(fact is not None, 'REQUEST_NOT_OWNED', '补充请求不属于该 ehall 任务。')
        child = self.assistant.answer_request(request_id, text, scope, confirm_conflict)
        if child.get('reply_feedback'):
            task['reply_feedback'] = child['reply_feedback']
            self.save(task)
            return self.get(task_id)
        return self.resume(task_id)

    def visible_requests(self, task_id):
        task = self.get(task_id)
        result = []
        for fact in task['facts']:
            if fact.get('request_id') and fact.get('status') == 'waiting_input':
                request = self.assistant.get_request(fact['request_id'])
                if request['status'] == 'pending':
                    result.append(request)
        return result

    def pause(self, task_id, code, message):
        require(isinstance(code, str) and isinstance(message, str), 'INPUT_ERROR',
                '暂停信息无效。')
        with self.assistant.task_lock('ehall:' + task_id):
            task = self.get(task_id)
            task.update(status='needs_review', pause_reason={'code': code, 'message': message})
            self.save(task)
            return self.get(task_id)
