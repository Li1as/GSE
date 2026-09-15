"""Stage 4C worker for explicitly queued ehall inspection and safe readback."""
import argparse
import json
import sqlite3
import time
from pathlib import Path

from assistant import Assistant, ROOT, load_config
from ehall_browser import (EhallProbe, load_ehall_config, navigation_host,
                           normalize_url, restore_storage_state, save_storage_state)
from ehall_tasks import EhallAttachmentStore, EhallTasks, now
from ehall_submit import EhallSubmitService, fingerprint
from persistence import AppError


class RealBrowserBackend:
    """One bounded browser visit. No adapter submit capability is exposed."""

    def __init__(self, config, data_dir):
        self.config = config
        self.data_dir = Path(data_dir)

    def visit(self, task, adapter, prepare=False):
        try:
            from playwright.sync_api import Error as PlaywrightError, sync_playwright
        except ImportError:
            raise AppError('BROWSER_UNAVAILABLE', '未安装 Playwright。') from None
        target = normalize_url(task['url'], self.config['task_hosts'])
        timeout = self.config['timeout_seconds'] * 1000
        profile = self.data_dir / 'browser-profile'
        profile.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            with sync_playwright() as playwright:
                context = playwright.chromium.launch_persistent_context(
                    str(profile), headless=self.config['headless'], accept_downloads=False,
                    viewport={'width': 1440, 'height': 1000})
                try:
                    restore_storage_state(context, self.data_dir / 'storage-state.json')
                    page = context.pages[0] if context.pages else context.new_page()
                    page.set_default_timeout(timeout)
                    blocked = []

                    def guard(route):
                        request = route.request
                        if request.is_navigation_request() and request.frame == page.main_frame:
                            try:
                                navigation_host(request.url, self.config['allowed_navigation_hosts'])
                            except AppError:
                                blocked.append(True)
                                route.abort()
                                return
                        route.continue_()

                    page.route('**/*', guard)
                    page.goto(target, wait_until='domcontentloaded', timeout=timeout)
                    if blocked:
                        raise AppError('NAVIGATION_BLOCKED', '浏览器跳转到未允许的地址。')
                    EhallProbe(self.config, self.data_dir)._login(page, target)
                    page.wait_for_timeout(3000)
                    navigation_host(page.url, self.config['allowed_navigation_hosts'])
                    snapshot = adapter.inspect_page(page)
                    save_storage_state(context, self.data_dir / 'storage-state.json')
                    if not prepare:
                        return {'snapshot': snapshot}
                    plan = adapter.inspect(snapshot)
                    if plan['page_structure'] != task.get('page_structure'):
                        raise AppError('PAGE_CHANGED', '真实页面结构与准备版本不一致。')
                    # Attachments remain task-local because this adapter has no upload field.
                    binding = adapter.fill(page, task['prepared_values'], [])
                    return {'page_structure': plan['page_structure'],
                            'values': adapter.read_back(page, binding)}
                finally:
                    context.close()
        except AppError:
            raise
        except PlaywrightError as error:
            raise AppError('BROWSER_ERROR', '浏览器准备失败：' + type(error).__name__) from None

    def reconcile(self, task, adapter, preview):
        """Read the current course list; this path has no click capability."""
        result = self.visit(task, adapter, prepare=False)
        snapshot = result['snapshot']
        target = preview['content']['values'].get('target_course')
        present = any(item.get('course_id') == target and item.get('withdrawal_available')
                      for item in snapshot.get('courses', []))
        # This page only exposes currently withdrawable controls. Absence is
        # not proof of withdrawal: the deadline or page layout may have changed.
        return {'status': 'unknown', 'evidence': {
            'kind': 'withdrawal_control_observation', 'observed_at': now(),
            'selected_control_present': present,
            'page_identity': snapshot.get('page_identity'),
            'snapshot_fingerprint': fingerprint(snapshot),
            'meaning': '仅读观察不足以证明退课成功。'}}

    def operation(self, task, adapter):
        return RealSubmitOperation(self, task, adapter)


