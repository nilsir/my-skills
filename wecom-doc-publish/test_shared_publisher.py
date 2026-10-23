"""Shared catalog behavior with two independent author states, no external calls."""
import copy
import json
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import publisher
from test_publisher import FakeCli, TAPD

try:
    from shared_publisher import SharedPublisher
except ImportError:
    SharedPublisher = publisher.Publisher

URL = 'https://doc.weixin.qq.com/smartpage/'


class SharedCli(FakeCli):
    def __init__(self):
        super().__init__()
        self.denied = set()
        self.rule_failures = 0
        self.denied_rules = set()
        self.fail_blocks = False
        self.block_transport = False
        self.after_root_append = None
        for name in ('root', 'api', 'admin', 'mall'):
            self.add(name, '# ' + name + '\n\nColleague prose\n')
        self.docs['root']['content'] += ''.join('- [' + p + '](' + URL + p + ')\n' for p in ('api', 'admin', 'mall'))

    def add(self, docid, content):
        self.docs[docid] = {'name': docid, 'content': content, 'members': {}, 'restricted': False, 'images': []}

    def call(self, route, data):
        if route == ('doc', 'rules', 'update') and (self.rule_failures or data['docid'] in self.denied_rules):
            self.calls.append((route, dict(data)))
            self.rule_failures = max(0, self.rule_failures - 1)
            raise publisher.CliError('rules permission denied', uncertain=False)
        if route == ('smartpage', 'pages', 'get'):
            if data['docid'] in self.denied:
                self.calls.append((route, dict(data)))
                raise publisher.CliError('read permission denied', uncertain=False)
            if data.get('content_type') == 'block':
                self.calls.append((route, dict(data)))
                nodes = []
                for i, line in enumerate(self.docs[data['docid']]['content'].splitlines()):
                    if line.startswith('## '):
                        nodes.append({'id': 'actual-' + str(i), 'type': 'header_2',
                                      'property': {'title': {'text': line[3:]}}, 'parent_id': 'real-page'})
                result = {'pages': [{'page_id': 'page', 'content_file_inner': json.dumps(nodes)}]}
                if self.block_transport:
                    return {'transport_metadata': result, 'response': {'file_path': '/never/open/source/content'}}
                return result
        if route == ('smartpage', 'blocks', 'update'):
            self.calls.append((route, dict(data)))
            if self.fail_blocks:
                raise publisher.CliError('blocks timeout', uncertain=True)
            content = self.docs[data['docid']]['content']
            if data['method'] == 'append':
                content += '\n' + data['mdx'] + '\n'
            else:
                self.assert_insert(data)
                number = int(data['block_id'].removeprefix('actual-'))
                lines = content.splitlines()
                lines.insert(number + 1, data['mdx'])
                content = '\n'.join(lines) + '\n'
            self.docs[data['docid']]['content'] = content
            if data['docid'] == 'root' and self.after_root_append:
                callback, self.after_root_append = self.after_root_append, None
                callback()
            return {'errcode': 0}
        return super().call(route, data)

    @staticmethod
    def assert_insert(data):
        assert data['method'] == 'insertAfter', data
        assert data['block_id'].startswith('actual-'), data


class SharedPublisherTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.source = self.root / 'design.md'
        self.source.write_text('# Source\nOwn content\n')
        self.cli = SharedCli()
        self.config = {'state_path': str(self.root / 'one.json'), 'publisher_label': 'Author One',
                       'projects': {p: {'repo_root': str(self.root), 'maintainers': ['Alice'], 'readers': ['Bob']}
                                    for p in ('api', 'admin', 'mall')},
                       'shared_catalog': {'root': URL + 'root', 'projects': {p: URL + p for p in ('api', 'admin', 'mall')}}}
        self.app = SharedPublisher(self.config, cli=self.cli)

    def publish(self, app=None, execute=True, **kwargs):
        return (app or self.app).publish('api', TAPD, self.source, 'design', execute=execute, **kwargs)

    def existing_requirement(self, docid='req'):
        self.cli.add(docid, '# Existing requirement\n[TAPD](' + TAPD + ')\n\n## API\n\n- [Other author](' + URL + 'other)\n\n## Admin\n\n## Mall\n')
        self.cli.docs['root']['content'] += '- [Requirement](' + URL + docid + ')\n'

    def block_mutations(self):
        return [(r, d) for r, d in self.cli.calls if r == ('smartpage', 'blocks', 'update')]

    def test_two_author_states_reuse_remote_requirement_and_preserve_colleague_links(self):
        self.existing_requirement()
        first = self.publish()
        config = copy.deepcopy(self.config)
        config.update(state_path=str(self.root / 'two.json'), publisher_label='Author Two')
        second = self.publish(SharedPublisher(config, cli=self.cli))
        self.assertNotEqual(first['url'], second['url'])
        self.assertEqual(len(self.cli.docs), 7)
        content = self.cli.docs['req']['content']
        for url in (URL + 'other', first['url'], second['url']):
            self.assertEqual(content.count(url), 1)
        self.assertIn('Author One · design · design.md', content)
        self.assertIn('Author Two · design · design.md', content)
        for name in ('root', 'api', 'admin', 'mall'):
            self.assertIn('Colleague prose', self.cli.docs[name]['content'])
        self.assertTrue(all(d['docid'] not in ('root', 'api', 'admin', 'mall', 'req')
                            for r, d in self.cli.calls if r in (('doc', 'rules', 'update'), ('doc', 'members', 'update'))))
        self.assertTrue(all(d['docid'] not in ('root', 'api', 'admin', 'mall', 'req')
                            for r, d in self.cli.calls if r == ('smartpage', 'pages', 'overwrite')))

    def test_root_html_anchor_reuses_existing_requirement(self):
        self.existing_requirement()
        self.cli.docs['root']['content'] = self.cli.docs['root']['content'].replace(
            '[Requirement](' + URL + 'req)', '<A HREF="https&#58;//doc.weixin.qq.com/smartpage/req">Requirement</A>')
        self.publish()
        self.assertEqual(len(self.cli.docs), 6)
        state = json.loads(Path(self.config['state_path']).read_text())
        self.assertEqual(next(d['docid'] for k, d in state['documents'].items() if k.startswith('requirement:')), 'req')

    def test_known_requirement_html_tapd_anchor_preserves_exact_target_matching(self):
        self.existing_requirement()
        first = self.publish()
        self.cli.docs['req']['content'] = self.cli.docs['req']['content'].replace(
            '[TAPD](' + TAPD + ')', '<a href="' + TAPD.replace(':', '&#58;', 1) + '">TAPD</a>')
        second = self.publish()
        self.assertEqual(first['url'], second['url'])
        self.cli.docs['req']['content'] = self.cli.docs['req']['content'].replace('">TAPD</a>', '?different=1">TAPD</a>')
        with self.assertRaisesRegex(publisher.PublisherError, 'TAPD'):
            self.publish()

    def test_equivalent_html_body_and_catalog_links_do_not_repeat_index_entries(self):
        self.existing_requirement()
        self.publish()
        for docid in ('root', 'api', 'req'):
            self.cli.docs[docid]['content'] = re.sub(
                r'\[([^]]*)\]\(([^)]+)\)', r'<a href="\2">\1</a>', self.cli.docs[docid]['content'])
        before = {docid: self.cli.docs[docid]['content'] for docid in ('root', 'api', 'req')}
        self.cli.calls.clear()
        self.publish()
        self.assertEqual(self.block_mutations(), [])
        self.assertEqual(before, {docid: self.cli.docs[docid]['content'] for docid in before})

    def test_legacy_entry_rejects_shared_config_before_remote_calls_or_source_writes(self):
        self.existing_requirement()
        self.publish()
        self.cli.docs['root']['content'] += '\nCOLLEAGUE-LINK-TO-PRESERVE\n'
        before_docs = copy.deepcopy(self.cli.docs)
        before_source = self.source.read_bytes()
        before_state = Path(self.config['state_path']).read_bytes()
        self.cli.calls.clear()
        legacy = publisher.Publisher(self.config, cli=self.cli)
        with self.assertRaisesRegex(publisher.PublisherError, 'shared_publisher.py'):
            self.publish(legacy)
        self.assertEqual(self.cli.calls, [])
        self.assertEqual(self.cli.docs, before_docs)
        self.assertEqual(self.source.read_bytes(), before_source)
        self.assertEqual(Path(self.config['state_path']).read_bytes(), before_state)

    def test_unchanged_source_does_not_mutate_indexes_or_notify(self):
        self.existing_requirement()
        self.publish()
        self.cli.calls.clear()
        result = self.publish()
        self.assertFalse(result['changed'])
        self.assertEqual(self.block_mutations(), [])
        self.assertEqual(result['notification']['status'], 'unchanged')

    def test_missing_edit_read_permission_stops_before_any_body_create(self):
        self.cli.denied.add('mall')
        with self.assertRaisesRegex(publisher.PublisherError, 'permission'):
            self.publish()
        self.assertFalse(any(r == ('smartpage', 'create') for r, _ in self.cli.calls))

    def test_multiple_exact_tapd_matches_stop_before_create_or_write(self):
        self.existing_requirement('req-a')
        self.existing_requirement('req-b')
        with self.assertRaisesRegex(publisher.PublisherError, 'conflict|multiple|ambiguous'):
            self.publish()
        self.assertFalse(any(r == ('smartpage', 'create') for r, _ in self.cli.calls))
        self.assertEqual(self.block_mutations(), [])

    def test_known_requirement_wrong_exact_tapd_is_not_trusted(self):
        self.existing_requirement()
        self.publish()
        self.cli.docs['req']['content'] = self.cli.docs['req']['content'].replace(TAPD, TAPD + '?different=1')
        self.cli.calls.clear()
        with self.assertRaisesRegex(publisher.PublisherError, 'TAPD'):
            self.publish()
        self.assertEqual(self.block_mutations(), [])

    def test_wrong_tapd_link_does_not_get_reused(self):
        self.existing_requirement()
        self.cli.docs['req']['content'] = self.cli.docs['req']['content'].replace(TAPD, TAPD + '?different=1')
        self.publish()
        self.assertEqual(len(self.cli.docs), 7)
        self.assertNotIn('Author One', self.cli.docs['req']['content'])

    def test_new_requirement_is_seeded_and_linked_by_append_without_replacing_indexes(self):
        result = self.publish()
        state = json.loads(Path(self.config['state_path']).read_text())
        requirement = next(d for k, d in state['documents'].items() if k.startswith('requirement:'))
        content = self.cli.docs[requirement['docid']]['content']
        self.assertIn('[TAPD 需求](' + TAPD + ')', content)
        self.assertIn('## API', content)
        self.assertIn(result['url'], content)
        self.assertEqual(len(self.cli.docs), 6)
        self.assertIn(requirement['url'], self.cli.docs['root']['content'])
        self.assertIn(requirement['url'], self.cli.docs['api']['content'])
        self.assertFalse(any(r[-1] == 'overwrite' for r, _ in self.cli.calls))

    def test_new_own_requirement_keeps_configured_member_permissions(self):
        self.publish()
        state = json.loads(Path(self.config['state_path']).read_text())
        requirement = next(d for k, d in state['documents'].items() if k.startswith('requirement:'))
        self.assertFalse(requirement.get('external_catalog', False))
        self.assertEqual(self.cli.docs[requirement['docid']]['members'], {'alice': 'edit', 'bob': 'read'})
        self.publish()
        self.assertFalse(json.loads(Path(self.config['state_path']).read_text())['documents'][
            next(k for k in state['documents'] if k.startswith('requirement:'))].get('external_catalog', False))

    def test_new_requirement_retries_initial_rule_failure_and_seeds_same_doc(self):
        self.cli.rule_failures = 1
        with self.assertRaisesRegex(publisher.CliError, 'rules permission denied'):
            self.publish()
        state = json.loads(Path(self.config['state_path']).read_text())
        requirement = next(d for k, d in state['documents'].items() if k.startswith('requirement:'))
        docid = requirement['docid']
        self.assertTrue(requirement.get('seed_pending'))
        self.assertEqual(len(self.cli.docs), 5)
        self.assertEqual(self.cli.docs[docid]['content'], '')
        result = self.publish()
        self.assertEqual(len(self.cli.docs), 6)
        self.assertTrue(self.cli.docs[docid]['restricted'])
        self.assertIn(TAPD, self.cli.docs[docid]['content'])
        self.assertIn(result['url'], self.cli.docs[docid]['content'])
        self.assertIn(requirement['url'], self.cli.docs['root']['content'])

    def test_known_own_requirement_rule_denial_stops_before_body_create_or_index_write(self):
        self.cli.rule_failures = 1
        with self.assertRaises(publisher.CliError):
            self.publish()
        state = json.loads(Path(self.config['state_path']).read_text())
        key = next(k for k in state['documents'] if k.startswith('requirement:'))
        requirement = state['documents'][key]
        # Seed intent would have survived a create/restrict interruption.
        requirement['seed_pending'] = True
        Path(self.config['state_path']).write_text(json.dumps(state))
        self.cli.denied_rules.add(requirement['docid'])
        self.cli.calls.clear()
        with self.assertRaisesRegex(publisher.CliError, 'rules permission denied'):
            self.publish()
        self.assertFalse(any(r == ('smartpage', 'create') for r, _ in self.cli.calls))
        self.assertEqual(self.block_mutations(), [])
        self.assertEqual(len(self.cli.docs), 5)

    def test_body_link_in_wrong_project_section_is_added_to_correct_section(self):
        self.existing_requirement()
        result = self.publish()
        self.cli.docs['req']['content'] = self.cli.docs['req']['content'].replace(
            '- [Author One · design · design.md](' + result['url'] + ')\n', '')
        self.cli.docs['req']['content'] += '- [Wrong section](' + result['url'] + ')\n'
        self.publish()
        content = self.cli.docs['req']['content']
        api = content.split('## API', 1)[1].split('## Admin', 1)[0]
        self.assertIn(result['url'], api)
        self.assertEqual(content.count(result['url']), 2)

    def test_missing_heading_is_appended_without_guessing_block_id(self):
        self.existing_requirement()
        self.cli.docs['req']['content'] = self.cli.docs['req']['content'].replace('## API', '# Historical API')
        self.publish()
        self.assertIn('## API\n', self.cli.docs['req']['content'])
        self.assertTrue(any(d['method'] == 'append' and '## API' in d['mdx'] for _, d in self.block_mutations()))

    def test_duplicate_project_heading_fails_without_inserting_under_a_guess(self):
        self.existing_requirement()
        self.cli.docs['req']['content'] += '\n## API\n'
        with self.assertRaisesRegex(publisher.PublisherError, 'duplicate|ambiguous'):
            self.publish()
        self.assertFalse(any(d['docid'] == 'req' for _, d in self.block_mutations()))

    def test_transport_block_json_list_is_used_without_opening_embedded_path(self):
        self.existing_requirement()
        self.cli.block_transport = True
        self.publish()
        inserted = [d for _, d in self.block_mutations() if d['method'] == 'insertAfter']
        self.assertTrue(inserted)
        self.assertTrue(all(d['block_id'].startswith('actual-') for d in inserted))

    def test_large_block_transport_can_be_the_direct_node_list(self):
        self.existing_requirement()
        original = self.cli.call
        def call(route, data):
            result = original(route, data)
            if route == ('smartpage', 'pages', 'get') and data.get('content_type') == 'block':
                return {'response': {'pages': [{'page_id': 'page', 'file_path': '/never/open'}]},
                        'transport_metadata': json.loads(result['pages'][0]['content_file_inner'])}
            return result
        self.cli.call = call
        result = self.publish()
        self.assertIn(result['url'], self.cli.docs['req']['content'])

    def test_markdown_special_display_names_do_not_inject_links_or_repeat_entries(self):
        self.existing_requirement()
        self.config['publisher_label'] = 'Author [One]'
        first = self.publish()
        content = self.cli.docs['req']['content']
        self.assertIn(r'Author \[One\] · design · design.md', content)
        self.cli.calls.clear()
        self.publish()
        self.assertEqual(content.count(first['url']), 1)
        self.assertEqual(self.block_mutations(), [])

    def test_uncertain_block_mutation_preserves_pending_and_stops_retry(self):
        self.existing_requirement()
        self.cli.fail_blocks = True
        with self.assertRaises(publisher.CliError):
            self.publish()
        state = json.loads(Path(self.config['state_path']).read_text())
        self.assertTrue(any(d.get('pending') for d in state['documents'].values()))
        count = len(self.cli.calls)
        with self.assertRaisesRegex(publisher.PublisherError, 'pending'):
            self.publish()
        self.assertEqual(len(self.cli.calls), count)

    def test_duplicate_requirement_appearing_after_root_link_is_reported(self):
        self.cli.after_root_append = lambda: self.existing_requirement('raced-req')
        with self.assertRaisesRegex(publisher.PublisherError, 'conflict|multiple|ambiguous'):
            self.publish()
        self.assertIn('raced-req', self.cli.docs)
        self.assertFalse(any('delete' in r for r, _ in self.cli.calls))

    def test_excluded_members_are_not_queried_or_reported_pending(self):
        self.existing_requirement()
        self.config['member_lookup_excluded'] = ['Outside Authorization']
        self.config['projects']['api']['maintainers'].append('Outside Authorization')
        result = self.publish()
        searches = [d['keywords'] for r, d in self.cli.calls if r == ('contact', 'users', 'search')]
        self.assertFalse(any('Outside Authorization' in names for names in searches))
        self.assertFalse(any('Outside Authorization' in w for w in result['warnings']))
        self.assertFalse(result['permissions_pending'])
        self.assertIn('Outside Authorization', self.config['projects']['api']['maintainers'])

    def test_label_is_required_only_for_execution(self):
        self.config['publisher_label'] = ' '
        self.publish(execute=False)
        with self.assertRaisesRegex(publisher.PublisherError, 'publisher_label'):
            self.publish()
        self.assertEqual(self.cli.calls, [])

    def test_previews_have_zero_remote_calls_and_no_state(self):
        self.app.init()
        self.publish(execute=False)
        self.app.sync_members()
        self.assertEqual(self.cli.calls, [])
        self.assertFalse(Path(self.config['state_path']).exists())

    def test_existing_unprefixed_body_keeps_title_but_index_uses_current_author_label(self):
        self.existing_requirement()
        result = self.publish()
        state = json.loads(Path(self.config['state_path']).read_text())
        body = next(d for k, d in state['documents'].items() if k.startswith('body:'))
        body['title'] = 'api · design · design.md'
        Path(self.config['state_path']).write_text(json.dumps(state))
        self.cli.docs['req']['content'] = self.cli.docs['req']['content'].replace(
            '- [Author One · design · design.md](' + result['url'] + ')\n', '')
        self.publish()
        self.assertIn('- [Author One · design · design.md](' + result['url'] + ')', self.cli.docs['req']['content'])
        state = json.loads(Path(self.config['state_path']).read_text())
        self.assertEqual(next(d['title'] for k, d in state['documents'].items() if k.startswith('body:')),
                         'api · design · design.md')

    def test_existing_own_body_title_and_url_survive_label_change(self):
        self.existing_requirement()
        first = self.publish()
        self.config['publisher_label'] = 'Renamed Author'
        self.source.write_text('# Changed body\n')
        second = self.publish()
        self.assertEqual(first['url'], second['url'])
        doc = next(d for d in self.cli.docs.values() if d['content'] == '# Changed body\n')
        self.assertEqual(doc['name'], 'Author One · api · design · design.md')


if __name__ == '__main__':
    unittest.main()
