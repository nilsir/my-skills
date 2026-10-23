import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

try:
    import publisher
except ImportError:
    publisher = None


TAPD = 'https://www.tapd.cn/tapd_fe/12345678/story/detail/1123456789012345678'
PNG = b'\x89PNG\r\n\x1a\n' + b'fixture-image'


class FakeCli:
    """Stateful external service boundary: documents, pages, images and members."""
    def __init__(self):
        self.docs = {}
        self.calls = []
        self.contacts_denied = False
        self.uncertain_create = False
        self.uncertain_write = False
        self.reject_create = False
        self.uncertain_upload = False
        self.uncertain_rules = False
        self.uncertain_members = False
        self.contacts = [{'userid': 'alice', 'name': 'Alice'}, {'userid': 'bob', 'name': 'Bob'}]

    def call(self, route, data):
        self.calls.append((route, dict(data)))
        if route == ('contact', 'users', 'search'):
            if self.contacts_denied:
                raise publisher.CliError('permission denied 850002', uncertain=False)
            return {'users': self.contacts}
        if route == ('smartpage', 'create'):
            if self.reject_create:
                raise publisher.CliError('definite rejection', uncertain=False)
            if self.uncertain_create:
                raise publisher.CliError('timeout', uncertain=True)
            docid = str(len(self.docs) + 1)
            self.docs[docid] = {'name': data['name'], 'content': '', 'members': {}, 'restricted': False, 'images': []}
            return {'docid': docid, 'url': 'https://doc.weixin.qq.com/smartpage/' + docid}
        doc = self.docs[data['docid']]
        if route == ('smartpage', 'pages', 'get'):
            if 'page_id' not in data:
                return {'pages': [{'page_id': 'page', 'parent_id': 'hidden-root'}]}
            return {'content_file_inner': json.dumps(doc['content'])}
        if route in [('smartpage', 'pages', 'append'), ('smartpage', 'pages', 'overwrite')]:
            if self.uncertain_write:
                raise publisher.CliError('write timed out', uncertain=True)
            content = Path(data['file_path']).read_text()
            doc['content'] = doc['content'] + content if route[-1] == 'append' else content
            return {'errcode': 0}
        if route == ('smartpage', 'images', 'upload'):
            if self.uncertain_upload:
                raise publisher.CliError('upload timeout', uncertain=True)
            content = Path(data['file_path']).read_bytes()
            doc['images'].append(content)
            return {'url': 'https://doc.weixin.qq.com/image/' + str(len(doc['images']))}
        if route == ('doc', 'rules', 'update'):
            if self.uncertain_rules:
                raise publisher.CliError('rules timeout', uncertain=True)
            doc['restricted'] = data['enable_member_join_admin_check']
            return {'errcode': 0}
        if route == ('doc', 'members', 'update'):
            if self.uncertain_members:
                raise publisher.CliError('members timeout', uncertain=True)
            for item in data['add_member_list']['items']:
                doc['members'][item['userid']] = item['user_auth']
            return {'errcode': 0}
        raise AssertionError((route, data))


class PublisherTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(publisher, 'publisher implementation is missing')
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.source = self.root / 'design.md'
        self.source.write_text('# Example\nBody\n')
        self.cli = FakeCli()
        self.config = {'state_path': str(self.root / 'state.json'), 'cli_path': 'wecom-cli', 'projects': {
            name: {'repo_root': str(self.root), 'maintainers': ['Alice'], 'readers': ['Bob']}
            for name in ('api', 'admin', 'mall')}}
        self.app = publisher.Publisher(self.config, cli=self.cli)

    def publish(self, project='api', execute=True):
        return self.app.publish(project, TAPD, self.source, 'design', execute=execute)

    def test_stable_document_and_unique_indexes_across_changed_source(self):
        first = self.publish()
        self.source.write_text('# Example\nUpdated body\n')
        second = self.publish()
        self.assertEqual(first['url'], second['url'])
        self.assertEqual(len(self.cli.docs), 6)  # root, three projects, requirement, source
        body = next(doc for doc in self.cli.docs.values() if 'Updated body' in doc['content'])
        self.assertTrue(body['restricted'])
        self.assertEqual(body['members'], {'alice': 'edit', 'bob': 'read'})
        req = [doc for doc in self.cli.docs.values() if first['url'] in doc['content']]
        self.assertEqual(len(req), 1)
        self.assertEqual(req[0]['content'].count(first['url']), 1)
        self.assertTrue(any(route[-1] == 'overwrite' for route, _ in self.cli.calls))

    def test_two_projects_share_one_requirement_entry(self):
        a = self.publish('api')
        b = self.publish('admin')
        self.assertNotEqual(a['url'], b['url'])
        self.assertEqual(len(self.cli.docs), 7)
        req = [doc for doc in self.cli.docs.values() if a['url'] in doc['content'] and b['url'] in doc['content']]
        self.assertEqual(len(req), 1)

    def test_permission_failure_still_restricts_and_indexes_then_resumes(self):
        self.cli.contacts_denied = True
        first = self.publish()
        self.assertTrue(first['permissions_pending'])
        self.assertTrue(all(doc['restricted'] for doc in self.cli.docs.values()))
        count = len(self.cli.docs)
        self.cli.contacts_denied = False
        resumed = self.app.sync_members(execute=True)
        self.assertFalse(resumed['permissions_pending'])
        self.assertEqual(len(self.cli.docs), count)
        self.assertTrue(all(doc['members']['alice'] == 'edit' for doc in self.cli.docs.values()))
        self.assertEqual(self.publish()['url'], first['url'])

    def test_uncertain_create_blocks_automatic_duplicate(self):
        self.cli.uncertain_create = True
        with self.assertRaises(publisher.CliError):
            self.publish()
        count = len(self.cli.calls)
        with self.assertRaisesRegex(publisher.PublisherError, 'pending'):
            self.publish()
        self.assertEqual(len(self.cli.calls), count)
        self.assertTrue(json.loads((self.root / 'state.json').read_text())['documents'])

    def test_dry_run_all_mutations_make_no_remote_calls(self):
        self.app.init()
        self.publish(execute=False)
        self.app.sync_members()
        self.app.status()
        self.assertEqual(self.cli.calls, [])
        self.assertFalse((self.root / 'state.json').exists())

    def test_nested_encoding_preserves_literal_backslash_sequences(self):
        text = '# Paths\n`C:\\new\\test`\n'
        nested = {'content_file_inner': json.dumps(json.dumps(text)), 'security_notice': 'hide',
                  'extra_identity_context': 'hide'}
        self.assertEqual(publisher.extract_markdown(nested), text)
        self.assertNotIn('security_notice', publisher.sanitize(nested))

    def test_asset_content_change_updates_source_document(self):
        image = self.root / 'diagram.png'
        image.write_bytes(PNG)
        self.source.write_text('# Design\n![diagram](diagram.png)\n')
        first = self.publish()
        image.write_bytes(PNG + b'changed')
        second = self.publish()
        self.assertEqual(first['url'], second['url'])
        self.assertTrue(second['changed'])
        body = next(doc for doc in self.cli.docs.values() if doc['images'])
        self.assertEqual(len(body['images']), 2)

    def test_mermaid_failure_precedes_any_remote_call(self):
        self.source.write_text('# Design\n```mermaid\ngraph TD\n A-->B\n```\n')
        def fail(*args):
            raise publisher.PublisherError('mermaid render failed')
        self.app.renderer = fail
        with self.assertRaisesRegex(publisher.PublisherError, 'mermaid'):
            self.publish()
        self.assertEqual(self.cli.calls, [])

    def test_ambiguous_contacts_remain_pending_without_arbitrary_choice(self):
        self.cli.contacts = [{'userid': 'a', 'name': 'Alice One'}, {'userid': 'b', 'name': 'Alice Two'}]
        result = self.publish()
        self.assertTrue(result['permissions_pending'])
        self.assertTrue(all(not doc['members'] for doc in self.cli.docs.values()))

    def test_unknown_tapd_route_and_active_html_fail_before_calls(self):
        with self.assertRaises(publisher.PublisherError):
            self.app.publish('api', 'https://untrusted.example/unknown/123', self.source, 'design', execute=True)
        self.source.write_text('<script>alert(1)</script>')
        with self.assertRaises(publisher.PublisherError):
            self.publish()
        self.assertEqual(self.cli.calls, [])

    def test_read_uses_saved_url_and_markdown_content(self):
        result = self.publish()
        read = self.app.read(result['url'])
        self.assertEqual(read['markdown'], self.source.read_text())

    def test_read_unmanaged_smartpage_url_and_ordinary_document(self):
        self.cli.docs['external'] = {'content': '# External'}
        result = self.app.read('https://doc.weixin.qq.com/smartpage/external?scode=ok')
        self.assertEqual(result['markdown'], '# External')
        class OrdinaryCli:
            def call(self, route, data):
                if route != ('doc', 'contents', 'get') or data != {'docid': 'ordinary', 'content_type': 'markdown'}:
                    raise AssertionError((route, data))
                return {'content': '# Ordinary'}
        self.app.cli = OrdinaryCli()
        self.assertEqual(self.app.read('https://doc.weixin.qq.com/doc/ordinary?scode=ok')['markdown'], '# Ordinary')
        with self.assertRaisesRegex(publisher.PublisherError, 'Unsupported'):
            self.app.read('https://doc.weixin.qq.com/sheet/example')

    def test_uncertain_write_stops_retries_with_existing_mapping(self):
        self.cli.uncertain_write = True
        with self.assertRaises(publisher.CliError):
            self.publish()
        count = len(self.cli.calls)
        with self.assertRaisesRegex(publisher.PublisherError, 'pending'):
            self.publish()
        self.assertEqual(len(self.cli.calls), count)
        docs = json.loads((self.root / 'state.json').read_text())['documents']
        self.assertTrue(any(doc.get('pending') == 'append' and doc.get('url') for doc in docs.values()))

    def test_definite_create_rejection_can_retry(self):
        self.cli.reject_create = True
        with self.assertRaises(publisher.CliError):
            self.publish()
        self.cli.reject_create = False
        self.assertEqual(self.publish()['status'], 'published')

    def test_alias_matches_and_lookup_batches_are_at_most_ten(self):
        self.config['projects']['api']['readers'] = ['person' + str(i) for i in range(25)]
        self.cli.contacts = [{'userid': 'alice', 'name': 'Formal Name', 'alias': 'Alice'}]
        self.publish()
        searches = [data for route, data in self.cli.calls if route == ('contact', 'users', 'search')]
        self.assertEqual(len(searches), 3)
        self.assertTrue(all(len(data['keywords']) <= 10 for data in searches))
        self.assertTrue(all(doc['members'].get('alice') == 'edit' for doc in self.cli.docs.values()))

    def test_unchanged_source_does_not_rewrite_body(self):
        result = self.publish()
        self.cli.calls.clear()
        again = self.publish()
        self.assertFalse(again['changed'])
        self.assertEqual(again['url'], result['url'])
        self.assertFalse(any(route[-1] in ('append', 'overwrite') for route, _ in self.cli.calls))

    def test_invalid_image_is_rejected_before_remote_calls(self):
        (self.root / 'bad.png').write_text('not an image')
        self.source.write_text('![bad](bad.png)')
        with self.assertRaisesRegex(publisher.PublisherError, 'invalid image'):
            self.publish()
        self.assertFalse(self.cli.calls)

    def test_fenced_code_examples_are_not_uploaded_as_images(self):
        self.source.write_text('```md\n![example](missing.png)\n<script>example</script>\n```\n')
        self.assertEqual(self.publish()['status'], 'published')

    def test_mermaid_renderer_uses_strict_config_and_chrome(self):
        self.config['chrome_path'] = '/Chrome path'
        self.config['mermaid_cli'] = '/mmdc path'
        def render(argv, **kwargs):
            self.assertEqual(argv[0], '/mmdc path')
            self.assertFalse(kwargs.get('shell', False))
            self.assertEqual(json.loads(Path(argv[argv.index('-p') + 1]).read_text())['executablePath'], '/Chrome path')
            settings = json.loads(Path(argv[argv.index('-c') + 1]).read_text())
            self.assertEqual(settings['securityLevel'], 'strict')
            self.assertEqual(settings['fontFamily'], 'PingFang SC')
            self.assertEqual(settings['flowchart']['wrappingWidth'], 320)
            Path(argv[argv.index('-o') + 1]).write_bytes(PNG)
            return subprocess.CompletedProcess(argv, 0, '', '')
        with patch('subprocess.run', side_effect=render):
            path = self.app.render_mermaid('graph TD; A-->B', self.root, 0)
        self.assertTrue(path.exists())

    def test_requirements_group_documents_by_project_heading(self):
        a = self.publish('api')
        b = self.publish('admin')
        req = next(doc for doc in self.cli.docs.values() if a['url'] in doc['content'] and b['url'] in doc['content'])
        self.assertIn('## API\n', req['content'])
        self.assertIn('## Admin\n', req['content'])
        self.assertIn('## Mall\n', req['content'])
        api_section = req['content'].split('## API\n', 1)[1].split('## Admin\n', 1)[0]
        self.assertIn(a['url'], api_section)
        self.assertNotIn(b['url'], api_section)

    def test_unique_substring_never_grants_access_without_exact_identity(self):
        self.cli.contacts = [{'userid': 'unconfirmed', 'name': 'Alice Smith'}]
        result = self.publish()
        self.assertTrue(result['permissions_pending'])
        self.assertTrue(all('unconfirmed' not in doc['members'] for doc in self.cli.docs.values()))

    def test_read_combines_all_visible_pages_in_returned_order(self):
        class MultiPageCli:
            def call(self, route, data):
                if route != ('smartpage', 'pages', 'get'):
                    raise AssertionError(route)
                if 'page_id' not in data:
                    return {'pages': [{'page_id': 'one', 'parent_id': 'hidden'}, {'page_id': 'two', 'parent_id': 'hidden'}]}
                return {'content_file_inner': json.dumps('# ' + data['page_id'])}
        self.app.cli = MultiPageCli()
        markdown = self.app.read('https://doc.weixin.qq.com/smartpage/external')['markdown']
        self.assertIn('# one', markdown)
        self.assertIn('# two', markdown)
        self.assertLess(markdown.index('# one'), markdown.index('# two'))

    def test_local_reference_style_image_is_uploaded(self):
        (self.root / 'diagram.png').write_bytes(PNG)
        self.source.write_text('# Design\n![diagram][chart]\n\n[chart]: diagram.png "Diagram"\n')
        result = self.publish()
        body = next(doc for doc in self.cli.docs.values() if doc['images'])
        self.assertEqual(len(body['images']), 1)
        self.assertIn('https://doc.weixin.qq.com/image/', body['content'])
        self.assertTrue(result['changed'])

    def test_longer_closing_fence_renders_mermaid_and_unclosed_fails(self):
        self.source.write_text('# Graph\n```mermaid\ngraph TD; A-->B\n````\n')
        def render(source, directory, number):
            path = Path(directory) / 'graph.png'
            path.write_bytes(PNG)
            return path
        self.app.renderer = render
        self.publish()
        self.assertTrue(any(doc['images'] for doc in self.cli.docs.values()))
        self.assertFalse(any('```mermaid' in doc['content'] for doc in self.cli.docs.values()))
        self.cli.calls.clear()
        self.source.write_text('```mermaid\ngraph TD; A-->B\n~~\n')
        with self.assertRaisesRegex(publisher.PublisherError, 'Unclosed Mermaid'):
            self.publish()
        self.assertFalse(self.cli.calls)

    def test_link_only_tapd_fallback_is_stable_and_has_no_inferred_id(self):
        url = 'https://tapd.cn/other/view?token=123'
        a = self.app.publish('api', url, self.source, 'design', execute=True)
        self.source.write_text('# New body')
        b = self.app.publish('api', url, self.source, 'design', execute=True)
        self.assertEqual(a['url'], b['url'])
        state = json.loads((self.root / 'state.json').read_text())
        req = next(iter(state['requirements'].values()))
        self.assertIsNone(req['story'])
        self.assertEqual(req['tapd'], url)
        for url in ('http://tapd.cn/other', 'https://tapd.cn.evil.example/view', 'https://tapd.cn@evil.example/view'):
            with self.subTest(url=url), self.assertRaises(publisher.PublisherError):
                self.app.publish('api', url, self.source, 'design', execute=True)

    def test_uncertain_image_upload_blocks_unsafe_retry(self):
        (self.root / 'diagram.png').write_bytes(PNG)
        self.source.write_text('![image](diagram.png)')
        self.cli.uncertain_upload = True
        with self.assertRaises(publisher.CliError):
            self.publish()
        count = len(self.cli.calls)
        with self.assertRaisesRegex(publisher.PublisherError, 'pending'):
            self.publish()
        self.assertEqual(len(self.cli.calls), count)

    def test_uncertain_rules_and_members_persist_then_safe_sync_resumes(self):
        self.cli.uncertain_rules = True
        with self.assertRaises(publisher.CliError):
            self.publish()
        state = json.loads((self.root / 'state.json').read_text())
        self.assertTrue(any(doc.get('pending_access') == 'rules_update' for doc in state['documents'].values()))
        self.cli.uncertain_rules = False
        self.cli.uncertain_members = True
        result = self.publish()
        self.assertTrue(result['permissions_pending'])
        count = len(self.cli.docs)
        state = json.loads((self.root / 'state.json').read_text())
        self.assertTrue(any(doc.get('pending_access') == 'members_update' for doc in state['documents'].values()))
        self.cli.uncertain_members = False
        result = self.app.sync_members(execute=True)
        self.assertFalse(result['permissions_pending'])
        self.assertEqual(len(self.cli.docs), count)
        state = json.loads((self.root / 'state.json').read_text())
        self.assertFalse(any(doc.get('pending_access') for doc in state['documents'].values()))

    def test_benign_query_and_inline_html_examples_are_publishable(self):
        source = '# Notes\n[Open](https://example.test/?online=1)\nUse `<img onerror="example">` and `javascript:example`.\n'
        self.source.write_text(source)
        result = self.publish()
        self.assertEqual(self.app.read(result['url'])['markdown'], source)

    def test_event_attribute_on_actual_html_tag_is_rejected(self):
        self.source.write_text('<img src="x" onerror="alert(1)">')
        with self.assertRaises(publisher.PublisherError):
            self.publish()
        self.assertFalse(self.cli.calls)

    def test_inline_and_escaped_image_examples_remain_unchanged(self):
        source = '# Notes\nUse `![diagram](missing.png)` and ``![diagram][ref]``.\n\\![literal](missing.png)\n\\![literal][ref]\n\n[ref]: missing.png\n'
        self.source.write_text(source)
        result = self.publish()
        self.assertEqual(self.app.read(result['url'])['markdown'], source)
        self.assertFalse(any(doc['images'] for doc in self.cli.docs.values()))

    def test_unpublished_local_inline_doc_links_become_plain_references(self):
        original = '# Handoff\n[Design](./design-notes.md#scope)\n[Remote](https://example.test/guide.md)\n[Section](#scope)\n`[Example](./missing.md)`\n\\[Literal](./missing.md)\n'
        self.source.write_text(original)
        result = self.publish()
        markdown = self.app.read(result['url'])['markdown']
        self.assertIn('Design（design-notes.md#scope）', markdown)
        self.assertNotIn('[Design](./design-notes.md#scope)', markdown)
        self.assertIn('[Remote](https://example.test/guide.md)', markdown)
        self.assertIn('[Section](#scope)', markdown)
        self.assertIn('`[Example](./missing.md)`', markdown)
        self.assertIn('\\[Literal](./missing.md)', markdown)
        self.assertTrue(any('unresolved_local_links' in warning for warning in result['warnings']))
        self.assertEqual(self.source.read_text(), original)

    def test_later_published_link_target_refreshes_source_without_source_edit(self):
        target = self.root / 'target.md'
        target.write_text('# Target\n')
        self.source.write_text('# Source\n[Target](target.md)\n')
        first = self.publish()
        target_result = self.app.publish('api', TAPD, target, 'design', execute=True)
        second = self.publish()
        self.assertEqual(second['url'], first['url'])
        self.assertTrue(second['changed'])
        markdown = self.app.read(second['url'])['markdown']
        self.assertIn('[Target](' + target_result['url'] + ')', markdown)
        state = json.loads((self.root / 'state.json').read_text())
        self.assertTrue(any(doc.get('source_file') == str(target.resolve()) for doc in state['documents'].values()))
        self.assertFalse(any('unresolved_local_links' in warning for warning in second['warnings']))

    def test_local_reference_doc_links_use_registered_target_or_visible_filename(self):
        target = self.root / 'target.md'
        target.write_text('# Target\n')
        target_result = self.app.publish('api', TAPD, target, 'design', execute=True)
        self.source.write_text('# Handoff\n[Published][target]\n[Missing][missing]\n\n[target]: target.md\n[missing]: missing.md#section\n')
        result = self.publish()
        markdown = self.app.read(result['url'])['markdown']
        self.assertIn('[Published](' + target_result['url'] + ')', markdown)
        self.assertIn('Missing（missing.md#section）', markdown)
        self.assertNotIn('[missing]: missing.md', markdown)
        self.assertNotIn('[target]: target.md', markdown)

    def test_local_link_dry_run_warns_without_remote_calls(self):
        self.source.write_text('[Missing](missing.md)')
        result = self.publish(execute=False)
        self.assertTrue(any('unresolved_local_links' in warning for warning in result['warnings']))
        self.assertFalse(self.cli.calls)

    def test_combined_doc_explicit_projects_share_one_url_and_union_permissions(self):
        self.config['projects']['admin']['maintainers'] = ['Admin Maintainer']
        self.config['resolved_members'] = {'Alice': 'alice', 'Bob': 'bob', 'Admin Maintainer': 'admin-editor'}
        first = self.publish()
        second = self.app.publish('api', TAPD, self.source, 'design', execute=True, also_projects=['admin'])
        self.assertEqual(second['url'], first['url'])
        self.assertEqual(len(self.cli.docs), 6)
        state = json.loads((self.root / 'state.json').read_text())
        docs = state['documents']
        body = next(doc for doc in docs.values() if doc.get('url') == first['url'])
        self.assertEqual(body['projects'], ['api', 'admin'])
        self.assertEqual(self.cli.docs[body['docid']]['members']['admin-editor'], 'edit')
        req = next(doc for key, doc in docs.items() if key.startswith('requirement:'))
        req_content = self.cli.docs[req['docid']]['content']
        self.assertEqual(req_content.count(first['url']), 2)
        for project in ('api', 'admin'):
            self.assertIn(req['url'], self.cli.docs[docs['project:' + project]['docid']]['content'])
        mall = self.cli.docs[docs['project:mall']['docid']]['content']
        self.assertIn('暂无已发布需求', mall)
        self.assertNotIn(req['url'], mall)

    def test_default_cli_temp_stages_use_private_tmp_when_available(self):
        real_temporary_directory = tempfile.TemporaryDirectory
        with patch('publisher.tempfile.TemporaryDirectory', wraps=real_temporary_directory) as directories:
            self.app.init(execute=True)
            self.publish()
        expected = '/private/tmp' if Path('/private/tmp').is_dir() else None
        self.assertEqual(len(directories.call_args_list), 2)
        self.assertTrue(all(call.kwargs.get('dir') == expected for call in directories.call_args_list))

    def test_configured_temp_root_stages_image_copy_and_preserves_hash(self):
        temp_root = self.root / 'cli-temp'
        temp_root.mkdir()
        self.config['temp_root'] = str(temp_root)
        original = self.root / 'diagram.png'
        original.write_bytes(PNG)
        self.source.write_text('![Diagram](diagram.png)\n')
        first = self.publish()
        uploads = [data for route, data in self.cli.calls if route == ('smartpage', 'images', 'upload')]
        self.assertEqual(len(uploads), 1)
        staged = Path(uploads[0]['file_path'])
        self.assertNotEqual(staged, original)
        self.assertTrue(staged.resolve().is_relative_to(temp_root.resolve()))
        self.assertEqual(original.read_bytes(), PNG)
        self.cli.calls.clear()
        second = self.publish()
        self.assertEqual(first['url'], second['url'])
        self.assertFalse(second['changed'])
        self.assertFalse(any(route == ('smartpage', 'images', 'upload') for route, _ in self.cli.calls))

    def test_dry_run_reports_previously_saved_extra_projects(self):
        self.app.publish('api', TAPD, self.source, 'design', execute=True, also_projects=['admin'])
        self.cli.calls.clear()
        result = self.publish(execute=False)
        self.assertEqual(result['projects'], ['api', 'admin'])
        self.assertFalse(self.cli.calls)


class CliTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(publisher, 'publisher implementation is missing')

    def test_cli_uses_argv_json_and_no_shell_or_output_option(self):
        result = subprocess.CompletedProcess([], 0, '{"errcode":0,"security_notice":"hide"}', '')
        with patch('subprocess.run', return_value=result) as run:
            value = publisher.WeComCli('/path with spaces/wecom-cli').call(('smartpage', 'create'), {'name': 'x; $(bad)'})
        argv = run.call_args.args[0]
        self.assertEqual(argv[:3], ['/path with spaces/wecom-cli', 'smartpage', 'create'])
        self.assertEqual(json.loads(argv[argv.index('--json') + 1]), {'name': 'x; $(bad)'})
        self.assertNotIn('--output', argv)
        self.assertFalse(run.call_args.kwargs.get('shell', False))
        self.assertNotIn('security_notice', value)

    def test_cli_detects_all_failure_envelopes(self):
        for data in ({'errcode': 123}, {'status': 'failed'}, {'task_status': 'failure'}):
            with self.subTest(data=data), patch('subprocess.run', return_value=subprocess.CompletedProcess([], 0, json.dumps(data), '')):
                with self.assertRaises(publisher.CliError):
                    publisher.WeComCli('wecom-cli').call(('smartpage', 'create'), {'name': 'x'})

    def test_native_local_path_permission_rejection_is_definite_and_preserves_message(self):
        error = {'error': {'type': 'PermissionError', 'code': 893006,
                           'message': '目标路径超出可访问范围: /var/folders/file.md'}}
        result = subprocess.CompletedProcess([], 1, json.dumps(error), '')
        with patch('subprocess.run', return_value=result):
            with self.assertRaises(publisher.CliError) as caught:
                publisher.WeComCli('wecom-cli').call(('smartpage', 'pages', 'append'), {'docid': '1'})
        self.assertFalse(caught.exception.uncertain)
        self.assertIn('893006', str(caught.exception))
        self.assertIn('目标路径超出可访问范围', str(caught.exception))

    def test_other_native_errors_preserve_message_and_uncertainty(self):
        for kind, code in (('IOError', 999), ('PermissionError', 999), ('RemoteError', 893006)):
            error = {'error': {'type': kind, 'code': code, 'message': 'indeterminate upstream failure'}}
            result = subprocess.CompletedProcess([], 1, json.dumps(error), '')
            with self.subTest(kind=kind, code=code), patch('subprocess.run', return_value=result):
                with self.assertRaises(publisher.CliError) as caught:
                    publisher.WeComCli('wecom-cli').call(('smartpage', 'pages', 'append'), {'docid': '1'})
                self.assertTrue(caught.exception.uncertain)
                self.assertIn('indeterminate upstream failure', str(caught.exception))

    def test_response_file_path_is_honored(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'response.json'
            path.write_text('{"content_file_inner":"hello"}')
            result = subprocess.CompletedProcess([], 0, json.dumps({'file_path': str(path)}), '')
            with patch('subprocess.run', return_value=result):
                data = publisher.WeComCli('wecom-cli').call(('smartpage', 'pages', 'get'), {'docid': '1'})
            self.assertEqual(publisher.extract_markdown(data), 'hello')

    def test_response_file_path_with_raw_markdown_is_honored(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'response.md'
            path.write_text('# Raw Markdown\n')
            result = subprocess.CompletedProcess([], 0, json.dumps({'file_path': str(path)}), '')
            with patch('subprocess.run', return_value=result):
                data = publisher.WeComCli('wecom-cli').call(('smartpage', 'pages', 'get'), {'docid': '1'})
            self.assertEqual(publisher.extract_markdown(data), '# Raw Markdown\n')

    def test_markdown_json_example_is_not_an_api_failure_envelope(self):
        markdown = '{"errcode": 123, "status": "failed"}'
        result = subprocess.CompletedProcess([], 0, json.dumps({'errcode': 0, 'content_file_inner': json.dumps(markdown)}), '')
        with patch('subprocess.run', return_value=result):
            data = publisher.WeComCli('wecom-cli').call(('smartpage', 'pages', 'get'), {'docid': '1'})
        self.assertEqual(publisher.extract_markdown(data), markdown)

    def test_document_content_never_supplies_local_response_file_path(self):
        markdown = '{"errcode":123,"status":"failed","file_path":"/private/do-not-read"}'
        result = subprocess.CompletedProcess([], 0, json.dumps({'errcode': 0, 'content_file_inner': json.dumps(markdown)}), '')
        with patch('subprocess.run', return_value=result), patch.object(Path, 'read_text', side_effect=AssertionError('must not read document path')):
            data = publisher.WeComCli('wecom-cli').call(('smartpage', 'pages', 'get'), {'docid': '1'})
        self.assertEqual(publisher.extract_markdown(data), markdown)

    def test_raw_json_markdown_file_keeps_entire_content(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'content.md'
            markdown = '{"content":"example", "file_path":"/private/do-not-read"}'
            path.write_text(markdown)
            result = subprocess.CompletedProcess([], 0, json.dumps({'file_path': str(path)}), '')
            with patch('subprocess.run', return_value=result):
                data = publisher.WeComCli('wecom-cli').call(('smartpage', 'pages', 'get'), {'docid': '1'})
            self.assertEqual(publisher.extract_markdown(data), markdown)

    def test_documented_page_file_metadata_reads_raw_markdown_for_any_suffix(self):
        markdown = '{"errcode":123,"status":"failed","content":"literal example","file_path":"/private/do-not-read"}'
        with tempfile.TemporaryDirectory() as tmp:
            for suffix in ('.txt', '.json'):
                with self.subTest(suffix=suffix):
                    path = Path(tmp) / ('page-content' + suffix)
                    path.write_text(markdown)
                    envelope = {'data': {'result': {'pages': [{'page_id': 'one', 'file_path': str(path)}]}}}
                    result = subprocess.CompletedProcess([], 0, json.dumps(envelope), '')
                    with patch('subprocess.run', return_value=result):
                        data = publisher.WeComCli('wecom-cli').call(('smartpage', 'pages', 'get'), {
                            'docid': '1', 'page_id': 'one', 'content_type': 'markdown'})
                    self.assertEqual(publisher.extract_markdown(data), markdown)

    def test_top_level_ordinary_markdown_content_file_is_raw_json_text(self):
        markdown = '{"errcode":123,"status":"failed","content":"literal example"}'
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'content.json'
            path.write_text(markdown)
            result = subprocess.CompletedProcess([], 0, json.dumps({'file_path': str(path)}), '')
            with patch('subprocess.run', return_value=result):
                data = publisher.WeComCli('wecom-cli').call(('doc', 'contents', 'get'), {
                    'docid': '1', 'content_type': 'markdown'})
            self.assertEqual(publisher.extract_markdown(data), markdown)

    def test_nested_page_content_json_never_supplies_response_file_metadata(self):
        markdown = '{"pages":[{"file_path":"/private/do-not-read"}],"file_path":"/private/do-not-read"}'
        for content in (markdown, json.dumps(markdown)):
            with self.subTest(content=content):
                envelope = {'data': {'result': {'pages': [{'page_id': 'one', 'content_file_inner': content}]}}}
                result = subprocess.CompletedProcess([], 0, json.dumps(envelope), '')
                with patch('subprocess.run', return_value=result), patch.object(Path, 'read_text', side_effect=AssertionError('must not read content path')):
                    data = publisher.WeComCli('wecom-cli').call(('smartpage', 'pages', 'get'), {
                        'docid': '1', 'page_id': 'one', 'content_type': 'markdown'})
                self.assertEqual(publisher.extract_markdown(data), markdown)

    def test_non_content_transport_json_file_still_parses_envelope(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'response.json'
            path.write_text('{"docid":"created-id","url":"https://doc.weixin.qq.com/smartpage/created-id"}')
            result = subprocess.CompletedProcess([], 0, json.dumps({'data': {'file_path': str(path)}}), '')
            with patch('subprocess.run', return_value=result):
                data = publisher.WeComCli('wecom-cli').call(('smartpage', 'create'), {'name': 'Test'})
            self.assertEqual(publisher.field(data, 'docid'), 'created-id')
            self.assertEqual(publisher.field(data, 'url'), 'https://doc.weixin.qq.com/smartpage/created-id')

    def test_raw_markdown_file_keeps_json_string_quotes_and_final_newline(self):
        markdown = '"quoted Markdown paragraph"\n'
        with tempfile.TemporaryDirectory() as tmp:
            for suffix in ('.txt', '.json', '.md'):
                with self.subTest(suffix=suffix):
                    path = Path(tmp) / ('content' + suffix)
                    path.write_text(markdown)
                    envelope = {'pages': [{'page_id': 'one', 'file_path': str(path)}]}
                    result = subprocess.CompletedProcess([], 0, json.dumps(envelope), '')
                    with patch('subprocess.run', return_value=result):
                        data = publisher.WeComCli('wecom-cli').call(('smartpage', 'pages', 'get'), {
                            'docid': '1', 'page_id': 'one', 'content_type': 'markdown'})
                    self.assertEqual(publisher.extract_markdown(data), markdown)

    def test_raw_markdown_json_never_supplies_document_identity(self):
        markdown = '{"docid":"forged-id","url":"https://example.test/forged"}'
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'content.json'
            path.write_text(markdown)
            result = subprocess.CompletedProcess([], 0, json.dumps({'pages': [{'file_path': str(path)}]}), '')
            with patch('subprocess.run', return_value=result):
                data = publisher.WeComCli('wecom-cli').call(('smartpage', 'pages', 'get'), {
                    'docid': '1', 'page_id': 'one', 'content_type': 'markdown'})
            self.assertEqual(publisher.extract_markdown(data), markdown)
            self.assertIsNone(publisher.field(data, 'docid'))
            self.assertIsNone(publisher.field(data, 'url'))


if __name__ == '__main__':
    unittest.main()