class RealSubmitOperation:
    """Own one browser session from final revalidation through one submit."""

    def __init__(self, backend, task, adapter):
        self.backend, self.task, self.adapter = backend, task, adapter
        self.playwright = self.context = self.page = self.binding = None

    def prepare(self, content):
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            raise AppError('BROWSER_UNAVAILABLE', '未安装 Playwright。') from None
        config, data_dir = self.backend.config, self.backend.data_dir
        target = normalize_url(self.task['url'], config['task_hosts'])
        self.playwright = sync_playwright().start()
        profile = data_dir / 'browser-profile'
        profile.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.context = self.playwright.chromium.launch_persistent_context(
            str(profile), headless=config['headless'], accept_downloads=False,
            viewport={'width': 1440, 'height': 1000})
        restore_storage_state(self.context, data_dir / 'storage-state.json')
        self.page = self.context.pages[0] if self.context.pages else self.context.new_page()
        self.page.set_default_timeout(config['timeout_seconds'] * 1000)
        blocked = []

        def guard(route):
            request = route.request
            if request.is_navigation_request() and request.frame == self.page.main_frame:
                try:
                    navigation_host(request.url, config['allowed_navigation_hosts'])
                except AppError:
                    blocked.append(True)
                    route.abort()
                    return
            route.continue_()

        self.page.route('**/*', guard)
        self.page.goto(target, wait_until='domcontentloaded')
        if blocked:
            raise AppError('NAVIGATION_BLOCKED', '浏览器跳转到未允许的地址。')
        EhallProbe(config, data_dir)._login(self.page, target)
        self.page.wait_for_timeout(3000)
        navigation_host(self.page.url, config['allowed_navigation_hosts'])
        self.adapter.dismiss_known_dialogs(self.page)
        snapshot = self.adapter.inspect_page(self.page)
        plan = self.adapter.inspect(snapshot)
        if (self.adapter.version != content['adapter_version'] or
                plan['page_structure'] != content['page_structure']):
            raise AppError('PAGE_CHANGED', '提交前页面或适配器版本已变化。')
        self.binding = self.adapter.fill(self.page, content['values'], [])
        if self.adapter.read_back(self.page, self.binding) != content['values']:
            raise AppError('READBACK_MISMATCH', '提交前回读与冻结预览不一致。')
        save_storage_state(self.context, data_dir / 'storage-state.json')
        return fingerprint(content)

    def open_confirmation(self):
        try:
            self.confirmation = self.adapter.open_confirmation(self.page, self.binding)
            return {'prompt': self.confirmation['prompt']}
        except AppError:
            raise
        except Exception as error:
            raise AppError('CONFIRMATION_UI_ERROR',
                           '退课确认框未能安全打开：' + type(error).__name__) from None

    def submit_once(self):
        return self.adapter.submit(self.page, self.confirmation)

    def close(self):
        try:
            if self.context:
                self.context.close()
        finally:
            if self.playwright:
                self.playwright.stop()


