"""TAPD 交付同步的离线行为验证，不发送真实请求。"""
import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import tapd_handoff
from publisher import State
from shared_publisher import SharedPublisher
from test_shared_publisher import SharedPublisherTests

WS = '44667425'
SID = '1144667425001026696'
URL = 'https://www.tapd.cn/tapd_fe/' + WS + '/story/detail/' + SID


class FakeTapd:
    def __init__(self):
        self.calls = []
        self.comments = []
        self.fail = False
        self.lose_response = False
        self.story = {'id': SID, 'workspace_id': WS, 'workitem_type_id': '1', 'custom_field_20': '', 'custom_field_19': ''}

    def get(self, route, params=None):
        self.calls.append(('GET', route, params))
        if route == 'stories':
            return [{'Story': dict(self.story)}]
        if route == 'stories/get_fields_info':
            return {'custom_field_20': {'name': 'custom_field_20', 'label': '代码分支',
                                      'html_type': 'textarea', 'readonly': 0, 'options': []},
                    'custom_field_19': {'name': 'custom_field_19', 'label': '发布关联操作',
                                       'html_type': 'multi_select', 'readonly': 0,
                                       'options': {'需要执行SQL': '需要执行SQL', '需配置菜单': '需配置菜单'}}}
        if route == 'users/info':
            return {'nick': 'tester'}
        if route == 'comments':
            return [{'Comment': c} for c in self.comments] if params.get('page', 1) == 1 else []
        return []

    def post(self, route, data):
        self.calls.append(('POST', route, copy.deepcopy(data)))
        if route == 'comments':
            if self.fail:
                raise tapd_handoff.TapdError('模拟超时', uncertain=True)
            row = dict(data, id=str(len(self.comments) + 1))
            self.comments.append(row)
            if self.lose_response:
                raise tapd_handoff.TapdError('模拟响应丢失', uncertain=True)
            return {'Comment': row}
        if route == 'stories':
            self.story.update(data)
            return {'Story': dict(self.story)}
        raise AssertionError(route)


class HandoffTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.manifest = {'workspace_id': WS, 'story_id': SID, 'summary': '交接',
                         'verification': '本地验证通过，QA待验收', 'fields': {'代码分支': 'api:feature-demo'},
                         'sql': [{'sql': 'ALTER TABLE x ADD INDEX i (a);', 'environment': 'QA',
                                  'status': '待执行', 'notes': '执行前核对索引'}],
                         'menus': [{'name': '测试菜单', 'parent': '营销', 'route': '/demo',
                                    'permissions': '按现有角色', 'status': '待配置'}]}
        self.path = self.root / 'handoff.json'
        self.path.write_text(json.dumps(self.manifest))
        self.client = FakeTapd()
        self.state = State(self.root / 'state.json')
        self.doc = {'docid': 'abc', 'url': 'https://doc.weixin.qq.com/smartpage/abc', 'title': '测试交接'}

    def prepare(self, url=URL):
        return tapd_handoff.prepare({'enabled': True}, url, self.path, client=self.client)

    def sync(self, digest='hash'):
        item = self.prepare()
        with self.state.locked(write=True):
            return item.sync(self.state, self.doc, digest)

    def test_first_unchanged_and_new_version(self):
        self.assertEqual(self.sync()['status'], 'verified')
        self.assertEqual(self.sync()['status'], 'verified')
        self.assertEqual(len(self.client.comments), 1)
        self.sync('changed')
        self.assertEqual(len(self.client.comments), 2)
        text = self.client.comments[0]['description']
        for value in ('ALTER TABLE', '测试菜单', '待执行', self.doc['url']):
            self.assertIn(value, text)
        self.assertNotIn('update_story_step_status', str(self.client.calls))

    def test_unknown_not_found_does_not_retry_post(self):
        self.client.fail = True
        self.assertEqual(self.sync()['status'], 'unknown')
        self.client.fail = False
        self.assertEqual(self.sync()['status'], 'unknown')
        self.assertEqual(len([c for c in self.client.calls if c[:2] == ('POST', 'comments')]), 1)

    def test_lost_response_reconciles_existing_comment(self):
        self.client.lose_response = True
        self.sync()
        self.client.lose_response = False
        self.assertEqual(self.sync()['status'], 'verified')
        self.assertEqual(len(self.client.comments), 1)

    def test_remote_marker_cannot_match_another_story(self):
        self.sync()
        self.client.comments[0]['entry_id'] = '1144667425001000000'
        with self.state.locked(write=True):
            for e in self.state.data['tapd_handoffs'].values():
                e['comment_status'] = 'unknown'
            self.state.save()
        self.assertEqual(self.sync()['status'], 'unknown')

    def test_sending_is_saved_before_post(self):
        original = self.client.post
        def check(route, data):
            if route == 'comments':
                saved = json.loads(self.state.path.read_text())
                self.assertEqual(next(iter(saved['tapd_handoffs'].values()))['comment_status'], 'sending')
            return original(route, data)
        self.client.post = check
        self.sync()

    def test_wrong_object_and_bad_sql_fail_before_remote_write(self):
        self.manifest['story_id'] = '1144667425001000000'
        self.path.write_text(json.dumps(self.manifest))
        with self.assertRaises(tapd_handoff.TapdError):
            self.prepare()
        self.assertFalse(any(c[0] == 'POST' for c in self.client.calls))

    def test_preview_link_resolves_same_identity(self):
        self.prepare('https://www.tapd.cn/tapd_fe/my/work?dialog_preview_id=story_' + SID)
        self.assertFalse(any(c[0] == 'POST' for c in self.client.calls))

    def test_empty_or_unrecognised_manifest_rejected(self):
        for value in ({}, dict(self.manifest, sql=[{}]), dict(self.manifest, surprise='ignored')):
            self.path.write_text(json.dumps(value))
            with self.assertRaises(tapd_handoff.TapdError):
                self.prepare()

    def test_comment_html_is_escaped(self):
        self.manifest['summary'] = '<img src=x onerror=alert(1)> @someone'
        self.path.write_text(json.dumps(self.manifest))
        self.sync()
        self.assertNotIn('<img', self.client.comments[0]['description'])
        self.assertNotIn('@someone', self.client.comments[0]['description'])

    def test_existing_field_conflict_is_not_reported_as_complete(self):
        self.client.story['custom_field_20'] = 'api:someone-else'
        result = self.sync()
        self.assertEqual(result['status'], 'partial')
        self.assertTrue(result['field_conflicts'])
        self.assertEqual(self.client.story['custom_field_20'], 'api:someone-else')
        self.assertEqual(result['comment'], 'verified')

    def test_cleared_conflict_resumes_same_delivery_without_duplicate_comment(self):
        self.client.story['custom_field_20'] = 'api:someone-else'
        self.assertEqual(self.sync()['status'], 'partial')
        self.client.story['custom_field_20'] = ''
        result = self.sync()
        self.assertEqual(result['status'], 'verified')
        self.assertEqual(result['field_conflicts'], {})
        self.assertEqual(self.client.story['custom_field_20'], 'api:feature-demo')
        self.assertEqual(len(self.client.comments), 1)

    def test_unknown_fields_keep_original_plan_and_do_not_retry_write(self):
        original = self.client.post
        def lose_field_write(route, data):
            if route == 'stories':
                self.client.calls.append(('POST', route, copy.deepcopy(data)))
                raise tapd_handoff.TapdError('模拟字段超时', uncertain=True)
            return original(route, data)
        self.client.post = lose_field_write
        self.assertEqual(self.sync()['status'], 'unknown')
        saved_plan = copy.deepcopy(next(iter(self.state.data['tapd_handoffs'].values()))['field_plan'])
        self.client.post = original
        self.client.story['custom_field_20'] = 'api:someone-else'
        self.assertEqual(self.sync()['status'], 'unknown')
        self.assertEqual(next(iter(self.state.data['tapd_handoffs'].values()))['field_plan'], saved_plan)
        self.assertEqual(len([c for c in self.client.calls if c[:2] == ('POST', 'stories')]), 1)


