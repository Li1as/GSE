"""Stage 4D frozen previews and conservative external-action records."""
import hashlib
import json
import sqlite3
import uuid

from ehall_tasks import now
from persistence import AppError


def require(ok, code, message):
    if not ok:
        raise AppError(code, message)


def fingerprint(value):
    return hashlib.sha256(json.dumps(
        value, ensure_ascii=False, sort_keys=True,
        separators=(',', ':')).encode()).hexdigest()


class EhallSubmitService:
    def __init__(self, tasks):
        self.tasks = tasks
        self.db_path = tasks.db_path
        with sqlite3.connect(str(self.db_path)) as db:
            db.execute('''CREATE TABLE IF NOT EXISTS ehall_previews (
                id TEXT PRIMARY KEY, task_id TEXT NOT NULL, version INTEGER NOT NULL,
                fingerprint TEXT NOT NULL, payload TEXT NOT NULL)''')
            db.execute('CREATE INDEX IF NOT EXISTS ehall_previews_task ON ehall_previews(task_id)')
            db.execute('''CREATE TABLE IF NOT EXISTS ehall_submissions (
                id TEXT PRIMARY KEY, task_id TEXT NOT NULL, preview_id TEXT NOT NULL,
                status TEXT NOT NULL, payload TEXT NOT NULL)''')
            db.execute('CREATE INDEX IF NOT EXISTS ehall_submissions_task ON ehall_submissions(task_id)')

    def _save_preview(self, preview):
        with sqlite3.connect(str(self.db_path)) as db:
            db.execute('INSERT OR REPLACE INTO ehall_previews VALUES (?,?,?,?,?)',
                       (preview['id'], preview['task_id'], preview['field_version'],
                        preview['fingerprint'], json.dumps(preview, ensure_ascii=False)))

    def _save_submission(self, record):
        with sqlite3.connect(str(self.db_path)) as db:
            db.execute('INSERT OR REPLACE INTO ehall_submissions VALUES (?,?,?,?,?)',
                       (record['id'], record['task_id'], record['preview_id'],
                        record['status'], json.dumps(record, ensure_ascii=False)))

    def get_preview(self, task_id, preview_id):
        with sqlite3.connect(str(self.db_path)) as db:
            row = db.execute('SELECT payload FROM ehall_previews WHERE id=? AND task_id=?',
                             (preview_id, task_id)).fetchone()
        require(row is not None, 'PREVIEW_NOT_FOUND', '冻结预览不属于该任务。')
        return json.loads(row[0])

    def previews(self, task_id):
        with sqlite3.connect(str(self.db_path)) as db:
            return [json.loads(row[0]) for row in db.execute(
                'SELECT payload FROM ehall_previews WHERE task_id=? ORDER BY rowid', (task_id,))]

    def get_submission(self, task_id, submission_id):
        with sqlite3.connect(str(self.db_path)) as db:
            row = db.execute('SELECT payload FROM ehall_submissions WHERE id=? AND task_id=?',
                             (submission_id, task_id)).fetchone()
        require(row is not None, 'SUBMISSION_NOT_FOUND', '执行记录不属于该任务。')
        return json.loads(row[0])

    def submissions(self, task_id):
        with sqlite3.connect(str(self.db_path)) as db:
            return [json.loads(row[0]) for row in db.execute(
                'SELECT payload FROM ehall_submissions WHERE task_id=? ORDER BY rowid', (task_id,))]

    def _current(self, task, preview):
        content = preview['content']
        adapter = self.tasks.adapters.get(task['adapter'])
        return (adapter is not None and adapter.version == content['adapter_version'] and
                task['adapter'] == content['adapter'] and
                task['adapter_version'] == content['adapter_version'] and
                task['field_version'] == content['field_version'] and
                task.get('page_structure') == content['page_structure'] and
                task.get('preparation_fingerprint') == content['preparation_fingerprint'] and
                task.get('prepared_values') == content['values'] and
                task.get('attachments') == content['attachments'] and
                task.get('fill_readback', {}).get('submitted') is False and
                task.get('fill_readback', {}).get('field_version') == task['field_version'])

    def preview(self, task_id, version):
        with self.tasks.assistant.task_lock('ehall:' + task_id):
            task = self.tasks.get(task_id)
            require(task['status'] == 'preview_ready' and type(version) is int and
                    version == task['field_version'], 'VERSION_CONFLICT',
                    '只能冻结当前已回读的字段版本。')
            if self.tasks.attachment_store:
                for item in task['attachments']:
                    self.tasks.attachment_store.verify(item)
            adapter = self.tasks.adapters[task['adapter']]
            require(adapter.version == task['adapter_version'], 'ADAPTER_CHANGED',
                    '事务适配器已更新，请重新读取和试填。')
            consequences = adapter.consequences(task['prepared_values'])
            require(isinstance(consequences, dict) and consequences.get('action') and
                    isinstance(consequences.get('warnings'), list), 'ADAPTER_PROTOCOL',
                    '适配器未提供有效的操作后果说明。')
            fields = []
            for field in task['fields']:
                value = task['prepared_values'].get(field['id'])
                display = value
                if field.get('input') == 'choice':
                    selected = next((option for option in field.get('options', [])
                                     if option['value'] == value), None)
                    require(selected is not None, 'PREVIEW_INVALID', '当前选项已不存在。')
                    display = selected['label']
                fields.append({'id': field['id'], 'label': field['label'],
                               'value': value, 'display': display,
                               'source': field['source']})
            sources = [{'field_id': fact['field_id'], 'child_id': fact['child_id'],
                        'evidence': [item['id'] for item in fact.get('result', {}).get('evidence', [])]}
                       for fact in task['facts']]
            content = {
                'transaction_name': task['transaction_name'],
                'entry': task['public_url'],
                'adapter': task['adapter'],
                'adapter_version': task['adapter_version'],
                'field_version': task['field_version'],
                'page_structure': task['page_structure'],
                'preparation_fingerprint': task['preparation_fingerprint'],
                'values': task['prepared_values'],
                'fields': fields,
                'attachments': task['attachments'],
                'sources': sources,
                'decisions': task['decisions'],
                'consequences': consequences,
            }
            preview = {'id': uuid.uuid4().hex, 'task_id': task_id, 'status': 'preview',
                       'field_version': version, 'content': content, 'created_at': now()}
            preview['fingerprint'] = fingerprint(content)
            self._save_preview(preview)
            task['current_preview_id'] = preview['id']
            self.tasks.save(task)
            return preview

    def confirm(self, task_id, preview_id, expected_fingerprint, confirmed):
        require(confirmed is True, 'CONFIRMATION_REQUIRED', '请明确确认冻结预览。')
        with self.tasks.assistant.task_lock('ehall:' + task_id):
            task = self.tasks.get(task_id)
            preview = self.get_preview(task_id, preview_id)
            require(preview['status'] in ('preview', 'confirmed') and
                    preview['fingerprint'] == expected_fingerprint and
                    fingerprint(preview['content']) == preview['fingerprint'] and
                    self._current(task, preview), 'PREVIEW_STALE',
                    '冻结预览已失效，请重新试填并核对。')
            if preview['status'] == 'confirmed':
                return preview
            preview.update(status='confirmed', confirmed_at=now())
            self._save_preview(preview)
            task.update(status='confirmed', confirmed_preview_id=preview_id,
                        confirmed_fingerprint=preview['fingerprint'])
            self.tasks.save(task)
            return preview

    def _begin(self, task_id, preview_id, mode):
        task = self.tasks.get(task_id)
        preview = self.get_preview(task_id, preview_id)
        require(task['status'] == 'confirmed' and preview['status'] == 'confirmed' and
                task.get('confirmed_preview_id') == preview_id and self._current(task, preview),
                'CONFIRMATION_REQUIRED', '当前字段版本未经确认。')
        with sqlite3.connect(str(self.db_path)) as db:
            active = db.execute(
                "SELECT 1 FROM ehall_submissions WHERE task_id=? AND status IN "
                "('submitting','unknown','succeeded')", (task_id,)).fetchone()
        require(active is None, 'SUBMISSION_BLOCKED', '已有执行中、结果不明或成功记录，禁止再次执行。')
        record = {'id': uuid.uuid4().hex, 'task_id': task_id, 'preview_id': preview_id,
                  'fingerprint': preview['fingerprint'], 'mode': mode,
                  'status': 'submitting', 'stage': 'before_open',
                  'created_at': now(), 'click_count': 0}
        self._save_submission(record)
        preview.update(status='consumed', consumed_at=now(), submission_id=record['id'])
        self._save_preview(preview)
        task['status'] = 'submitting'
        self.tasks.save(task)
        return task, preview, record

    def execute(self, task_id, preview_id, operation):
        """Execute an adapter operation once after exact-version confirmation."""
        with self.tasks.assistant.task_lock('ehall:' + task_id):
            task, preview, record = self._begin(task_id, preview_id, 'automatic')
        try:
            observed = operation.prepare(preview['content'])
            require(observed == preview['fingerprint'], 'PREVIEW_STALE',
                    '提交前重新回读与确认版本不一致。')
            open_confirmation = getattr(operation, 'open_confirmation', None)
            if open_confirmation:
                confirmation = open_confirmation()
                require(isinstance(confirmation, dict) and confirmation.get('prompt'),
                        'ADAPTER_PROTOCOL', '站点确认框核对结果无效。')
            with self.tasks.assistant.task_lock('ehall:' + task_id):
                record = self.get_submission(task_id, record['id'])
                require(record['status'] == 'submitting' and record['stage'] == 'before_open',
                        'SUBMISSION_BLOCKED', '执行记录已变化。')
                record.update(stage='before_click', before_click_at=now(), click_count=1)
                self._save_submission(record)
            result = operation.submit_once()
            require(isinstance(result, dict) and result.get('status') in (
                'succeeded', 'failed', 'unknown'), 'ADAPTER_PROTOCOL', '提交结果无效。')
            require(result['status'] != 'succeeded' or isinstance(result.get('evidence'), dict),
                    'ADAPTER_PROTOCOL', '成功结果缺少可核对证据。')
            with self.tasks.assistant.task_lock('ehall:' + task_id):
                record = self.get_submission(task_id, record['id'])
                record.update(result, stage='after_click', finished_at=now())
                self._save_submission(record)
                task = self.tasks.get(task_id)
                task['status'] = record['status']
                self.tasks.save(task)
            return record
        except AppError as error:
            with self.tasks.assistant.task_lock('ehall:' + task_id):
                record = self.get_submission(task_id, record['id'])
                ambiguous = record['stage'] != 'before_open'
                record.update(status='unknown' if ambiguous else 'failed', finished_at=now(),
                              error={'code': error.code, 'message': str(error)})
                self._save_submission(record)
                task = self.tasks.get(task_id)
                task['status'] = record['status']
                self.tasks.save(task)
            return record
        except Exception as error:
            with self.tasks.assistant.task_lock('ehall:' + task_id):
                record = self.get_submission(task_id, record['id'])
                ambiguous = record['stage'] != 'before_open'
                record.update(status='unknown' if ambiguous else 'failed', finished_at=now(),
                              error={'code': 'EXTERNAL_ERROR',
                                     'message': '外部操作中断：' + type(error).__name__})
                self._save_submission(record)
                task = self.tasks.get(task_id)
                task['status'] = record['status']
                self.tasks.save(task)
            return record
        finally:
            close = getattr(operation, 'close', None)
            if close:
                try:
                    close()
                except Exception:
                    pass

    def reconcile(self, task_id, submission_id, checker):
        with self.tasks.assistant.task_lock('ehall:' + task_id):
            record = self.get_submission(task_id, submission_id)
            require(record['status'] == 'unknown', 'STATE_CONFLICT', '只核对结果不明的执行记录。')
        result = checker(record, self.get_preview(task_id, record['preview_id']))
        require(isinstance(result, dict) and result.get('status') in ('succeeded', 'unknown'),
                'ADAPTER_PROTOCOL', '核对结果无效。')
        require(result['status'] != 'succeeded' or isinstance(result.get('evidence'), dict),
                'ADAPTER_PROTOCOL', '核对成功缺少可验证证据。')
        with self.tasks.assistant.task_lock('ehall:' + task_id):
            record = self.get_submission(task_id, submission_id)
            record.update(result, reconciled_at=now())
            self._save_submission(record)
            task = self.tasks.get(task_id)
            task['status'] = record['status']
            self.tasks.save(task)
            return record

    def recover(self):
        recovered = []
        with sqlite3.connect(str(self.db_path)) as db:
            rows = [json.loads(row[0]) for row in db.execute(
                "SELECT payload FROM ehall_submissions WHERE status='submitting'")]
        for record in rows:
            with self.tasks.assistant.task_lock('ehall:' + record['task_id']):
                latest = self.get_submission(record['task_id'], record['id'])
                if latest['status'] != 'submitting':
                    continue
                latest['status'] = 'failed' if latest['stage'] == 'before_open' else 'unknown'
                latest['recovery'] = '重启发现未完成记录；可能已点击时禁止自动重试。'
                latest['recovered_at'] = now()
                self._save_submission(latest)
                task = self.tasks.get(latest['task_id'])
                task['status'] = latest['status']
                self.tasks.save(task)
                recovered.append(latest)
        return recovered