class EhallWorker:
    def __init__(self, tasks, backend=None):
        self.tasks = tasks
        self.backend = backend
        self.db_path = tasks.db_path
        self.submit = EhallSubmitService(tasks)
        with sqlite3.connect(str(self.db_path)) as db:
            db.execute('''CREATE TABLE IF NOT EXISTS ehall_jobs (
                task_id TEXT PRIMARY KEY, status TEXT NOT NULL,
                created_at TEXT NOT NULL, payload TEXT NOT NULL)''')

    def recover(self):
        """Recover jobs only in the dedicated worker process."""
        recovered_submissions = self.submit.recover()
        with sqlite3.connect(str(self.db_path)) as db:
            rows = db.execute("SELECT task_id,payload FROM ehall_jobs WHERE status='running'").fetchall()
            for task_id, raw in rows:
                payload = json.loads(raw)
                status = 'queued'
                if payload.get('kind') == 'submit':
                    records = [item for item in self.submit.submissions(task_id)
                               if item['preview_id'] == payload.get('preview_id')]
                    if records:
                        payload.update(submission_id=records[-1]['id'],
                                       task_status=records[-1]['status'], recovered_at=now())
                        status = ('done' if records[-1]['status'] == 'failed'
                                  else 'waiting_input')
                db.execute('UPDATE ehall_jobs SET status=?,payload=? WHERE task_id=?',
                           (status, json.dumps(payload, ensure_ascii=False), task_id))
        return recovered_submissions

    def enqueue(self, task_id):
        self.tasks.get(task_id)
        with sqlite3.connect(str(self.db_path)) as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT status FROM ehall_jobs WHERE task_id=?', (task_id,)).fetchone()
            if row and row[0] in ('queued', 'running'):
                return {'task_id': task_id, 'status': row[0]}
            payload = {'task_id': task_id, 'queued_at': now()}
            db.execute('INSERT OR REPLACE INTO ehall_jobs VALUES (?,?,?,?)',
                       (task_id, 'queued', now(), json.dumps(payload, ensure_ascii=False)))
        return {'task_id': task_id, 'status': 'queued'}

    def enqueue_reconcile(self, task_id, submission_id):
        record = self.submit.get_submission(task_id, submission_id)
        if record['status'] != 'unknown':
            raise AppError('STATE_CONFLICT', '只能排队核对结果不明的执行记录。')
        with sqlite3.connect(str(self.db_path)) as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT status FROM ehall_jobs WHERE task_id=?', (task_id,)).fetchone()
            if row and row[0] in ('queued', 'running'):
                return {'task_id': task_id, 'submission_id': submission_id, 'status': row[0]}
            payload = {'task_id': task_id, 'submission_id': submission_id,
                       'kind': 'reconcile', 'queued_at': now()}
            db.execute('INSERT OR REPLACE INTO ehall_jobs VALUES (?,?,?,?)',
                       (task_id, 'queued', now(), json.dumps(payload, ensure_ascii=False)))
        return dict(payload, status='queued')

    def enqueue_submit(self, task_id, preview_id):
        task = self.tasks.get(task_id)
        preview = self.submit.get_preview(task_id, preview_id)
        if (task['status'] != 'confirmed' or preview['status'] != 'confirmed' or
                task.get('confirmed_preview_id') != preview_id):
            raise AppError('CONFIRMATION_REQUIRED', '当前冻结版本未确认。')
        with sqlite3.connect(str(self.db_path)) as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT status,payload FROM ehall_jobs WHERE task_id=?',
                             (task_id,)).fetchone()
            if row and row[0] in ('queued', 'running'):
                current = json.loads(row[1])
                if current.get('kind') != 'submit' or current.get('preview_id') != preview_id:
                    raise AppError('SUBMISSION_BLOCKED', '该任务已有其他浏览器作业。')
                return dict(current, status=row[0])
            payload = {'task_id': task_id, 'preview_id': preview_id,
                       'kind': 'submit', 'queued_at': now()}
            db.execute('INSERT OR REPLACE INTO ehall_jobs VALUES (?,?,?,?)',
                       (task_id, 'queued', now(), json.dumps(payload, ensure_ascii=False)))
        return dict(payload, status='queued')

    def status(self, task_id):
        with sqlite3.connect(str(self.db_path)) as db:
            row = db.execute('SELECT status,payload FROM ehall_jobs WHERE task_id=?', (task_id,)).fetchone()
        return None if not row else dict(json.loads(row[1]), status=row[0])

    def run_once(self):
        if self.backend is None:
            raise AppError('CONFIG_ERROR', 'ehall 浏览器 worker 未配置。')
        with sqlite3.connect(str(self.db_path)) as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute("SELECT task_id,payload FROM ehall_jobs WHERE status='queued' "
                             'ORDER BY created_at,rowid LIMIT 1').fetchone()
            if not row:
                return None
            task_id, payload = row
            queued = json.loads(payload)
            changed = db.execute("UPDATE ehall_jobs SET status='running' "
                                 "WHERE task_id=? AND status='queued'", (task_id,)).rowcount
            if changed != 1:
                return None
        try:
            task = self.tasks.get(task_id)
            adapter = self.tasks.adapters[task['adapter']]
            if queued.get('kind') == 'submit':
                preview_id = queued['preview_id']
                record = self.submit.execute(
                    task_id, preview_id, self.backend.operation(task, adapter))
                payload = {'task_id': task_id, 'preview_id': preview_id,
                           'submission_id': record['id'], 'finished_at': now(),
                           'task_status': record['status'], 'kind': 'submit'}
                status = 'done' if record['status'] in ('succeeded', 'failed') else 'waiting_input'
                with sqlite3.connect(str(self.db_path)) as db:
                    db.execute('UPDATE ehall_jobs SET status=?,payload=? WHERE task_id=?',
                               (status, json.dumps(payload, ensure_ascii=False), task_id))
                return dict(payload, status=status)
            if queued.get('kind') == 'reconcile':
                submission_id = queued['submission_id']
                record = self.submit.reconcile(
                    task_id, submission_id,
                    lambda submission, preview: self.backend.reconcile(task, adapter, preview))
                payload = {'task_id': task_id, 'submission_id': submission_id,
                           'finished_at': now(), 'task_status': record['status'],
                           'kind': 'reconcile'}
                status = 'done' if record['status'] == 'succeeded' else 'waiting_input'
                with sqlite3.connect(str(self.db_path)) as db:
                    db.execute('UPDATE ehall_jobs SET status=?,payload=? WHERE task_id=?',
                               (status, json.dumps(payload, ensure_ascii=False), task_id))
                return dict(payload, status=status)
            if task['status'] in ('new', 'needs_review'):
                result = self.backend.visit(task, adapter, prepare=False)
                task = self.tasks.inspect(task_id, result['snapshot'])
            if task['status'] == 'ready_to_fill':
                result = self.backend.visit(task, adapter, prepare=True)
                task = self.tasks.record_readback(task_id, task['field_version'],
                                                  result['page_structure'], result['values'])
            payload = {'task_id': task_id, 'finished_at': now(), 'task_status': task['status']}
            status = 'done' if task['status'] == 'preview_ready' else 'waiting_input'
        except AppError as error:
            # A concurrent user edit is valid progress.  Discard this stale
            # readback without overwriting the newer parent state.
            # A failed read-only reconciliation must also preserve the
            # conservative unknown submission state.
            if error.code != 'VERSION_CONFLICT' and queued.get('kind') != 'reconcile':
                self.tasks.pause(task_id, error.code, str(error))
            payload = {'task_id': task_id, 'finished_at': now(),
                       'error': {'code': error.code, 'message': str(error)}}
            status = 'failed'
        with sqlite3.connect(str(self.db_path)) as db:
            db.execute('UPDATE ehall_jobs SET status=?,payload=? WHERE task_id=?',
                       (status, json.dumps(payload, ensure_ascii=False), task_id))
        return dict(payload, status=status)


def build():
    assistant = Assistant(load_config(ROOT / 'config.local.json'))
    files = EhallAttachmentStore(ROOT / 'data' / 'ehall' / 'attachments')
    tasks = EhallTasks(assistant, attachment_store=files)
    backend = RealBrowserBackend(load_ehall_config(ROOT / 'ehall.local.json'),
                                 ROOT / 'data' / 'ehall')
    worker = EhallWorker(tasks, backend)
    worker.recover()
    return worker


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('once', 'run'))
    parser.add_argument('--interval', type=float, default=2.0)
    args = parser.parse_args()
    worker = build()
    if args.command == 'once':
        print(json.dumps(worker.run_once(), ensure_ascii=False))
        return
    if not 0.2 <= args.interval <= 60:
        raise SystemExit('--interval 须为 0.2—60 秒。')
    while True:
        result = worker.run_once()
        if result is None:
            time.sleep(args.interval)


if __name__ == '__main__':
    main()
