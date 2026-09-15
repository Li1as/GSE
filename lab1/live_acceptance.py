"""Explicit, billable live acceptance. Uses only the configured API and local corpus."""
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from assistant import Assistant, ROOT, load_config


def evaluate(name, task, expected):
    if task['status'] == 'failed':
        return False
    result = task['result']
    answers = '\n'.join(a['text'] for a in result['answers'])
    evidence = result['evidence']
    if name == 'database':
        return expected['database_score'] in answers and any(e['file'] == 'transcript.md' and '数据库概论' in e['text'] for e in evidence)
    if name == 'alias':
        return expected['alias_score'] in answers and any('计算机程序的构造和解释' in e['text'] for e in evidence)
    if name == 'repeated':
        return all(t in answers for t in expected['repeated_values']) and sum(e['file'] == 'transcript.md' and '篮球初级' in e['text'] for e in evidence) == 3
    if name == 'missing':
        return task['status'] == 'waiting_input' and bool(result['missing']) and not result['answers']
    if name == 'score_scope':
        return all(value in answers for value in expected['project_scores']) and {'resume.md', 'transcript.md'} <= {e['file'] for e in evidence}
    if name == 'date_conflict':
        return bool(result['conflicts']) and any(expected['conflicting_date'] in e['text'] for e in evidence)
    return False


def main():
    expected = json.loads((ROOT/'data/live-expectations.json').read_text(encoding='utf-8'))
    config = load_config(ROOT/'config.local.json')

    def run(case):
        name, query = case
        task = Assistant(config).query(query)
        row = {'case': name, 'passed': evaluate(name, task, expected), 'task_id': task['task_id'],
               'status': task['status'], 'steps': len(task['trace'])}
        if 'error' in task:
            row['error'] = task['error']
        print(json.dumps(row, ensure_ascii=False), flush=True)
        return row

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(run, expected['cases']))
    report = {'at': datetime.now(timezone.utc).isoformat(), 'model': config['model'], 'cases': results}
    (ROOT/'data/live-acceptance.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    return 0 if all(r['passed'] for r in results) else 1


if __name__ == '__main__':
    raise SystemExit(main())
