"""TAPD 关联人员解析的离线契约，不调用真实服务或写入业务状态。"""
import contextlib
import copy
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import group_notify
import publisher
import test_publisher

try:
    import tapd_mentions as mentions
except ImportError:
    mentions = None


WORKSPACE = '44667425'
ROOT = '1144667425001026606'
TEST = '1144667425001026684'
FRONT = '1144667425001026690'
BACK = '1144667425001026696'
ANCESTOR = '1144667425001000001'
NAMES = ['Hayden张海坤', 'Guofeng郭丰', 'Alante肖世利童']
USERIDS = ['hayden', 'guofeng', 'alante']
URL = 'https://www.tapd.cn/tapd_fe/' + WORKSPACE + '/story/detail/' + BACK
PREVIEW = 'https://www.tapd.cn/tapd_fe/my/work?dialog_preview_id=story_' + BACK


def story(story_id, parent='0', name='需求', **values):
    result = {'id': story_id, 'workspace_id': WORKSPACE, 'parent_id': parent,
              'children_id': '|', 'name': name, 'developer': '', 'owner': '',
              'custom_field_one': '', 'custom_field_two': '', 'custom_field_three': ''}
    result.update(values)
    return result


class FakeTapd:
    def __init__(self):
        self.calls = []
        self.error = None
        self.stories = {
            ROOT: story(ROOT, children_id='||' + '|'.join([TEST, FRONT, BACK]),
                        developer=';'.join(NAMES[1:]), owner='产品负责人', custom_field_three=NAMES[0]),
            TEST: story(TEST, ROOT, '【测试任务】验收', developer=NAMES[0]),
            FRONT: story(FRONT, ROOT, '【前端开发任务】实现', developer=NAMES[1]),
            BACK: story(BACK, ROOT, '【后端任务】实现', developer=NAMES[2]),
        }
        self.fields = {name: {'name': name, 'label': label, 'html_type': 'user_chooser'} for name, label in (
            ('custom_field_one', '前端人员'), ('custom_field_two', '后端人员'),
            ('custom_field_three', '测试人员'), ('developer', '开发人员'), ('owner', '处理人'))}

    def get(self, route, params):
        self.calls.append((route, copy.deepcopy(params)))
        if self.error:
            raise self.error
        if route == 'stories':
            return [{'Story': copy.deepcopy(self.stories[params['id']])}]
        if route == 'stories/get_fields_info':
            return copy.deepcopy(self.fields)
        raise AssertionError('只允许读取需求与字段')

    def post(self, *args, **kwargs):
        raise AssertionError('人员解析禁止 TAPD 写入')


class FakeCli:
    def __init__(self):
        self.calls = []
        self.error = None
        self.users = [{'userid': userid, 'name': name} for name, userid in zip(NAMES, USERIDS)]

    def call(self, route, data):
        if route != ('contact', 'users', 'search'):
            raise AssertionError('人员解析禁止企微写入')
        self.calls.append((route, copy.deepcopy(data)))
        if self.error:
            raise self.error
        return {'data': json.dumps({'users': self.users}, ensure_ascii=False)}


class MentionTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(mentions, '应实现独立的只读 TAPD 人员解析模块')
        self.client, self.cli = FakeTapd(), FakeCli()
        self.config = {'tapd_mentions': {'userids': dict(zip(NAMES, USERIDS))}}

    def resolve(self, url=URL, **kwargs):
        return mentions.resolve_mentions(self.config, url, client=self.client, cli=self.cli, **kwargs)

    def test_direct_parent_and_children_collect_roles_without_product_owner(self):
        result = self.resolve()
        self.assertEqual(result['status'], 'read_only')
        self.assertEqual(result['scope']['root_story_id'], ROOT)
        self.assertEqual(set(result['scope']['story_ids']), {ROOT, TEST, FRONT, BACK})
        people = {person['tapd_name']: person for person in result['people']}
        self.assertEqual(set(people), set(NAMES))
        self.assertEqual(people[NAMES[0]]['roles'], ['测试'])
        self.assertEqual(people[NAMES[1]]['roles'], ['前端'])
        self.assertEqual(people[NAMES[2]]['roles'], ['后端'])
        self.assertGreaterEqual(len(people[NAMES[0]]['sources']), 2)
        self.assertEqual(len(result['matched']), 3)
        self.assertEqual(result['unresolved'], [])
        self.assertEqual(self.cli.calls, [])

    def test_parent_ancestor_is_not_traversed(self):
        self.client.stories[ROOT]['parent_id'] = ANCESTOR
        result = self.resolve()
        self.assertEqual(result['status'], 'read_only')
        self.assertNotIn(ANCESTOR, [params.get('id') for _, params in self.client.calls])

    def test_without_parent_current_is_scope_root(self):
        result = self.resolve(URL.replace(BACK, ROOT))
        self.assertEqual(result['scope']['root_story_id'], ROOT)
        self.assertEqual(len(result['scope']['story_ids']), 4)

    def test_standalone_task_prefix_identifies_developer_and_owner_roles(self):
        for title, role in (('【前端开发任务】实现', '前端'), ('【后端任务】实现', '后端'), ('【测试任务】验收', '测试')):
            with self.subTest(title=title):
                self.client.stories[BACK].update(parent_id='0', name=title, owner=NAMES[0])
                result = self.resolve()
                self.assertEqual(result['status'], 'read_only')
                self.assertEqual({p['tapd_name'] for p in result['people']}, {NAMES[0], NAMES[2]})
                self.assertTrue(all(p['roles'] == [role] for p in result['people']))

    def test_preview_requires_explicit_workspace_and_rejects_mismatch(self):
        self.assertEqual(self.resolve(PREVIEW)['status'], 'error')
        self.assertEqual(self.client.calls, [])
        self.assertEqual(self.resolve(workspace='999')['status'], 'error')
        self.assertEqual(self.client.calls, [])
        self.assertEqual(self.resolve(PREVIEW, workspace=WORKSPACE)['status'], 'read_only')

    def test_identity_and_bidirectional_parent_validation(self):
        for target_id, key, value in (
                (BACK, 'workspace_id', '999'), (FRONT, 'workspace_id', '999'),
                (ROOT, 'id', ANCESTOR), (FRONT, 'parent_id', ANCESTOR),
                (BACK, 'parent_id', 'invalid'), (ROOT, 'children_id', '||' + FRONT),
                (ROOT, 'parent_id', BACK), (ROOT, 'parent_id', ROOT),
                (FRONT, 'children_id', '||' + ROOT)):
            with self.subTest(target=target_id, key=key):
                self.client = FakeTapd()
                self.client.stories[target_id][key] = value
                result = self.resolve()
                self.assertEqual(result['status'], 'error')
                self.assertEqual(result['userids'], [])
                self.assertEqual(result['mention_markdown'], '')
                self.assertEqual(self.cli.calls, [])

    def test_children_format_duplicate_cycle_and_limit_are_not_truncated(self):
        invalid = ['||short', FRONT + ',' + BACK, '||' + FRONT + '|',
                   '||' + ROOT, '||' + BACK + '|' + BACK, [], None,
                   '||' + '|'.join(str(1144667425002000000 + i) for i in range(51))]
        for value in invalid:
            with self.subTest(value=value):
                self.client = FakeTapd()
                self.client.stories[ROOT]['children_id'] = value
                self.assertEqual(self.resolve()['status'], 'error')
                self.assertLessEqual(len(self.client.calls), 2)

    def test_unknown_leaf_children_format_is_rejected(self):
        self.client.stories[FRONT]['children_id'] = 'arbitrary'
        self.assertEqual(self.resolve()['status'], 'error')

    def test_dynamic_user_fields_and_multiple_people_preserve_sources(self):
        self.client.fields['custom_field_one']['name'] = 'custom_field_80'
        self.client.stories[ROOT]['custom_field_80'] = NAMES[0] + '; ' + NAMES[2] + ';'
        self.client.stories[ROOT]['custom_field_one'] = '不能使用旧编号'
        self.client.stories[BACK]['custom_field_two'] = NAMES[0]
        result = self.resolve()
        people = {person['tapd_name']: person for person in result['people']}
        self.assertEqual(set(people), set(NAMES))
        self.assertEqual(set(people[NAMES[0]]['roles']), {'前端', '后端', '测试'})
        self.assertTrue(any(source['field'] == 'custom_field_80' for source in people[NAMES[0]]['sources']))

    def test_wrong_field_type_ignored_and_duplicate_labels_rejected(self):
        self.client.fields['custom_field_one']['html_type'] = 'text'
        self.client.stories[ROOT]['custom_field_one'] = '不是真正人员'
        self.assertEqual({p['tapd_name'] for p in self.resolve()['people']}, set(NAMES))
        self.client.fields['other'] = dict(self.client.fields['owner'], name='other')
        self.assertEqual(self.resolve()['status'], 'error')

    def test_unclassified_developer_is_generic_and_only_explicit_child_prefix_uses_owner(self):
        self.client.stories[FRONT].update(name='说明中提及【前端任务】', developer='开发甲;开发乙', owner='产品乙')
        self.client.stories[BACK].update(owner='后端乙')
        self.config['tapd_mentions']['userids'].update({'开发甲': 'dev-a', '开发乙': 'dev-b', '后端乙': 'dev-c'})
        people = {person['tapd_name']: person for person in self.resolve()['people']}
        self.assertEqual(people['开发甲']['roles'], ['开发'])
        self.assertEqual(people['开发乙']['roles'], ['开发'])
        self.assertEqual(people['后端乙']['roles'], ['后端'])
        self.assertNotIn('产品乙', people)

    def test_explicit_mapping_precedes_cache_and_same_userid_is_mentioned_once(self):
        self.config['resolved_members'] = {NAMES[0]: 'old'}
        self.config['tapd_mentions']['userids'][NAMES[1]] = 'alante'
        result = self.resolve(state={'resolved_members': {NAMES[0]: 'other'}})
        self.assertEqual(set(result['userids']), {'hayden', 'alante'})
        self.assertEqual(result['mention_markdown'].count('<@alante>'), 1)
        self.assertEqual(len(result['matched']), 3)

    def test_cache_must_match_full_name_and_conflicts_do_not_query(self):
        self.config = {'resolved_members': {NAMES[0]: 'hayden', '郭丰': 'wrong'}}
        result = self.resolve(state={'resolved_members': {NAMES[0]: 'other'}})
        missing = {p['tapd_name']: p for p in result['unresolved']}
        self.assertEqual(missing[NAMES[0]]['reason'], 'mapping_conflict')
        searched = [name for _, data in self.cli.calls for name in data['keywords']]
        self.assertNotIn(NAMES[0], searched)
        self.assertIn(NAMES[1], searched)
        self.assertNotIn('wrong', result['userids'])

    def test_same_config_and_state_cache_mapping_is_accepted_without_search(self):
        self.config = {'resolved_members': dict(zip(NAMES, USERIDS))}
        result = self.resolve(state={'resolved_members': dict(zip(NAMES, USERIDS))})
        self.assertEqual(result['status'], 'read_only')
        self.assertEqual(self.cli.calls, [])

    def test_search_accepts_unique_exact_name_or_alias_only(self):
        self.config = {}
        self.cli.users = [{'userid': 'hayden', 'name': '其他', 'alias': [NAMES[0]]},
                          {'userid': 'hayden', 'name': NAMES[0]},
                          {'userid': 'guofeng', 'name': '郭丰'},
                          {'userid': 'alante', 'alias': NAMES[2]}]
        result = self.resolve()
        self.assertEqual(result['status'], 'partial')
        self.assertEqual(set(result['userids']), {'hayden', 'alante'})
        self.assertEqual(result['unresolved'][0]['tapd_name'], NAMES[1])
        self.assertEqual(len(self.cli.calls), 1)

    def test_ambiguous_contact_does_not_guess(self):
        self.config = {}
        self.cli.users.append({'userid': 'other', 'name': NAMES[0]})
        result = self.resolve()
        self.assertEqual(result['unresolved'][0]['reason'], 'ambiguous_contact')
        self.assertNotIn('hayden', result['userids'])

    def test_excluded_chinese_suffix_is_not_queried_but_explicit_mapping_works(self):
        self.config = {'member_lookup_excluded': ['郭丰', NAMES[0]]}
        result = self.resolve()
        self.assertEqual({p['tapd_name'] for p in result['unresolved']}, set(NAMES[:2]))
        self.assertTrue(all(p['reason'] == 'lookup_excluded' for p in result['unresolved']))
        self.assertEqual(self.cli.calls[0][1]['keywords'], [NAMES[2]])
        self.config['tapd_mentions'] = {'userids': {NAMES[0]: 'hayden', NAMES[1]: 'guofeng'}}
        self.assertEqual(self.resolve()['status'], 'read_only')

    def test_search_batches_ten_and_never_persists_config_or_state(self):
        self.config = {}
        state = {'resolved_members': {}}
        self.client.stories[ROOT]['developer'] = ';'.join('开发' + str(i) for i in range(21))
        original = copy.deepcopy((self.config, state))
        self.resolve(state=state)
        self.assertGreaterEqual(len(self.cli.calls), 3)
        self.assertTrue(all(len(data['keywords']) <= 10 for _, data in self.cli.calls))
        self.assertEqual((self.config, state), original)

    def test_contact_failure_reports_missing_without_private_exception(self):
        self.config = {}
        self.cli.error = RuntimeError('secret alice@example.com 13800138000 <@all>')
        result = self.resolve()
        self.assertEqual(result['status'], 'partial')
        self.assertEqual({p['reason'] for p in result['unresolved']}, {'lookup_failed'})
        self.assertNotIn('secret', json.dumps(result))
        self.assertNotIn('13800138000', json.dumps(result))
        self.assertNotIn('alice@example.com', json.dumps(result))

    def test_invalid_broadcast_injected_email_or_phone_userids_never_form_mentions(self):
        for userid in ('all', '@all', 'ALL', 'x y', 'x\ny', 'x|y', 'x;y', '<@all>',
                       'x><@all', '', 'person@example.com', 'person@internal',
                       '13800138000', '138-0013-8000', '8613800138000', '86-138-0013-8000', ['bad']):
            with self.subTest(userid=userid):
                self.config['tapd_mentions']['userids'][NAMES[0]] = userid
                result = self.resolve()
                self.assertEqual(result['status'], 'partial')
                self.assertEqual(result['unresolved'][0]['reason'], 'invalid_userid')
                self.assertNotIn('<@all>', result['mention_markdown'])
                self.assertNotIn('person@example.com', json.dumps(result))
                self.assertNotIn('13800138000', json.dumps(result))
                self.assertEqual(self.cli.calls, [])

    def test_private_and_malicious_tapd_names_are_safe_in_output(self):
        self.client.stories[BACK]['developer'] = 'person@example.com;13800138000;<@all>'
        self.config['tapd_mentions']['userids'].update({'person@example.com': 'safe-email-owner',
                                                      '13800138000': 'safe-phone-owner', '<@all>': 'safe-tag-owner'})
        result = self.resolve()
        text = json.dumps(result, ensure_ascii=False)
        self.assertNotIn('person@example.com', text)
        self.assertNotIn('13800138000', text)
        self.assertNotIn('<@all>', text)
        self.assertIn('safe-email-owner', result['userids'])

    def test_formatted_phone_and_internal_email_names_are_redacted(self):
        private = ['person@internal', '+86 138-0013-8000', '138 0013 8000',
                   '+8613800138000', '8613800138000', '+86 138 0013 8000']
        self.client.stories[BACK]['developer'] = ';'.join(private)
        self.config['tapd_mentions']['userids'].update({name: 'safe-owner' for name in private})
        text = json.dumps(self.resolve(), ensure_ascii=False)
        for name in private:
            self.assertNotIn(name, text)

    def test_international_phone_candidates_are_rejected_in_every_userid_source(self):
        phones = ['008613800138000', '447911123456', '00447911123456',
                  '44-7911-123456', '1.415.555.0123', '123_456_7890',
                  '1234567', '123456789012345', '00123456789012345']
        for source in ('explicit', 'cache', 'contact'):
            for phone in phones:
                with self.subTest(source=source, phone=phone):
                    self.config = {'tapd_mentions': {'userids': dict(zip(NAMES[1:], USERIDS[1:]))}}
                    if source == 'explicit':
                        self.config['tapd_mentions']['userids'][NAMES[0]] = phone
                    elif source == 'cache':
                        self.config['resolved_members'] = {NAMES[0]: phone}
                    else:
                        self.cli.users = [{'name': NAMES[0], 'userid': phone}]
                    result = self.resolve()
                    self.assertEqual(result['status'], 'partial')
                    self.assertEqual(result['unresolved'][0]['reason'], 'invalid_userid')
                    self.assertNotIn(phone, json.dumps(result))

    def test_international_phone_candidates_embedded_in_names_are_redacted(self):
        phones = ['008613800138000', '447911123456', '00447911123456', '+44 7911 123456',
                  '+1 (415) 555-0123', '123_456_7890', '1234567', '123456789012345',
                  '00123456789012345']
        for phone in phones:
            with self.subTest(phone=phone):
                name = '开发甲(' + phone + ')'
                self.client.stories[BACK]['developer'] = name
                self.config['tapd_mentions']['userids'][name] = 'safe-owner'
                result = self.resolve()
                self.assertEqual(result['status'], 'read_only')
                self.assertNotIn(phone, json.dumps(result, ensure_ascii=False))
                self.assertEqual(result['scope']['current_story_id'], BACK)

    def test_short_internal_numeric_userids_remain_usable(self):
        for userid in ('12345', '123456', '12-3456'):
            with self.subTest(userid=userid):
                self.config['tapd_mentions']['userids'][NAMES[0]] = userid
                result = self.resolve()
                self.assertEqual(result['status'], 'read_only')
                self.assertIn('<@' + userid + '>', result['mention_markdown'])

    def test_tapd_exception_is_sanitized_and_no_partial_mentions_escape(self):
        self.client.error = RuntimeError('secret person@example.com 13800138000')
        result = self.resolve()
        self.assertEqual(result['status'], 'error')
        self.assertEqual(result['userids'], [])
        self.assertNotIn('secret', json.dumps(result))

    def test_existing_markdown_payload_preserves_real_mentions_in_same_message(self):
        line = self.resolve()['mention_markdown']
        payload = group_notify.markdown_payload('交付完成\n' + line + '\n[正文]({document_url})',
                                                'https://doc.weixin.qq.com/smartpage/test')
        self.assertEqual(payload['msgtype'], 'markdown')
        for userid in USERIDS:
            self.assertEqual(payload['markdown']['content'].count('<@' + userid + '>'), 1)

    def test_mentions_use_existing_durable_notification_and_unchanged_body_never_resends(self):
        line = self.resolve()['mention_markdown']
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, notice = root / 'handoff.md', root / 'notice.md'
            source.write_text('# 交付\n正文已完成\n')
            notice.write_text('**交付完成**\n' + line + '\n[正文]({document_url})')
            state_path = root / 'state.json'
            config = {'state_path': str(state_path), 'projects': {
                name: {'repo_root': str(root), 'maintainers': [], 'readers': []}
                for name in ('api', 'admin', 'mall')}, 'group_notification': {
                    'enabled': True, 'webhook_key_env': 'TAPD_MENTION_TEST_WEBHOOK'}}
            cli, sent = test_publisher.FakeCli(), []
            def send(url, payload):
                events = json.loads(state_path.read_text())['group_notifications']
                self.assertEqual([event['status'] for event in events.values()], ['sending'])
                sent.append(payload)
                return {'errcode': 0}
            with patch.dict(os.environ, {'TAPD_MENTION_TEST_WEBHOOK': 'fixture-key'}):
                first = publisher.Publisher(config, cli=cli, notification_sender=send).publish(
                    'api', URL, source, 'handoff', execute=True, notification_file=notice)
                second = publisher.Publisher(config, cli=cli, notification_sender=send).publish(
                    'api', URL, source, 'handoff', execute=True, notification_file=notice)
            self.assertEqual(first['notification']['status'], 'sent')
            self.assertEqual(second['notification']['status'], 'unchanged')
            self.assertEqual(len(sent), 1)
            self.assertIn(line, sent[0]['markdown']['content'])
            self.assertIn(first['url'], sent[0]['markdown']['content'])
            events = json.loads(state_path.read_text())['group_notifications']
            self.assertEqual(len(events), 1)
            self.assertEqual(next(iter(events.values()))['payload'], sent[0])


class CliTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(mentions, '应实现独立的只读 TAPD 人员解析模块')
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config_path = self.root / 'config.json'
        self.state_path = self.root / 'state.json'
        self.config_path.write_text(json.dumps({'state_path': 'state.json',
            'tapd_sync': {'credentials_path': 'credentials.json'}}))
        self.state_path.write_text(json.dumps({'resolved_members': dict(zip(NAMES, USERIDS))}))

    def run_cli(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = mentions.main(['--config', str(self.config_path), '--tapd', URL],
                                 client=FakeTapd(), cli=FakeCli())
        return code, json.loads(output.getvalue())

    def test_cli_only_reads_relative_state_without_lock_or_file_changes(self):
        before = {path.name: path.read_bytes() for path in self.root.iterdir()}
        with patch('publisher.State.save', side_effect=AssertionError('禁止写状态')):
            code, result = self.run_cli()
        self.assertEqual(code, 0)
        self.assertEqual(result['status'], 'read_only')
        self.assertEqual(before, {path.name: path.read_bytes() for path in self.root.iterdir()})

    def test_missing_state_is_not_created(self):
        self.state_path.unlink()
        code, result = self.run_cli()
        self.assertEqual(code, 0)
        self.assertEqual(result['status'], 'read_only')
        self.assertEqual([path.name for path in self.root.iterdir()], ['config.json'])

    def test_invalid_config_error_never_prints_file_contents(self):
        self.config_path.write_text('person@example.com 13800138000')
        code, result = self.run_cli()
        self.assertEqual(code, 1)
        self.assertEqual(result['status'], 'error')
        self.assertNotIn('person@example.com', json.dumps(result))
        self.assertNotIn('13800138000', json.dumps(result))


if __name__ == '__main__':
    unittest.main()
