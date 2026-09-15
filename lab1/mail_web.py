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
from mail_tasks import MailTasks
from mail_reader import Reader, MailError, save_snapshot
from mail_send import SendService


class WebApp:
    def __init__(self, tasks, data, token, mail_config=None, scheduler=None):
        self.tasks, self.data, self.token = tasks, Path(data), token
        self.mail_config = mail_config
        self.scheduler = scheduler
        self.sender = SendService(tasks, self.data, mail_config)
        self.sender.recover()

    def snapshot_path(self, key):
        if not isinstance(key, str) or not re.fullmatch('[a-f0-9]{64}', key):
            raise AppError('INPUT_ERROR', '邮件快照编号无效。')
        path = self.data / 'mail' / key
        if path.is_symlink() or not (path / 'message.json').is_file():
            raise AppError('INPUT_ERROR', '邮件快照不存在。')
        return path

    def dispatch(self, method, path, data):
        app = self.tasks
        if path in ('/api/automation', '/api/mail-queue') and self.scheduler is None:
            raise AppError('CONFIG_ERROR', '持续邮件调度器未配置。')
        if method == 'GET' and path == '/api/automation':
            return self.scheduler.status()
        if method == 'GET' and path == '/api/mail-queue':
            return self.scheduler.items()
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
        if method == 'POST' and path == '/api/attachments':
            return self.sender.upload(data.get('name'), data.get('base64'))
        if method == 'GET' and path == '/api/tasks':
            with sqlite3.connect(str(app.db_path)) as db:
                rows = [json.loads(r[0]) for r in db.execute('SELECT payload FROM mail_tasks')]
            summaries = [{'task_id': t['task_id'], 'subject': t['mail']['Subject'], 'status': t['status'],
                     'created_at': t['created_at']} for t in sorted(rows, key=lambda t:t['created_at'], reverse=True)]
            for summary in summaries:
                records = self.sender.history(summary['task_id'])
                summary['delivery_status'] = records[-1]['status'] if records else None
            return summaries
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
        match = re.fullmatch('/api/tasks/([a-f0-9]{64})(?:/(answer|decide|resume|edit|replan|retry-child|conversation-update|prepare-send|confirm-send|send|reconcile))?', path)
        if match:
            task_id, action = match.groups()
            if method == 'GET' and action is None:
                task = app.get(task_id)
                task['send_records'] = self.sender.history(task_id)
                task['requests'] = app.visible_requests(task)
                return task
            if method == 'POST':
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
            self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'")
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
                if method == 'POST':
                    length = int(self.headers.get('Content-Length', '0'))
                    limit = 8 * 1024 * 1024 if path == '/api/attachments' else 100000
                    if not 0 < length <= limit or self.headers.get('Content-Type', '').split(';')[0] != 'application/json':
                        return self.respond(400, {'error': '请求不是 JSON 或超过大小限制。'})
                    data = json.loads(self.rfile.read(length))
                    if not isinstance(data, dict):
                        raise ValueError()
                result = app.dispatch(method, path, data)
                return self.respond(200, result)
            except AppError as error:
                status = 409 if error.code in ('VERSION_CONFLICT', 'DRAFT_STALE', 'TASK_BUSY') else 400
                if error.code in ('NOT_FOUND', 'MAIL_NOT_FOUND'):
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
    tasks = MailTasks(assistant)
    config = json.loads((ROOT / 'mail.local.json').read_text()) if (ROOT / 'mail.local.json').exists() else None
    scheduler = None
    if config:
        from mail_classifier import Classifier
        from mail_monitor import Monitor
        from mail_pipeline import Pipeline
        from mail_scheduler import Scheduler
        monitor = Monitor(config, ROOT/'data/monitor')
        classifier = Classifier(monitor.inbox, assistant.client)
        scheduler = Scheduler(monitor, classifier, Pipeline(classifier, tasks))
    server = ThreadingHTTPServer((args.host, args.port),
                                 handler(WebApp(tasks, ROOT / 'data', token, config, scheduler)))
    print('网页 http://{}:{}；访问口令保存在 {}'.format(args.host, server.server_port, token_path), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
