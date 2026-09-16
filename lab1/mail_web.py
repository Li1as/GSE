"""Authenticated single-user LAN UI with explicitly confirmed stage-2D sending."""
import argparse
import hmac
import json
import os
import re
import secrets
import sqlite3
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from assistant import Assistant, AppError, ROOT, load_config
from experience import ExperienceService, ExperienceStore
from mail_tasks import MailTasks
from mail_reader import Reader, MailError, save_snapshot
from mail_send import SendService


class WebApp:
    def __init__(self, tasks, data, token, mail_config=None, scheduler=None,
                 ehall=None, ehall_worker=None, ehall_files=None, ehall_submit=None,
                 experience=None):
        self.tasks, self.data, self.token = tasks, Path(data), token
        self.mail_config = mail_config
        self.scheduler = scheduler
        self.ehall, self.ehall_worker, self.ehall_files = ehall, ehall_worker, ehall_files
        self.ehall_submit = ehall_submit
        self.experience = experience
        self.sender = SendService(tasks, self.data, mail_config)
        self.sender.recover()

    def ehall_qr(self):
        path = self.data / 'ehall' / 'login' / 'qr.png'
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 1024 * 1024:
            raise AppError('LOGIN_NOT_WAITING', '当前没有等待扫码的 ehall 登录。')
        return path.read_bytes()

    def ehall_task(self, task_id):
        task = self.ehall.public(self.ehall.get(task_id))
        task['archived'] = self.ehall.is_archived(task_id)
        task['actions'] = self.ehall.actions(task_id)
        task['requests'] = self.ehall.visible_requests(task_id)
        task['job'] = self.ehall_worker.status(task_id) if self.ehall_worker else None
        task['previews'] = self.ehall_submit.previews(task_id) if self.ehall_submit else []
        task['submissions'] = self.ehall_submit.submissions(task_id) if self.ehall_submit else []
        return task

    def task_summaries(self, archived=False, limit=None):
        with sqlite3.connect(str(self.tasks.db_path)) as db:
            rows = {task_id: json.loads(payload) for task_id, payload in
                    db.execute('SELECT id,payload FROM mail_tasks')}
        archive_rows = self.tasks.archived_tasks(limit or 100) if archived else []
        archive_map = {row['task_id']: row for row in archive_rows}
        selected = ([rows[row['task_id']] for row in archive_rows if row['task_id'] in rows]
                    if archived else [task for key, task in rows.items() if key not in self.tasks.archived_task_ids()])
        if not archived:
            selected.sort(key=lambda task: task['created_at'], reverse=True)
        summaries = [{'task_id': task['task_id'], 'subject': task['mail']['Subject'],
                      'status': task['status'], 'created_at': task['created_at']}
                     for task in selected]
        for summary in summaries:
            records = self.sender.history(summary['task_id'])
            summary['delivery_status'] = records[-1]['status'] if records else None
            if archived:
                summary['archived_at'] = archive_map[summary['task_id']]['archived_at']
        return summaries

    def snapshot_path(self, key):
        if not isinstance(key, str) or not re.fullmatch('[a-f0-9]{64}', key):
            raise AppError('INPUT_ERROR', '邮件快照编号无效。')
        path = self.data / 'mail' / key
        if path.is_symlink() or not (path / 'message.json').is_file():
            raise AppError('INPUT_ERROR', '邮件快照不存在。')
        return path

    def dispatch(self, method, path, data):
        app = self.tasks
        if path.startswith('/api/experience') and self.experience is None:
            raise AppError('CONFIG_ERROR', '经验规则服务未配置。')
        if method == 'GET' and path == '/api/experience/proposals':
            return self.experience.proposals()
        if method == 'GET' and path == '/api/experience/rules':
            return self.experience.store.rules()
        if method == 'POST' and path == '/api/experience/from-classification':
            return self.experience.from_classification(
                self.scheduler, data.get('validity'), data.get('uid'), data.get('guidance'))
        if method == 'POST' and path == '/api/experience/from-draft':
            return self.experience.from_draft(
                self.tasks, data.get('task_id'), data.get('version'), data.get('guidance'))
        match = re.fullmatch('/api/experience/proposals/([a-f0-9]{32})/(approve|reject)', path)
        if match and method == 'POST':
            proposal_id, action = match.groups()
            if action == 'approve':
                if 'candidate' in data:
                    self.experience.store.revise_proposal(proposal_id, data['candidate'])
                return self.experience.store.approve(proposal_id)
            return self.experience.store.reject(proposal_id, data.get('reason'))
        match = re.fullmatch('/api/experience/rules/([a-z][a-z0-9.-]{0,99})/(disable|restore)', path)
        if match and method == 'POST':
            rule_id, action = match.groups()
            if action == 'disable':
                return self.experience.store.disable(rule_id, data.get('reason'))
            return self.experience.store.restore(rule_id, data.get('reason'))
        if path.startswith('/api/ehall') and self.ehall is None:
            raise AppError('CONFIG_ERROR', 'ehall 任务服务未配置。')
        if method == 'GET' and path == '/api/ehall/tasks':
            return self.ehall.summaries()
        if method == 'GET' and path == '/api/ehall/tasks/archived':
            return self.ehall.summaries(archived=True, limit=10)
        if method == 'POST' and path == '/api/ehall/attachments':
            if self.ehall_files is None:
                raise AppError('CONFIG_ERROR', 'ehall 附件存储未配置。')
            return self.ehall_files.upload(data.get('name'), data.get('base64'),
                                           data.get('content_type'))
        if method == 'POST' and path == '/api/ehall/tasks':
            task = self.ehall.create(data.get('url'), data.get('description', ''),
                                     data.get('attachments'))
            if self.ehall_worker:
                self.ehall_worker.enqueue(task['task_id'])
            return self.ehall.public(task)
        if method == 'GET' and path == '/api/ehall/login':
            status = self.data / 'ehall' / 'login' / 'qr.json'
            if status.is_symlink() or not status.is_file():
                return {'status': 'idle'}
            value = json.loads(status.read_text(encoding='utf-8'))
            return {'status': value.get('status'), 'captured_at': value.get('captured_at'),
                    'qr_url': '/api/ehall/login/qr'}
        match = re.fullmatch('/api/ehall/tasks/([a-f0-9]{32})(?:/(answer|decide|edit|prepare|preview|confirm|submit|reconcile|archive|restore))?', path)
        if match:
            task_id, action = match.groups()
            if method == 'GET' and action is None:
                return self.ehall_task(task_id)
            if method == 'POST':
                if self.ehall.is_archived(task_id) and action != 'restore':
                    raise AppError('STATE_CONFLICT', '该 ehall 任务已归档，请先恢复。')
                if action == 'answer':
                    if type(data.get('confirm_conflict', False)) is not bool:
                        raise AppError('INPUT_ERROR', '冲突确认须为布尔值。')
                    result = self.ehall.answer(task_id, data.get('request_id'), data.get('text'),
                                               data.get('scope'), data.get('confirm_conflict', False))
                elif action == 'decide':
                    result = self.ehall.decide(task_id, data.get('field_id'), data.get('answer'))
                elif action == 'edit':
                    result = self.ehall.edit(task_id, data.get('version'), data.get('values'),
                                             data.get('attachments'))
                elif action == 'prepare':
                    result = self.ehall.get(task_id)
                elif action == 'preview':
                    if self.ehall_submit is None:
                        raise AppError('CONFIG_ERROR', 'ehall 确认服务未配置。')
                    return self.ehall_submit.preview(task_id, data.get('version'))
                elif action == 'confirm':
                    if self.ehall_submit is None:
                        raise AppError('CONFIG_ERROR', 'ehall 确认服务未配置。')
                    return self.ehall_submit.confirm(task_id, data.get('preview_id'),
                                                     data.get('fingerprint'), data.get('confirmed'))
                elif action == 'submit':
                    if self.ehall_submit is None or self.ehall_worker is None:
                        raise AppError('CONFIG_ERROR', 'ehall 提交服务未配置。')
                    return self.ehall_worker.enqueue_submit(task_id, data.get('preview_id'))
                elif action == 'reconcile':
                    if self.ehall_submit is None or self.ehall_worker is None:
                        raise AppError('CONFIG_ERROR', 'ehall 核对服务未配置。')
                    return self.ehall_worker.enqueue_reconcile(task_id, data.get('submission_id'))
                elif action == 'archive':
                    return self.ehall.public(self.ehall.archive(task_id))
                elif action == 'restore':
                    return self.ehall.public(self.ehall.restore(task_id))
                else:
                    raise AppError('NOT_FOUND', '接口不存在。')
                if self.ehall_worker and result['status'] in ('new', 'ready_to_fill', 'needs_review'):
                    self.ehall_worker.enqueue(task_id)
                return self.ehall.public(result)
        if (path.startswith('/api/automation') or path.startswith('/api/mail-queue')) and self.scheduler is None:
            raise AppError('CONFIG_ERROR', '持续邮件调度器未配置。')
        if method == 'GET' and path == '/api/automation':
            return self.scheduler.status()
        if method == 'GET' and path == '/api/mail-queue':
            return self.scheduler.items()
        if method == 'GET' and path == '/api/mail-queue/handled':
            return self.scheduler.items(include_dismissed=True, limit=10)
        if method == 'POST' and path == '/api/automation/pause':
            return self.scheduler.pause()
        if method == 'POST' and path == '/api/automation/resume':
            return self.scheduler.resume()
        if method == 'POST' and path == '/api/automation/scan':
            return self.scheduler.cycle(force=True)
        if method == 'POST' and path == '/api/mail-queue/correct':
            return self.scheduler.correct(data.get('validity'), data.get('uid'), data.get('category'),
                                          data.get('reason'), data.get('suggested_goal', ''),
                                          data.get('decision_question', ''))
        if method == 'POST' and path == '/api/mail-queue/dismiss':
            return self.scheduler.dismiss(data.get('validity'), data.get('uid'))
        if method == 'POST' and path == '/api/mail-queue/restore':
            return self.scheduler.restore(data.get('validity'), data.get('uid'))
        if method == 'POST' and path == '/api/attachments':
            return self.sender.upload(data.get('name'), data.get('base64'))
        if method == 'GET' and path == '/api/tasks':
            return self.task_summaries()
        if method == 'GET' and path == '/api/tasks/archived':
            return self.task_summaries(archived=True, limit=10)
        if method == 'GET' and path == '/api/snapshots':
            rows = []
            for p in sorted((self.data / 'mail').glob('*/message.json')):
                if re.fullmatch('[a-f0-9]{64}', p.parent.name):
                    m = json.loads(p.read_text())
                    rows.append({'id': p.parent.name, 'subject': m['Subject'], 'from': m['From'], 'folder': m['folder']})
            return rows
        if method == 'GET' and path == '/api/inbox':
            if self.mail_config is None:
                raise AppError('CONFIG_ERROR', '未配置邮箱。')
            reader = Reader(self.mail_config)
            try:
                reader.select('INBOX')
                return reader.recent(10)
            finally:
                reader.close()
        if method == 'POST' and path == '/api/import':
            if self.mail_config is None:
                raise AppError('CONFIG_ERROR', '未配置邮箱。')
            reader = Reader(self.mail_config)
            try:
                reader.select('INBOX')
                if data.get('uidvalidity') != reader.validity:
                    raise AppError('INPUT_ERROR', '邮箱标识已变化，请刷新邮件列表。')
                parsed, raw = reader.fetch(data.get('uid', ''))
                directory = save_snapshot(self.data / 'mail', parsed, raw)
                return {'id': directory.name}
            finally:
                reader.close()
        if method == 'POST' and path == '/api/tasks':
            history = data.get('history', [])
            if not isinstance(history, list) or len(history) > 4:
                raise AppError('INPUT_ERROR', '历史最多四封。')
            return app.create(self.snapshot_path(data.get('snapshot')), data.get('goal'),
                              [self.snapshot_path(k) for k in history])
        match = re.fullmatch('/api/tasks/([a-f0-9]{64})(?:/(answer|decide|resume|edit|replan|retry-child|conversation-update|prepare-send|confirm-send|send|reconcile|archive|restore|experience))?', path)
        if match:
            task_id, action = match.groups()
            if method == 'GET' and action == 'experience':
                task = app.get(task_id)  # Enforce task existence before exposing scoped audit data.
                if self.experience is None:
                    raise AppError('CONFIG_ERROR', '经验规则服务未配置。')
                return {
                    'task_id': task_id,
                    'experience_snapshot': task.get('experience_snapshot'),
                    'applications': self.experience.store.applications('mail_task', task_id),
                }
            if method == 'GET' and action is None:
                task = app.get(task_id)
                task['send_records'] = self.sender.history(task_id)
                task['requests'] = app.visible_requests(task)
                task['archived'] = task_id in app.archived_task_ids()
                task['experience_applications'] = (self.experience.store.applications(
                    'mail_task', task_id) if self.experience is not None else [])
                return task
            if method == 'POST':
                if action != 'restore' and task_id in app.archived_task_ids():
                    raise AppError('INPUT_ERROR', '任务已归档，请先恢复后再操作。')
                if action == 'prepare-send':
                    return self.sender.preview(task_id, data.get('version'))
                if action == 'confirm-send':
                    require_confirm = data.get('confirmed') is True
                    if not require_confirm:
                        raise AppError('INPUT_ERROR', '请明确确认预览内容。')
                    return self.sender.confirm(task_id, data.get('send_id'), data.get('fingerprint'))
                if action == 'send':
                    return self.sender.send(task_id, data.get('send_id'))
                if action == 'reconcile':
                    return self.sender.reconcile(task_id, data.get('send_id'))
                if action == 'archive':
                    records = self.sender.history(task_id)
                    accepted = [record for record in records if record['status'] == 'accepted']
                    if not accepted:
                        raise AppError('INPUT_ERROR', '只有 SMTP 已接收的回复任务可以归档。')
                    return app.archive(task_id, {'accepted_send_id': accepted[-1]['id']})
                if action == 'restore':
                    return app.restore_archive(task_id)
                if action == 'replan':
                    return app.replan(task_id)
                if action == 'retry-child':
                    return app.retry_child(task_id, data.get('child_id'))
                if action == 'conversation-update':
                    return app.review_conversation_update(task_id, data.get('source_id'), data.get('action'))
                if action == 'answer':
                    if type(data.get('confirm_conflict', False)) is not bool:
                        raise AppError('INPUT_ERROR', '冲突确认须为布尔值。')
                    return app.answer(task_id, data.get('request_id'), data.get('text'), data.get('scope'),
                                      data.get('confirm_conflict', False))
                if action == 'decide':
                    return app.decide(task_id, data.get('decision_id'), data.get('text'))
                if action == 'resume':
                    return app.resume(task_id)
                if action == 'edit':
                    for a in data.get('attachments', []) or []:
                        self.sender.blob(a)
                    return app.edit(task_id, data.get('version'), data.get('body'), data.get('subject'), data.get('to'),
                                    data.get('cc'), data.get('bcc'), data.get('attachments'))
        raise AppError('NOT_FOUND', '接口不存在。')


