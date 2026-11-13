"""节点命令必须显式执行，并保存未知状态阻止重复写。"""
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import tapd_cli
from tapd_client import TapdError


class CliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.evidence = self.root / 'evidence.md'
        self.evidence.write_text('实现、必要验证和独立审查均已完成；来源：本机交付记录。')
        self.args = ['--credentials', '/unused', '--state', str(self.root / 'state.json'), 'complete-step',
                     '--tapd', 'https://www.tapd.cn/tapd_fe/44667425/story/detail/1144667425001026696',
                     '--step', '开发中', '--evidence-file', str(self.evidence)]
        self.info = {'story': {'id': '1144667425001026696', 'workspace_id': '44667425'},
                     'fields': {}, 'transitions': [],
                     'steps': [{'WorkitemStepInfo': {'step': 'dev', 'status': '0', 'begin_time': '2026-11-03'}}],
                     'step_map': [{'name': 'progressing', 'label': '进行中', 'steps': [{'name': 'dev', 'label': '开发中'}]}]}

    def call(self, args, complete):
        with patch.object(tapd_cli, 'TapdClient'), patch.object(tapd_cli, 'inspect_story', return_value=self.info), \
                patch.object(tapd_cli, 'complete_step', side_effect=complete), contextlib.redirect_stdout(io.StringIO()):
            return tapd_cli.main(args)

    def test_default_preview_does_not_request_execution(self):
        def complete(*args, **kwargs):
            self.assertFalse(kwargs['execute'])
            return {'status': 'planned'}
        self.assertEqual(self.call(self.args, complete), 0)
        self.assertFalse((self.root / 'state.json').exists())

    def test_unknown_result_blocks_a_second_execute(self):
        calls = []
        def complete(*args, **kwargs):
            calls.append(1)
            kwargs['before_write']()
            raise TapdError('模拟超时', uncertain=True)
        self.assertEqual(self.call(self.args + ['--execute'], complete), 1)
        self.assertEqual(self.call(self.args + ['--execute'], complete), 1)
        self.assertEqual(len(calls), 1)

    def test_completed_readback_resolves_unknown_without_post(self):
        def complete(*args, **kwargs):
            kwargs['before_write']()
            raise TapdError('模拟超时', uncertain=True)
        self.call(self.args + ['--execute'], complete)
        self.info['steps'][0]['WorkitemStepInfo']['status'] = '1'
        self.assertEqual(self.call(self.args + ['--execute'], lambda *a, **k: self.fail('不应重复完成')), 0)

    def test_execute_requires_evidence_before_request(self):
        self.evidence.write_text('')
        self.assertEqual(self.call(self.args + ['--execute'], lambda *a, **k: self.fail('不得执行')), 1)

    def test_inspect_reports_current_required_field_missing(self):
        self.info['story']['custom_field_24'] = ''
        self.info['fields']['custom_field_24'] = {'label': '实际工时（人/日）', 'html_type': 'float'}
        self.info['transitions'] = [{'StepPrevious': 'dev', 'StepNext': 'done', 'Appendfield': [
            {'DBModel': 'Story', 'FieldName': 'custom_field_24', 'Notnull': 'yes'}]}]
        result = tapd_cli.inspect_result(self.info)
        self.assertIn('缺少必填字段：实际工时（人/日）', result['active_node_checks'][0]['conflicts'])
        self.assertEqual(result['fields']['custom_field_24']['current_value'], '')
