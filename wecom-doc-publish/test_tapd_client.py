"""TAPD 客户端的离线契约测试，不使用真实凭据或远端写入。"""

import copy
import importlib.util
from http.client import IncompleteRead
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit

try:
    import tapd_client as tapd
except ImportError:
    tapd = None

WORKSPACE = '12345678'
STORY_ID = '1123456789012345678'
TYPE_ID = '1123456789001000001'


def fields():
    return {
        'custom_field_one': {'name': 'custom_field_one', 'label': '涉及端口',
                             'html_type': 'multi_select', 'options': {'server': '后端', 'web': '前端'}, 'readonly': 0},
        'custom_field_two': {'name': 'custom_field_two', 'label': '需求关联系统',
                             'html_type': 'multi_select', 'options': {'api': 'API', 'admin': '后台'}, 'readonly': 0},
        'custom_field_three': {'name': 'custom_field_three', 'label': '发布关联操作',
                               'html_type': 'multi_select', 'options': {'SQL': 'SQL', '菜单': '菜单'}, 'readonly': 0},
        'custom_field_four': {'name': 'custom_field_four', 'label': '代码分支',
                              'html_type': 'textarea', 'options': [], 'readonly': 0},
        'custom_field_five': {'name': 'custom_field_five', 'label': '实际工时（人/日）',
                              'html_type': 'float', 'options': [], 'readonly': 0},
    }


def story(**values):
    result = {'id': STORY_ID, 'workspace_id': WORKSPACE, 'workitem_type_id': TYPE_ID,
              'status': 'develop', 'custom_field_one': '', 'custom_field_two': '',
              'custom_field_three': '', 'custom_field_four': '', 'custom_field_five': ''}
    result.update(values)
    return result


def node(step='step_design', status='0', **values):
    result = {'step': step, 'status': status, 'workspace_id': WORKSPACE,
              'workitem_id': STORY_ID, 'entity_type': 'story'}
    result.update(values)
    return {'WorkitemStepInfo': result}


class FakeClient:
    """模拟 TAPD 边界并保留真实的业务状态变化。"""
    def __init__(self):
        self.story = story()
        self.fields = fields()
        self.steps = [node(), node('step_test', '-1')]
        self.step_map = [{'name': 'develop', 'label': '开发中', 'steps': [
            {'name': 'step_design', 'label': '完成技术方案'},
            {'name': 'step_test', 'label': '完成测试'}]}]
        self.transitions = [{'StepPrevious': 'step_design', 'StepNext': 'step_design', 'Appendfield': []},
                            {'StepPrevious': 'step_design', 'StepNext': 'step_test', 'Appendfield': []}]
        self.calls = []
        self.ignore_write = False
        self.fail_readback = False

    def get(self, route, params):
        self.calls.append(('GET', route, copy.deepcopy(params)))
        if self.fail_readback and any(method == 'POST' for method, _, _ in self.calls):
            raise tapd.TapdError('读回失败')
        value = {'stories': [{'Story': self.story}], 'stories/get_fields_info': self.fields,
                 'stories/get_story_step_list': self.steps, 'workflows/step_map': self.step_map,
                 'workflows/all_transitions': self.transitions}[route]
        return copy.deepcopy(value)

    def post(self, route, data):
        self.calls.append(('POST', route, copy.deepcopy(data)))
        if not self.ignore_write:
            if route == 'stories':
                self.story.update(data)
            elif route == 'stories/update_story_step_status':
                for item in self.steps:
                    if item['WorkitemStepInfo']['step'] == data['step']:
                        item['WorkitemStepInfo']['status'] = '1'
        return {'Story': copy.deepcopy(self.story)}


class Response(io.BytesIO):
    def __init__(self, value, status=200):
        super().__init__(value if isinstance(value, bytes) else json.dumps(value).encode())
        self.status = status

    def geturl(self):
        return 'https://api.tapd.cn/stories?s=mcp'


class Opener:
    def __init__(self, response=None, error=None):
        self.response = response or {'status': 1, 'data': {'id': STORY_ID}}
        self.error = error
        self.calls = []

    def open(self, request, timeout):
        self.calls.append(request)
        if self.error:
            raise self.error
        return Response(self.response)


class ClientTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(tapd, '应实现 TAPD 客户端模块')
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.credentials = Path(self.tmp.name) / 'credentials.json'
        self.credentials.write_text(json.dumps({'access_token': 'fixture-secret-token'}))

    def test_fixed_host_authentication_and_long_id(self):
        opener = Opener()
        client = tapd.TapdClient(self.credentials, opener=opener)
        self.assertEqual(client.get('stories', {'id': STORY_ID}), {'id': STORY_ID})
        request = opener.calls[0]
        self.assertEqual(urlsplit(request.full_url).netloc, 'api.tapd.cn')
        self.assertEqual(parse_qs(urlsplit(request.full_url).query), {'s': ['mcp'], 'id': [STORY_ID]})
        self.assertEqual(request.get_header('Authorization'), 'Bearer fixture-secret-token')
        self.assertEqual(request.get_header('Via'), 'mcp')

    def test_post_encodes_form_with_mcp_query(self):
        opener = Opener()
        client = tapd.TapdClient(self.credentials, opener=opener)
        client.post('comments', {'entry_id': STORY_ID, 'description': '交付说明'})
        request = opener.calls[0]
        self.assertEqual(request.get_method(), 'POST')
        self.assertEqual(parse_qs(request.data.decode())['entry_id'], [STORY_ID])
        self.assertEqual(parse_qs(urlsplit(request.full_url).query), {'s': ['mcp']})

    def test_route_allowlist_rejects_urls_and_unrelated_writes(self):
        opener = Opener()
        client = tapd.TapdClient(self.credentials, opener=opener)
        for route in ['https://evil.invalid', '//evil.invalid', 'stories/../users', 'stories?x=1', 'users/info']:
            with self.subTest(route=route), self.assertRaises(tapd.TapdError):
                client.post(route, {})
        self.assertEqual(opener.calls, [])

    def test_confirmed_author_and_comment_read_routes_are_allowed(self):
        opener = Opener()
        client = tapd.TapdClient(self.credentials, opener=opener)
        client.get('users/info', {})
        client.get('comments', {})
        self.assertEqual(len(opener.calls), 2)

    def test_api_rejection_does_not_expose_response(self):
        for method in ['get', 'post']:
            opener = Opener({'status': 0, 'info': 'fixture-secret-token private@example.invalid', 'data': None})
            with self.subTest(method=method), self.assertRaises(tapd.TapdError) as raised:
                getattr(tapd.TapdClient(self.credentials, opener=opener), method)('stories', {})
            self.assertFalse(raised.exception.uncertain)
            self.assertNotIn('fixture-secret-token', str(raised.exception))
            self.assertNotIn('private@example.invalid', str(raised.exception))

    def test_post_timeout_is_uncertain_without_retry_or_sensitive_error(self):
        opener = Opener(error=URLError('fixture-secret-token'))
        with self.assertRaises(tapd.TapdError) as raised:
            tapd.TapdClient(self.credentials, opener=opener).post('stories', {})
        self.assertTrue(raised.exception.uncertain)
        self.assertEqual(len(opener.calls), 1)
        self.assertNotIn('fixture-secret-token', str(raised.exception))

    def test_incomplete_http_response_is_uncertain_and_redacted(self):
        opener = Opener(error=IncompleteRead(b'fixture-secret-token', 200))
        with self.assertRaises(tapd.TapdError) as raised:
            tapd.TapdClient(self.credentials, opener=opener).post('stories', {})
        self.assertTrue(raised.exception.uncertain)
        self.assertNotIn('fixture-secret-token', str(raised.exception))
        self.assertEqual(len(opener.calls), 1)

    def test_diagnostic_traceback_omits_original_sensitive_error(self):
        import traceback
        opener = Opener(error=URLError('fixture-secret-token'))
        try:
            tapd.TapdClient(self.credentials, opener=opener).post('stories', {})
        except tapd.TapdError:
            self.assertNotIn('fixture-secret-token', traceback.format_exc())
        else:
            self.fail('必须返回脱敏后的错误')

    def test_http_error_classification_and_sanitization(self):
        for status, uncertain in [(400, False), (403, False), (500, True), (302, True)]:
            opener = Opener(error=HTTPError('https://api.tapd.cn', status, 'fixture-secret-token', {}, None))
            with self.subTest(status=status), self.assertRaises(tapd.TapdError) as raised:
                tapd.TapdClient(self.credentials, opener=opener).post('stories', {})
            self.assertEqual(raised.exception.uncertain, uncertain)
            self.assertNotIn('fixture-secret-token', str(raised.exception))

    def test_malformed_post_response_is_uncertain(self):
        for response in [b'fixture-secret-token', {'data': {}}]:
            with self.subTest(response=response), self.assertRaises(tapd.TapdError) as raised:
                tapd.TapdClient(self.credentials, opener=Opener(response)).post('stories', {})
            self.assertTrue(raised.exception.uncertain)
            self.assertNotIn('fixture-secret-token', str(raised.exception))

    def test_default_transport_blocks_redirect_before_following(self):
        with patch.object(tapd, 'build_opener') as factory:
            tapd.TapdClient(self.credentials)
        handlers = factory.call_args.args
        redirect = next(item for item in handlers if isinstance(item, tapd.HTTPRedirectHandler))
        request = type('Request', (), {'get_method': lambda self: 'GET'})()
        with self.assertRaises(HTTPError) as raised:
            redirect.redirect_request(request, None, 302, '跳转', {}, 'https://evil.invalid')
        raised.exception.close()

    def test_invalid_credentials_are_sanitized(self):
        self.credentials.write_text('fixture-secret-token')
        with self.assertRaises(tapd.TapdError) as raised:
            tapd.TapdClient(self.credentials, opener=Opener())
        self.assertNotIn('fixture-secret-token', str(raised.exception))


class FieldTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(tapd, '应实现 TAPD 客户端模块')
        self.client = FakeClient()

    def test_plan_dynamically_maps_text_and_explicit_hours(self):
        plan = tapd.plan_fields(fields(), story(), {'代码分支': 'feature-delivery', '实际工时（人/日）': 0.5})
        self.assertEqual(plan, {'updates': {'custom_field_four': 'feature-delivery', 'custom_field_five': '0.5'},
                                'expected': {'custom_field_four': '', 'custom_field_five': ''}, 'conflicts': {}})

    def test_plan_does_not_invent_hours(self):
        self.assertEqual(tapd.plan_fields(fields(), story(), {})['updates'], {})

    def test_conflicting_nonempty_value_is_not_overwritten(self):
        plan = tapd.plan_fields(fields(), story(custom_field_four='feature-existing'), {'代码分支': 'feature-new'})
        self.assertEqual(plan['updates'], {})
        self.assertIn('代码分支', plan['conflicts'])

    def test_matching_existing_text_and_numeric_values_are_noops(self):
        plan = tapd.plan_fields(fields(), story(custom_field_four='feature-current', custom_field_five='1.00'),
                                {'代码分支': 'feature-current', '实际工时（人/日）': 1})
        self.assertEqual(plan, {'updates': {}, 'expected': {}, 'conflicts': {}})

    def test_readonly_unknown_and_wrong_type_are_conflicts(self):
        metadata = fields()
        metadata['custom_field_four']['readonly'] = '1'
        metadata['custom_field_five']['html_type'] = 'textarea'
        plan = tapd.plan_fields(metadata, story(), {'代码分支': 'feature-new', '实际工时（人/日）': 1, '状态': '完成'})
        self.assertEqual(set(plan['conflicts']), {'代码分支', '实际工时（人/日）', '状态'})
        self.assertEqual(plan['updates'], {})

    def test_hours_require_finite_explicit_nonnegative_number(self):
        for value in [True, None, '', 'NaN', float('inf'), -1, '1人日', {}, []]:
            with self.subTest(value=value):
                plan = tapd.plan_fields(fields(), story(), {'实际工时（人/日）': value})
                self.assertIn('实际工时（人/日）', plan['conflicts'])
                self.assertEqual(plan['updates'], {})

    def test_invalid_multi_options_are_conflicts(self):
        for value in [['不存在'], ['后端|前端'], '后端', []]:
            with self.subTest(value=value):
                plan = tapd.plan_fields(fields(), story(), {'涉及端口': value})
                self.assertIn('涉及端口', plan['conflicts'])

    def test_multi_select_maps_labels_and_serializes_internal_values(self):
        plan = tapd.plan_fields(fields(), story(), {'涉及端口': ['后端', '前端'], '发布关联操作': ['SQL', '菜单']})
        self.assertEqual(plan['updates'], {'custom_field_one': 'server|web', 'custom_field_three': 'SQL|菜单'})
        self.assertEqual(plan['conflicts'], {})

    def test_multi_select_same_set_in_different_order_is_noop(self):
        plan = tapd.plan_fields(fields(), story(custom_field_one='web|server'), {'涉及端口': ['后端', '前端']})
        self.assertEqual(plan, {'updates': {}, 'expected': {}, 'conflicts': {}})

    def test_duplicate_field_label_is_not_guessed(self):
        metadata = fields()
        metadata['custom_field_other'] = dict(metadata['custom_field_four'], name='custom_field_other')
        plan = tapd.plan_fields(metadata, story(), {'代码分支': 'feature-new'})
        self.assertIn('代码分支', plan['conflicts'])

    def test_preview_never_posts(self):
        plan = tapd.plan_fields(fields(), self.client.story, {'代码分支': 'feature-new'})
        result = tapd.update_fields(self.client, WORKSPACE, STORY_ID, plan)
        self.assertEqual(result['status'], 'planned')
        self.assertFalse(any(method == 'POST' for method, _, _ in self.client.calls))

    def test_write_rechecks_old_values_then_readback(self):
        plan = tapd.plan_fields(fields(), self.client.story, {'代码分支': 'feature-new'})
        checkpoints = []
        result = tapd.update_fields(self.client, WORKSPACE, STORY_ID, plan, execute=True,
                                    before_write=lambda: checkpoints.append(len(self.client.calls)))
        self.assertEqual(result['status'], 'verified')
        self.assertEqual(checkpoints, [2])
        self.assertEqual([call[0] for call in self.client.calls], ['GET', 'GET', 'POST', 'GET'])

    def test_concurrent_edit_stops_before_callback_and_post(self):
        plan = tapd.plan_fields(fields(), self.client.story, {'代码分支': 'feature-new'})
        self.client.story['custom_field_four'] = 'feature-concurrent'
        result = tapd.update_fields(self.client, WORKSPACE, STORY_ID, plan, execute=True,
                                    before_write=lambda: self.fail('冲突后不得调用写入回调'))
        self.assertEqual(result['status'], 'conflict')
        self.assertFalse(any(method == 'POST' for method, _, _ in self.client.calls))

    def test_ignored_update_cannot_be_verified(self):
        plan = tapd.plan_fields(fields(), self.client.story, {'代码分支': 'feature-new'})
        self.client.ignore_write = True
        with self.assertRaises(tapd.TapdError) as raised:
            tapd.update_fields(self.client, WORKSPACE, STORY_ID, plan, execute=True, before_write=lambda: None)
        self.assertTrue(raised.exception.uncertain)

    def test_write_rechecks_metadata_permissions(self):
        plan = tapd.plan_fields(fields(), self.client.story, {'代码分支': 'feature-new'})
        self.client.fields['custom_field_four']['readonly'] = 1
        result = tapd.update_fields(self.client, WORKSPACE, STORY_ID, plan, execute=True,
                                    before_write=lambda: self.fail('字段变成只读后不能写入'))
        self.assertEqual(result['status'], 'conflict')
        self.assertFalse(any(method == 'POST' for method, _, _ in self.client.calls))

    def test_readback_accepts_numeric_and_multi_value_normalization(self):
        plan = tapd.plan_fields(fields(), self.client.story,
                                {'实际工时（人/日）': 0.5, '涉及端口': ['后端', '前端']})
        post = self.client.post
        def normalized_post(route, data):
            result = post(route, data)
            self.client.story['custom_field_five'] = '0.50'
            self.client.story['custom_field_one'] = 'web|server'
            return result
        self.client.post = normalized_post
        result = tapd.update_fields(self.client, WORKSPACE, STORY_ID, plan, execute=True, before_write=lambda: None)
        self.assertEqual(result['status'], 'verified')

    def test_post_readback_failure_is_uncertain(self):
        plan = tapd.plan_fields(fields(), self.client.story, {'代码分支': 'feature-new'})
        self.client.fail_readback = True
        with self.assertRaises(tapd.TapdError) as raised:
            tapd.update_fields(self.client, WORKSPACE, STORY_ID, plan, execute=True, before_write=lambda: None)
        self.assertTrue(raised.exception.uncertain)

    def test_missing_write_checkpoint_blocks_mutation(self):
        plan = tapd.plan_fields(fields(), self.client.story, {'代码分支': 'feature-new'})
        with self.assertRaises(tapd.TapdError):
            tapd.update_fields(self.client, WORKSPACE, STORY_ID, plan, execute=True)
        self.assertFalse(any(method == 'POST' for method, _, _ in self.client.calls))

    def test_failed_checkpoint_blocks_mutation(self):
        plan = tapd.plan_fields(fields(), self.client.story, {'代码分支': 'feature-new'})
        def reject():
            raise OSError('本地记录写入失败')
        with self.assertRaises(OSError):
            tapd.update_fields(self.client, WORKSPACE, STORY_ID, plan, execute=True, before_write=reject)
        self.assertFalse(any(method == 'POST' for method, _, _ in self.client.calls))

    def test_forged_plan_cannot_update_status(self):
        plan = {'updates': {'status': 'done'}, 'expected': {'status': 'develop'}, 'conflicts': {}}
        with self.assertRaises(tapd.TapdError):
            tapd.update_fields(self.client, WORKSPACE, STORY_ID, plan, execute=True, before_write=lambda: None)

    def test_forged_plan_cannot_update_unapproved_custom_field(self):
        self.client.story['custom_field_extra'] = ''
        self.client.fields['custom_field_extra'] = {'name': 'custom_field_extra', 'label': '其它字段',
                                                  'html_type': 'textarea', 'readonly': 0}
        plan = {'updates': {'custom_field_extra': '新值'}, 'expected': {'custom_field_extra': ''}, 'conflicts': {}}
        result = tapd.update_fields(self.client, WORKSPACE, STORY_ID, plan, execute=True, before_write=lambda: None)
        self.assertEqual(result['status'], 'conflict')
        self.assertFalse(any(method == 'POST' for method, _, _ in self.client.calls))


class StepTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(tapd, '应实现 TAPD 客户端模块')
        self.client = FakeClient()

    def test_inspect_checks_story_and_node_identity(self):
        info = tapd.inspect_story(self.client, WORKSPACE, STORY_ID)
        self.assertEqual(set(info), {'story', 'fields', 'steps', 'step_map', 'transitions'})
        self.assertEqual(info['story']['id'], STORY_ID)
        workflow_params = next(params for _, route, params in self.client.calls if route == 'workflows/step_map')
        self.assertEqual(workflow_params, {'workspace_id': WORKSPACE, 'system': 'story', 'workitem_type_id': TYPE_ID})
        self.client.steps[0]['WorkitemStepInfo']['workitem_id'] = '1123456789000000000'
        with self.assertRaises(tapd.TapdError):
            tapd.inspect_story(self.client, WORKSPACE, STORY_ID)

    def test_observed_auto_creation_status_can_be_inspected_but_not_completed(self):
        self.client.steps.insert(0, node('step_begin', '90'))
        self.client.step_map.insert(0, {'name': 'initial', 'label': '创建中', 'steps': [
            {'name': 'step_begin', 'label': '创建'}]})
        info = tapd.inspect_story(self.client, WORKSPACE, STORY_ID)
        self.assertEqual(info['steps'][0]['WorkitemStepInfo']['status'], '90')
        result = tapd.complete_step(self.client, WORKSPACE, STORY_ID, 'step_begin', '证据')
        self.assertEqual(result['status'], 'conflict')

    def test_story_identity_mismatch_is_rejected(self):
        for key in ['workspace_id', 'id']:
            with self.subTest(key=key):
                self.client.story = story(**{key: '9'})
                with self.assertRaises(tapd.TapdError):
                    tapd.inspect_story(self.client, WORKSPACE, STORY_ID)

    def test_invalid_id_type_does_not_round_or_query(self):
        for invalid in [float(STORY_ID), True, '123,456', 'LIKE<123>', '']:
            with self.subTest(invalid=invalid), self.assertRaises(tapd.TapdError):
                tapd.inspect_story(self.client, WORKSPACE, invalid)
        self.assertEqual(self.client.calls, [])

    def test_preview_matches_unique_chinese_label(self):
        result = tapd.complete_step(self.client, WORKSPACE, STORY_ID, '完成技术方案', '方案已评审')
        self.assertEqual(result['status'], 'planned')
        self.assertEqual(result['step'], 'step_design')
        self.assertFalse(any(method == 'POST' for method, _, _ in self.client.calls))

    def test_complete_only_named_in_progress_node_and_readback(self):
        checkpoints = []
        result = tapd.complete_step(self.client, WORKSPACE, STORY_ID, 'step_design', '方案已评审',
                                    execute=True, before_write=lambda: checkpoints.append('saved'))
        self.assertEqual(result['status'], 'verified')
        posts = [call for call in self.client.calls if call[0] == 'POST']
        self.assertEqual(posts, [('POST', 'stories/update_story_step_status',
                                 {'workspace_id': WORKSPACE, 'story_id': STORY_ID, 'step': 'step_design'})])
        self.assertEqual(self.client.steps[1]['WorkitemStepInfo']['status'], '-1')
        self.assertEqual(checkpoints, ['saved'])

    def test_not_started_and_completed_nodes_are_rejected(self):
        for status in ['-1', '1']:
            self.client.steps[0]['WorkitemStepInfo']['status'] = status
            result = tapd.complete_step(self.client, WORKSPACE, STORY_ID, 'step_design', '证据')
            self.assertEqual(result['status'], 'conflict')

    def test_empty_evidence_and_ambiguous_label_are_rejected(self):
        with self.assertRaises(tapd.TapdError):
            tapd.complete_step(self.client, WORKSPACE, STORY_ID, 'step_design', ' ')
        self.client.step_map[0]['steps'][1]['label'] = '完成技术方案'
        result = tapd.complete_step(self.client, WORKSPACE, STORY_ID, '完成技术方案', '证据')
        self.assertEqual(result['status'], 'conflict')

    def test_required_fields_on_self_and_outgoing_transitions_block(self):
        for index in [0, 1]:
            with self.subTest(index=index):
                self.client.transitions[index]['Appendfield'] = [
                    {'DBModel': 'Story', 'FieldName': 'custom_field_four', 'Notnull': 'yes'}]
                result = tapd.complete_step(self.client, WORKSPACE, STORY_ID, 'step_design', '证据')
                self.assertEqual(result['status'], 'conflict')
                self.assertIn('代码分支', str(result))

    def test_unknown_required_rule_blocks_conservatively(self):
        self.client.transitions[0]['Appendfield'] = [{'DBModel': 'External', 'FieldName': 'x', 'Notnull': 'yes'}]
        result = tapd.complete_step(self.client, WORKSPACE, STORY_ID, 'step_design', '证据')
        self.assertEqual(result['status'], 'conflict')

    def test_dynamic_linkage_rules_are_not_assumed_optional(self):
        self.client.transitions[0]['Appendfield'] = [
            {'DBModel': 'Story', 'FieldName': 'custom_field_four', 'Notnull': 'no',
             'LinkageRules': [{'condition': 'unknown'}]}]
        result = tapd.complete_step(self.client, WORKSPACE, STORY_ID, 'step_design', '证据')
        self.assertEqual(result['status'], 'conflict')

    def test_filled_required_field_allows_preview(self):
        self.client.story['custom_field_four'] = 'feature-delivery'
        self.client.transitions[0]['Appendfield'] = [
            {'DBModel': 'Story', 'FieldName': 'custom_field_four', 'Notnull': 'yes'}]
        result = tapd.complete_step(self.client, WORKSPACE, STORY_ID, 'step_design', '证据')
        self.assertEqual(result['status'], 'planned')

    def test_ignored_completion_is_not_verified(self):
        self.client.ignore_write = True
        with self.assertRaises(tapd.TapdError) as raised:
            tapd.complete_step(self.client, WORKSPACE, STORY_ID, 'step_design', '证据',
                               execute=True, before_write=lambda: None)
        self.assertTrue(raised.exception.uncertain)

    def test_completion_readback_failure_is_uncertain(self):
        self.client.fail_readback = True
        with self.assertRaises(tapd.TapdError) as raised:
            tapd.complete_step(self.client, WORKSPACE, STORY_ID, 'step_design', '证据',
                               execute=True, before_write=lambda: None)
        self.assertTrue(raised.exception.uncertain)

    def test_missing_transition_information_blocks(self):
        self.client.transitions = []
        result = tapd.complete_step(self.client, WORKSPACE, STORY_ID, 'step_design', '证据')
        self.assertEqual(result['status'], 'conflict')

class ModuleTest(unittest.TestCase):
    def test_client_module_exists(self):
        self.assertIsNotNone(importlib.util.find_spec('tapd_client'), '应实现 TAPD 客户端模块')


if __name__ == '__main__':
    unittest.main()
