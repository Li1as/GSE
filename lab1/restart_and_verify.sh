#!/usr/bin/env bash
set -Eeuo pipefail

PROJECT_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
UNIT_SOURCE="$PROJECT_ROOT/lab1/systemd"
UNIT_TARGET="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
SERVICES=(gse-mail-scheduler.service gse-ehall-worker.service gse-mail-web.service)
RUN_TESTS=1

usage() {
  echo "用法：$0 [--skip-tests]"
  echo "默认运行离线测试、安装服务模板、重启服务并验证本机接口。"
}

case "${1:-}" in
  "") ;;
  --skip-tests) RUN_TESTS=0 ;;
  -h|--help) usage; exit 0 ;;
  *) usage >&2; exit 2 ;;
esac

fail() {
  echo "失败：第 $1 行命令执行失败。" >&2
  systemctl --user --no-pager --full status "${SERVICES[@]}" >&2 || true
}
trap 'fail "$LINENO"' ERR

cd "$PROJECT_ROOT"
echo "[1/5] 检查代码"
python3 -m compileall -q lab1
if command -v node >/dev/null 2>&1; then
  node --check lab1/web/app.js
else
  echo "提示：未安装 node，跳过 JavaScript 语法检查。"
fi

if (( RUN_TESTS )); then
  echo "[2/5] 运行离线回归测试"
  python3 -m unittest discover -s lab1 -p 'test_*.py'
else
  echo "[2/5] 已按参数跳过离线回归测试"
fi

echo "[3/5] 同步并重启 systemd 用户服务"
install -d -m 0755 "$UNIT_TARGET"
for service in "${SERVICES[@]}"; do
  install -m 0644 "$UNIT_SOURCE/$service" "$UNIT_TARGET/$service"
done
systemctl --user daemon-reload
systemctl --user enable "${SERVICES[@]}" >/dev/null
systemctl --user restart "${SERVICES[@]}"

echo "[4/5] 等待网页服务并检查受保护接口"
python3 - "$PROJECT_ROOT" <<'PY'
import json
import pathlib
import sys
import time
import urllib.error
import urllib.request

root = pathlib.Path(sys.argv[1])
token = (root / 'lab1/data/web-token.txt').read_text().strip()
base = 'http://127.0.0.1:8765'
deadline = time.monotonic() + 20

while True:
    try:
        with urllib.request.urlopen(base + '/', timeout=2) as response:
            if response.status == 200:
                break
    except (OSError, urllib.error.URLError):
        if time.monotonic() >= deadline:
            raise SystemExit('网页服务在 20 秒内未就绪。')
        time.sleep(0.5)

try:
    urllib.request.urlopen(base + '/api/tasks', timeout=2)
    raise SystemExit('未授权请求未被拒绝。')
except urllib.error.HTTPError as error:
    if error.code != 401:
        raise

headers = {'Authorization': 'Bearer ' + token}
paths = ('/api/tasks', '/api/tasks/archived', '/api/mail-queue', '/api/mail-queue/handled',
         '/api/ehall/tasks', '/api/ehall/tasks/archived',
         '/api/experience/proposals', '/api/experience/rules')
counts = {}
values = {}
for path in paths:
    request = urllib.request.Request(base + path, headers=headers)
    with urllib.request.urlopen(request, timeout=5) as response:
        value = json.loads(response.read())
        if response.status != 200 or not isinstance(value, list):
            raise SystemExit(path + ' 返回格式无效。')
        counts[path] = len(value)
        values[path] = value

if (counts['/api/tasks/archived'] > 10 or counts['/api/mail-queue/handled'] > 10 or
        counts['/api/ehall/tasks/archived'] > 10):
    raise SystemExit('历史接口返回超过 10 条记录。')
if values['/api/tasks']:
    task_id = values['/api/tasks'][0].get('task_id', '')
    request = urllib.request.Request(base + '/api/tasks/' + task_id + '/experience', headers=headers)
    with urllib.request.urlopen(request, timeout=5) as response:
        value = json.loads(response.read())
        if (response.status != 200 or set(value) != {
                'task_id', 'experience_snapshot', 'applications'} or value['task_id'] != task_id):
            raise SystemExit('任务经验审计接口返回格式无效。')
request = urllib.request.Request(base + '/api/ehall/login', headers=headers)
with urllib.request.urlopen(request, timeout=5) as response:
    value = json.loads(response.read())
    if response.status != 200 or value.get('status') not in ('idle', 'waiting_scan'):
        raise SystemExit('/api/ehall/login 返回格式无效。')
print('接口验证通过：' + '，'.join(f'{path}={count}' for path, count in counts.items()))
PY

echo "[5/5] 检查服务状态"
systemctl --user is-active "${SERVICES[@]}"
python3 lab1/mail_scheduler.py status >/dev/null
trap - ERR
echo "完成：服务已更新、重启并通过验证。"