def handler(app):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass  # Do not log credentials or private request content.

        def respond(self, status, value, mime='application/json; charset=utf-8'):
            raw = value if isinstance(value, bytes) else json.dumps(value, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header('Content-Type', mime)
            self.send_header('Content-Length', str(len(raw)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Referrer-Policy', 'no-referrer')
            self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' blob:; frame-ancestors 'none'; base-uri 'none'")
            self.end_headers()
            try:
                self.wfile.write(raw)
            except (BrokenPipeError, ConnectionResetError):
                pass  # Work was persisted even when the browser disconnected.

        def do_GET(self):
            self.handle_request('GET')

        def do_POST(self):
            self.handle_request('POST')

        def handle_request(self, method):
            path = urlsplit(self.path).path
            assets = {'/': ('index.html', 'text/html'), '/app.js': ('app.js', 'text/javascript'),
                      '/style.css': ('style.css', 'text/css')}
            if method == 'GET' and path in assets:
                name, mime = assets[path]
                return self.respond(200, (ROOT / 'web' / name).read_bytes(), mime + '; charset=utf-8')
            auth = self.headers.get('Authorization', '')
            if not hmac.compare_digest(auth.encode(), ('Bearer ' + app.token).encode()):
                return self.respond(401, {'error': '请先输入访问口令。'})
            # No cookies or CORS; browser requests must originate from this host.
            origin = self.headers.get('Origin')
            if origin and origin != 'http://' + self.headers.get('Host', ''):
                return self.respond(403, {'error': '拒绝跨站请求。'})
            try:
                data = {}
                if method == 'GET' and path == '/api/ehall/login/qr':
                    return self.respond(200, app.ehall_qr(), 'image/png')
                if method == 'POST':
                    length = int(self.headers.get('Content-Length', '0'))
                    limit = 8 * 1024 * 1024 if path in (
                        '/api/attachments', '/api/ehall/attachments') else 100000
                    if not 0 < length <= limit or self.headers.get('Content-Type', '').split(';')[0] != 'application/json':
                        return self.respond(400, {'error': '请求不是 JSON 或超过大小限制。'})
                    data = json.loads(self.rfile.read(length))
                    if not isinstance(data, dict):
                        raise ValueError()
                result = app.dispatch(method, path, data)
                return self.respond(200, result)
            except AppError as error:
                status = 409 if error.code in ('VERSION_CONFLICT', 'DRAFT_STALE', 'TASK_BUSY') else 400
                if error.code in ('NOT_FOUND', 'MAIL_NOT_FOUND', 'EHALL_NOT_FOUND'):
                    status = 404
                self.respond(status, {'error': str(error), 'code': error.code})
            except (ValueError, TypeError, KeyError):
                self.respond(400, {'error': '输入格式无效。'})
            except (OSError, MailError):
                self.respond(503, {'error': '文件或邮箱连接不可用，请检查本地配置后重试。'})
    return Handler


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=8765)
    args = parser.parse_args()
    os.umask(0o077)
    token_path = ROOT / 'data' / 'web-token.txt'
    token_path.parent.mkdir(parents=True, exist_ok=True)
    if not token_path.exists():
        with token_path.open('x') as stream:
            stream.write(secrets.token_urlsafe(32))
    token_path.chmod(0o600)
    token = token_path.read_text().strip()
    if len(token) < 32:
        raise SystemExit('访问口令过短，请更换 data/web-token.txt。')
    assistant = Assistant(load_config(ROOT / 'config.local.json'))
    experience_store = ExperienceStore(assistant.db_path, ROOT/'experience/rules/mail')
    tasks = MailTasks(assistant, experience_store)
    experience = ExperienceService(experience_store, assistant.client)
    config = json.loads((ROOT / 'mail.local.json').read_text()) if (ROOT / 'mail.local.json').exists() else None
    scheduler = None
    if config:
        from mail_classifier import Classifier
        from mail_monitor import Monitor
        from mail_pipeline import Pipeline
        from mail_scheduler import Scheduler
        monitor = Monitor(config, ROOT/'data/monitor')
        classifier = Classifier(monitor.inbox, assistant.client, experience=experience_store)
        scheduler = Scheduler(monitor, classifier, Pipeline(classifier, tasks))
    ehall = ehall_worker = ehall_files = ehall_submit = None
    if (ROOT / 'ehall.local.json').exists():
        from ehall_tasks import EhallAttachmentStore, EhallTasks
        from ehall_submit import EhallSubmitService
        from ehall_worker import EhallWorker
        ehall_files = EhallAttachmentStore(ROOT / 'data' / 'ehall' / 'attachments')
        ehall = EhallTasks(assistant, attachment_store=ehall_files)
        ehall_submit = EhallSubmitService(ehall)
        ehall_worker = EhallWorker(ehall)
    server = ThreadingHTTPServer((args.host, args.port),
                                 handler(WebApp(tasks, ROOT / 'data', token, config, scheduler,
                                                ehall, ehall_worker, ehall_files, ehall_submit,
                                                experience)))
    print('网页 http://{}:{}；访问口令保存在 {}'.format(args.host, server.server_port, token_path), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
