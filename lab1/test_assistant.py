import io
import json
import socket
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import Mock

from assistant import API, AppError, Assistant, Corpus, ROOT, load_config


class ScriptedClient:
    def __init__(self, responses):
        self.responses = iter(responses)

    def complete(self, messages):
        response = next(self.responses)
        if isinstance(response, Exception):
            raise response
        if callable(response):
            response = response(messages)
        return response if isinstance(response, str) else json.dumps(response)


class Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)
        (self.path / 'transcript.md').write_text(
            '# 成绩\n\n## 明细\n\n| 学期 | 课程号 | 课程 | 成绩 |\n| --- | --- | --- | --- |\n'
            '| 2025-1 | 001 | 计算机程序的构造和解释 | 91 |\n'
            '| 2025-1 | 002 | 篮球初级 | 72 |\n'
            '| 2025-2 | 002 | 篮球初级 | 73 |\n', encoding='utf-8')
        self.config = {'api_url': 'https://example.com', 'api_key': 'test-secret',
                       'model': 'gpt-5.6-terra', 'api_protocol': 'chat_completions',
                       'personal_data_dir': str(self.path)}
        self.config_file = self.path/'config.json'
        self.config_file.write_text(json.dumps(self.config))
        self.config = load_config(self.config_file)
        self.corpus = Corpus(self.path, json.loads((ROOT/'retrieval-aliases.json').read_text()))

    def app(self, responses):
        return Assistant(self.config, ScriptedClient(responses), self.path/'tasks.sqlite')

    def test_alias_and_table_header(self):
        result = self.corpus.search(['cs61a'])['results']
        self.assertEqual(len(result), 1)
        self.assertIn('91', result[0]['text'])
        self.assertIn('| 学期 |', result[0]['text'])
        self.assertEqual(result[0]['line_start'], 7)

    def test_repeated_course_and_category(self):
        self.assertEqual(self.corpus.search(['篮球'], 'transcript.md')['total'], 2)
        self.assertEqual(self.corpus.search(['篮球'], 'resume.md')['total'], 0)

    def test_pagination(self):
        (self.path/'notes.md').write_text('# 测试\n\n'+'\n\n'.join('信息'+str(i) for i in range(15)))
        c = Corpus(self.path, {})
        first, second = c.search(['信息']), c.search(['信息'], offset=8)
        self.assertEqual((len(first['results']), len(second['results'])), (8, 7))
        self.assertFalse({x['id'] for x in first['results']} & {x['id'] for x in second['results']})

    def test_versions_change_and_delete(self):
        original = self.corpus.search(['cs61a'])['results'][0]
        p = self.path/'transcript.md'
        p.write_text(p.read_text().replace('91', '92'))
        new = Corpus(self.path, self.corpus.aliases).search(['cs61a'])['results'][0]
        self.assertNotEqual(original['version'], new['version'])
        p.unlink()
        with self.assertRaises(AppError):
            Corpus(self.path, {})

    def test_query_and_saved_evidence(self):
        def finish(messages):
            row = json.loads(messages[-1]['content'])['tool_result']['results'][0]
            return {'action': 'final', 'answers': [{'text': '课程成绩91', 'citations': [row['id']]}], 'missing': [], 'conflicts': []}
        app = self.app([{'action': 'search_personal_info', 'keywords': ['CS61A']}, finish])
        task = app.query('这门课程成绩？')
        self.assertEqual(task['status'], 'completed')
        saved = Assistant({}, db_path=self.path/'tasks.sqlite').get_task(task['task_id'])
        self.assertEqual(saved['result']['evidence'][0]['text'], self.corpus.search(['cs61a'])['results'][0]['text'])

    def test_missing_after_two_searches(self):
        task = self.app([{'action': 'search_personal_info', 'keywords': ['护照']},
                         {'action': 'search_personal_info', 'keywords': ['旅行证件']},
                         {'action': 'final', 'answers': [], 'missing': ['护照有效期'], 'conflicts': []}]).query('护照何时到期')
        self.assertEqual(task['status'], 'waiting_input')

    def test_missing_requires_search(self):
        task = self.app([{'action': 'search_personal_info', 'keywords': ['护照']},
                         {'action': 'final', 'answers': [], 'missing': ['护照有效期'], 'conflicts': []}]).query('护照何时到期')
        self.assertEqual(task['error']['code'], 'INSUFFICIENT_SEARCH')

    def test_fabricated_citation_rejected(self):
        task = self.app([{'action': 'search_personal_info', 'keywords': ['CS61A']},
                         {'action': 'final', 'answers': [{'text': '100', 'citations': ['invented']}],
                          'missing': [], 'conflicts': []}]).query('成绩')
        self.assertEqual(task['error']['code'], 'INVALID_CITATION')

    def test_invalid_actions_and_json(self):
        for output in ('not json', {'action': 'shell', 'command': 'anything'},
                       {'action': 'read_evidence', 'ids': ['../../config.local.json']},
                       {'action': 'search_personal_info', 'keywords': 'string'}):
            with self.subTest(output=output):
                self.assertEqual(self.app([output, output]).query('查询')['error']['code'], 'MODEL_PROTOCOL')

    def test_project_title_keeps_description(self):
        (self.path/'resume.md').write_text('# 简历\n\n## 项目\n\n1. 数据库项目\n\n   实现 ARIES。\n\n2. 其他项目\n\n   实现界面。\n')
        chunks = Corpus(self.path, {}).search(['数据库'])['results']
        self.assertEqual(len(chunks), 1)
        self.assertIn('ARIES', chunks[0]['text'])
        self.assertNotIn('界面', chunks[0]['text'])

    def test_one_json_repair_within_step_budget(self):
        task = self.app(['bad json', {'action': 'search_personal_info', 'keywords': ['护照']},
                         {'action': 'search_personal_info', 'keywords': ['旅行证件']},
                         {'action': 'final', 'answers': [], 'missing': ['有效期'], 'conflicts': []}]).query('查询')
        self.assertEqual(task['status'], 'waiting_input')
        self.assertEqual(task['trace'][0]['action']['action'], 'format_retry')

    def test_timeout_is_not_missing(self):
        task = self.app([AppError('API_TIMEOUT', '请求超时')]).query('查询')
        self.assertEqual(task['status'], 'failed')
        self.assertNotIn('result', task)

    def test_step_limit(self):
        self.config['max_steps'] = 1
        task = self.app([{'action': 'list_sources'}]).query('查询')
        self.assertEqual(task['error']['code'], 'STEP_LIMIT')

    def test_configuration_validation(self):
        for key in ('api_url', 'api_key', 'model'):
            config = dict(self.config, **{key: ''})
            self.config_file.write_text(json.dumps(config))
            with self.assertRaises(AppError) as error:
                load_config(self.config_file)
            self.assertEqual(error.exception.code, 'CONFIG_ERROR')
        for url in ('https://example.com', 'https://example.com/v1', 'https://example.com/v1/chat/completions'):
            self.config_file.write_text(json.dumps(dict(self.config, api_url=url)))
            self.assertEqual(load_config(self.config_file)['endpoint'], 'https://example.com/v1/chat/completions')

    def test_http_errors_redact_remote_content(self):
        api = API(self.config)
        for status, expected in ((401, 'AUTH_ERROR'), (403, 'AUTH_ERROR'), (429, 'API_HTTP'), (500, 'API_HTTP')):
            api.opener = Mock()
            api.opener.open.side_effect = urllib.error.HTTPError('https://example.com', status, 'test-secret', {}, io.BytesIO(b'test-secret'))
            with self.assertRaises(AppError) as error:
                api.complete([])
            self.assertEqual(error.exception.code, expected)
            self.assertNotIn('test-secret', str(error.exception))

    def test_transport_timeout_and_malformed_response(self):
        api = API(self.config)
        api.opener = Mock()
        api.opener.open.side_effect = socket.timeout()
        with self.assertRaises(AppError) as error:
            api.complete([])
        self.assertEqual(error.exception.code, 'API_TIMEOUT')
        api.opener.open.side_effect = None
        api.opener.open.return_value.__enter__ = Mock(return_value=io.BytesIO(b'not-json'))
        api.opener.open.return_value.__exit__ = Mock(return_value=False)
        with self.assertRaises(AppError) as error:
            api.complete([])
        self.assertEqual(error.exception.code, 'API_FORMAT')


if __name__ == '__main__':
    unittest.main()
