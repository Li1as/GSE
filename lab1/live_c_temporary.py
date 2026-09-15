"""Real API temporary-scope check with generated data, isolated from personal files."""
import json
import tempfile
from pathlib import Path

from assistant import Assistant, ROOT, load_config


def main():
    config = load_config(ROOT/'config.local.json')
    with tempfile.TemporaryDirectory(prefix='c-temporary-', dir=str(ROOT/'data')) as directory:
        path = Path(directory)
        personal = path/'personal'
        personal.mkdir()
        (personal/'resume.md').write_text('# 样例\n\n## 活动\n\n准备参加读书活动，取件柜编号尚未提供。\n', encoding='utf-8')
        config['personal_data_dir'] = str(personal)
        app = Assistant(config, db_path=path/'tasks.sqlite')
        task = app.query('本次读书活动应去哪个编号的取件柜取资料？')
        assert task['status'] == 'waiting_input', task.get('error', task['status'])
        print('PASS: generated missing request', flush=True)
        resumed = app.answer_request(task['request_id'], '本次活动取件柜编号是 GX-42。', 'task')
        assert resumed['task_id'] == task['task_id']
        assert resumed['status'] in ('completed', 'needs_review'), resumed.get('error', resumed['status'])
        assert 'GX-42' in json.dumps(resumed['result'], ensure_ascii=False)
        assert any(e['file'].startswith('task:') for e in resumed['result']['evidence'])
        assert not list(personal.glob('supplement-*.md'))
        print('PASS: same task resumed with temporary evidence; no personal archive', flush=True)
        fresh = Assistant(config, db_path=path/'tasks.sqlite').query('另一个独立任务：取件柜编号是什么？')
        assert fresh['status'] == 'waiting_input', fresh.get('error', fresh['status'])
        assert 'GX-42' not in json.dumps(fresh, ensure_ascii=False)
        print('PASS: new task cannot access temporary answer', flush=True)
        (ROOT/'data/live-c-temporary.json').write_text(json.dumps({
            'passed': True, 'initial': task, 'resumed': resumed, 'fresh': fresh
        }, ensure_ascii=False, indent=2), encoding='utf-8')


if __name__ == '__main__':
    main()