class PublisherHandoffTests(SharedPublisherTests):
    def setup_handoff(self):
        self.config['tapd_sync'] = {'enabled': True, 'credentials_path': '/not-read'}
        path = self.root / 'tapd.json'
        path.write_text(json.dumps({'workspace_id': WS, 'story_id': SID, 'summary': '交付',
            'verification': '本地已验证', 'fields': {}, 'sql': [], 'menus': []}))
        return path

    def test_success_and_alias_reuse_single_document_and_comment(self):
        path = self.setup_handoff()
        client = FakeTapd()
        with patch.object(tapd_handoff, 'TapdClient', return_value=client):
            first = self.app.publish('api', URL, self.source, 'design', execute=True, tapd_handoff_file=path)
            second = self.app.publish('api', 'https://www.tapd.cn/tapd_fe/my/work?dialog_preview_id=story_' + SID,
                                      self.source, 'design', execute=True, tapd_handoff_file=path)
        self.assertEqual(first['url'], second['url'])
        self.assertEqual(first['tapd_sync']['status'], 'verified')
        self.assertEqual(len(client.comments), 1)

    def test_switch_primary_project_reuses_body_and_comment(self):
        path = self.setup_handoff()
        client = FakeTapd()
        with patch.object(tapd_handoff, 'TapdClient', return_value=client):
            first = self.app.publish('api', URL, self.source, 'design', execute=True,
                                     also_projects=['admin'], tapd_handoff_file=path)
            second = self.app.publish('admin', URL, self.source, 'design', execute=True,
                                      also_projects=['api'], tapd_handoff_file=path)
        self.assertEqual(first['url'], second['url'])
        self.assertEqual(len(client.comments), 1)

    def test_interleaved_primary_projects_recheck_mapping_under_write_lock(self):
        path = self.setup_handoff()
        client = FakeTapd()
        second_app = SharedPublisher(self.config, cli=self.cli)
        original = self.app.prepare
        other_results = []
        def interleave(source, directory):
            other_results.append(second_app.publish('admin', URL, self.source, 'design', execute=True,
                also_projects=['api'], tapd_handoff_file=path))
            return original(source, directory)
        with patch.object(tapd_handoff, 'TapdClient', return_value=client), \
                patch.object(self.app, 'prepare', side_effect=interleave):
            result = self.app.publish('api', URL, self.source, 'design', execute=True,
                                      also_projects=['admin'], tapd_handoff_file=path)
        self.assertEqual(result['url'], other_results[0]['url'])
        self.assertEqual(len(client.comments), 1)

    def test_dry_run_preserves_tapd_url_and_separates_sync_preview(self):
        disabled = self.publish(execute=False)
        self.assertIsInstance(disabled['tapd'], str)
        self.assertEqual(disabled['tapd_sync']['status'], 'disabled')
        path = self.setup_handoff()
        with patch.object(tapd_handoff, 'TapdClient', return_value=FakeTapd()):
            enabled = self.app.publish('api', URL, self.source, 'design', tapd_handoff_file=path)
        self.assertEqual(enabled['tapd'], URL)
        self.assertEqual(enabled['tapd_sync']['status'], 'planned')

    def test_wecom_failure_does_not_write_tapd(self):
        path = self.setup_handoff()
        client = FakeTapd()
        self.cli.fail_blocks = True
        with patch.object(tapd_handoff, 'TapdClient', return_value=client):
            with self.assertRaises(Exception):
                self.app.publish('api', URL, self.source, 'design', execute=True, tapd_handoff_file=path)
        self.assertFalse(any(call[0] == 'POST' for call in client.calls))

    def test_enabled_without_manifest_fails_before_wecom_write(self):
        self.config['tapd_sync'] = {'enabled': True, 'credentials_path': '/not-read'}
        with self.assertRaises(Exception):
            self.publish()
        self.assertEqual(self.cli.calls, [])

    def test_disabled_keeps_existing_publish_behavior(self):
        result = self.publish()
        self.assertEqual(result['tapd_sync']['status'], 'disabled')
