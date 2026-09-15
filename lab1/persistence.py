"""Single-host recovery, file version observation and process-scoped execution locks."""
import fcntl
import hashlib
import json
import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path


class AppError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def manifest(directory):
    root = Path(directory)
    if not root.is_dir():
        raise AppError("DATA_ERROR", "个人资料目录不存在。")
    return {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(root.glob('*.md')) if p.name != 'README.md' and not p.is_symlink()}


class RecoveryMixin:
    def init_recovery(self):
        self._lock_local = threading.local()
        with sqlite3.connect(str(self.db_path)) as db:
            db.execute('CREATE TABLE IF NOT EXISTS archives (id TEXT PRIMARY KEY, payload TEXT NOT NULL, status TEXT NOT NULL)')
            # Legacy C receipts predate the outbox. Do not resurrect intentionally removed files.
            for (payload,) in db.execute('SELECT payload FROM tasks').fetchall():
                for item in json.loads(payload).get('supplements', []):
                    if item['scope'] == 'personal':
                        db.execute('INSERT OR IGNORE INTO archives VALUES (?, ?, ?)',
                                   (item['request_id'], json.dumps(item, ensure_ascii=False), 'published'))

    @contextmanager
    def task_lock(self, task_id):
        held = getattr(self._lock_local, 'held', set())
        if task_id in held:
            yield
            return
        lock_dir = self.db_path.parent/'locks'
        lock_dir.mkdir(parents=True, exist_ok=True)
        # Hash identifiers, never interpret a caller's identifier as a path.
        name = hashlib.sha256(task_id.encode()).hexdigest() + '.lock'
        with (lock_dir/name).open('a') as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise AppError('TASK_BUSY', '任务正在其他进程执行，本次未重复调用模型。') from None
            self._lock_local.held = held | {task_id}
            try:
                yield
            finally:
                self._lock_local.held = held
                fcntl.flock(handle, fcntl.LOCK_UN)

    def decorate_task(self, task):
        if 'result' not in task:
            return task
        if 'corpus_versions' not in task:
            task['freshness'] = {'status': 'unknown', 'changed_files': []}
            return task
        try:
            current = manifest(task['data_dir'])
            before = task['corpus_versions']
            changed = sorted(k for k in set(before) | set(current) if before.get(k) != current.get(k))
            task['freshness'] = {'status': 'stale' if changed else 'current', 'changed_files': changed}
        except (OSError, AppError):
            task['freshness'] = {'status': 'unknown', 'changed_files': []}
        return task

    def resume(self, task_id):
        with self.task_lock(task_id):
            task = self.get_task(task_id)
            task['attempts'] = 0  # Explicit user retry resets the bounded retry budget.
            for item in task.get('supplements', []):
                self._materialize(item)
            self.save(task)
            return self._run(task, force=True)

    def recover(self, limit=4, retry_failed=False):
        """One pass. Network work is bounded; no sleeps or automatic background startup."""
        if type(limit) is not int or not 1 <= limit <= 100:
            raise AppError('INPUT_ERROR', 'limit 须为 1—100。')
        with sqlite3.connect(str(self.db_path)) as db:
            ids = [json.loads(r[0])['task_id'] for r in db.execute('SELECT payload FROM tasks').fetchall()]
        report = {'recovered': [], 'repaired': [], 'skipped_busy': [], 'errors': []}
        executed = 0
        for task_id in ids:
            try:
                with self.task_lock(task_id):
                    task = self.get_task(task_id)
                    for item in task.get('supplements', []):
                        if self._materialize(item):
                            report['repaired'].append(item['request_id'])
                    if task.get('status') == 'waiting_input' and task.get('result', {}).get('missing'):
                        self._ensure_request(task)
                    with sqlite3.connect(str(self.db_path)) as db:
                        requests = [json.loads(r[0]) for r in db.execute('SELECT payload FROM requests').fetchall()]
                    for request in requests:
                        if request['task_id'] == task_id:
                            if (request['status'] == 'pending' and task['status'] in ('completed', 'needs_review')
                                    and not task.get('result', {}).get('missing')):
                                request['status'] = 'resolved_by_update'
                                with sqlite3.connect(str(self.db_path)) as db:
                                    db.execute('UPDATE requests SET payload=? WHERE id=?',
                                               (json.dumps(request, ensure_ascii=False), request['request_id']))
                            self._project_request(request)
                    status = task['status']
                    changed = task.get('freshness', {}).get('status') == 'stale'
                    eligible = status == 'running' or (status == 'waiting_input' and changed)
                    if status == 'failed':
                        eligible = (retry_failed or task.get('retryable', False)) and time.time() >= task.get('retry_at', 0)
                    if not eligible or executed >= limit:
                        continue
                    if status == 'waiting_input' and changed:
                        task['attempts'] = 0
                        self.save(task)
                    if task.get('attempts', 0) >= 3:
                        if status != 'failed':
                            task.update(status='failed', retryable=False, error={
                                'code': 'RECOVERY_LIMIT', 'message': '自动恢复已达三次尝试，请检查后显式 resume。'})
                            self.save(task)
                        continue
                    result = self._run(task, force=True)
                    executed += 1
                    report['recovered'].append({'task_id': task_id, 'status': result['status']})
            except AppError as error:
                if error.code == 'TASK_BUSY':
                    report['skipped_busy'].append(task_id)
                else:
                    report['errors'].append({'task_id': task_id, 'code': error.code})
            except OSError:
                report['errors'].append({'task_id': task_id, 'code': 'IO_ERROR'})
        return report

    @staticmethod
    def durable_text(path, text):
        import uuid
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
        try:
            with temp.open('w', encoding='utf-8') as stream:
                stream.write(text)
                stream.flush()
                os.fsync(stream.fileno())
            temp.replace(path)
            fd = os.open(str(path.parent), os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        finally:
            if temp.exists():
                temp.unlink()
