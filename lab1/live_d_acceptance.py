"""Explicit API recovery/update acceptance on isolated, generated Markdown."""
import json
import tempfile
from pathlib import Path

from assistant import Assistant, ROOT, load_config


class CrashClient:
    def complete(self, messages):
        raise SystemExit('simulated process interruption before API call')


def main():
    config = load_config(ROOT/'config.local.json')
    with tempfile.TemporaryDirectory(prefix='d-acceptance-', dir=str(ROOT/'data')) as folder:
        base = Path(folder)
        personal = base/'personal'
        personal.mkdir()
        (personal/'resume.md').write_text('# 简历\n\n## 基本信息\n\n这是一份虚构课程测试资料。\n', encoding='utf-8')
        score = personal/'transcript.md'
        score.write_text('# 课程成绩\n\n## 测试课程\n\n测试课程总评为91分。\n', encoding='utf-8')
        config['personal_data_dir'] = str(personal)
        app = Assistant(config, CrashClient(), base/'tasks.sqlite')
        try:
            app.query('测试课程的总评是多少？请给出来源。')
        except SystemExit:
            pass
        restarted = Assistant(config, db_path=base/'tasks.sqlite')
        report = restarted.recover()
        assert len(report['recovered']) == 1, report
        task_id = report['recovered'][0]['task_id']
        first = restarted.get_task(task_id)
        assert first['status'] in ('completed', 'needs_review'), first.get('error')
        assert '91' in ''.join(a['text'] for a in first['result']['answers'])
        print('PASS: interrupted task recovered from SQLite through real API', flush=True)
        score.write_text('# 课程成绩\n\n## 测试课程\n\n测试课程总评更新为95分。\n', encoding='utf-8')
        assert restarted.get_task(task_id)['freshness']['status'] == 'stale'
        newer = restarted.query('测试课程现在的总评是多少？')
        assert newer['status'] in ('completed', 'needs_review'), newer.get('error')
        assert '95' in ''.join(a['text'] for a in newer['result']['answers'])
        print('PASS: modified Markdown used; historical answer marked stale', flush=True)
        score.unlink()
        absent = restarted.query('测试课程目前的总评是多少？')
        assert absent['status'] == 'waiting_input', absent.get('error', absent['status'])
        assert not absent['result']['answers'], absent['result']['answers']
        print('PASS: deleted information becomes missing, old answer not reused', flush=True)
        (ROOT/'data/live-d-acceptance.json').write_text(json.dumps({
            'passed': True, 'recovery': report, 'first': first, 'updated': newer, 'deleted': absent
        }, ensure_ascii=False, indent=2), encoding='utf-8')


if __name__ == '__main__':
    main()
