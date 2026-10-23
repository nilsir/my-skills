"""Link access regression tests; no real WeCom calls."""
import json
import unittest

import publisher
import test_publisher
import test_shared_publisher

RULES = ('doc', 'rules', 'update')
CORP_READ = {'enable_member_join_admin_check': False,
             'corp_internal_join_auth': 'read', 'corp_external_join_auth': 'deny'}


class JoinRulesTests(unittest.TestCase):
    setUp = test_publisher.PublisherTests.setUp
    publish = test_publisher.PublisherTests.publish

    def test_new_and_updated_documents_keep_corporate_read_without_external_join(self):
        self.config['link_access'] = 'corp_read'
        first = self.publish()
        rules = [d for r, d in self.cli.calls if r == RULES]
        self.assertEqual(len(rules), 6)
        for data in rules:
            self.assertEqual({k: v for k, v in data.items() if k != 'docid'}, CORP_READ)
        self.assertTrue(all(d['members'] == {'alice': 'edit', 'bob': 'read'} for d in self.cli.docs.values()))
        self.cli.calls.clear()
        self.source.write_text('# Example\nUpdated\n')
        self.assertEqual(self.publish()['url'], first['url'])
        self.assertFalse(any(r == RULES for r, _ in self.cli.calls))

    def test_existing_documents_migrate_only_rules_without_content_or_notifications(self):
        self.publish()
        before = {k: (v['content'], dict(v['members'])) for k, v in self.cli.docs.items()}
        self.config['link_access'] = 'corp_read'
        self.cli.calls.clear()
        result = self.app.sync_rules(execute=True)
        self.assertEqual(result['updated'], 6)
        self.assertEqual([r for r, _ in self.cli.calls], [RULES] * 6)
        self.assertEqual(before, {k: (v['content'], dict(v['members'])) for k, v in self.cli.docs.items()})
        state = json.loads((self.root / 'state.json').read_text())
        self.assertTrue(all(d['applied_join_rules'] == CORP_READ for d in state['documents'].values()))
        self.cli.calls.clear()
        self.publish()
        self.assertFalse(any(r == RULES for r, _ in self.cli.calls))

    def test_rules_dry_run_has_no_remote_call_or_state_write(self):
        result = self.app.sync_rules()
        self.assertEqual(result['status'], 'dry_run')
        self.assertEqual(self.cli.calls, [])
        self.assertFalse((self.root / 'state.json').exists())

    def test_uncertain_rules_remain_pending_and_exact_retry_recovers(self):
        self.publish()
        self.config['link_access'] = 'corp_read'
        self.cli.uncertain_rules = True
        with self.assertRaises(publisher.CliError):
            self.app.sync_rules(execute=True)
        state = json.loads((self.root / 'state.json').read_text())
        first = next(iter(state['documents'].values()))
        self.assertEqual(first['pending_access'], 'rules_update')
        self.assertNotEqual(first.get('applied_join_rules'), CORP_READ)
        self.cli.uncertain_rules = False
        self.app.sync_rules(execute=True)
        state = json.loads((self.root / 'state.json').read_text())
        self.assertTrue(all('pending_access' not in d for d in state['documents'].values()))

    def test_invalid_policy_rejected_before_any_remote_call(self):
        self.config['link_access'] = 'public_edit'
        with self.assertRaisesRegex(publisher.PublisherError, 'link_access'):
            publisher.Publisher(self.config, cli=self.cli)
        self.assertEqual(self.cli.calls, [])


class SharedJoinRulesTests(unittest.TestCase):
    setUp = test_shared_publisher.SharedPublisherTests.setUp
    publish = test_shared_publisher.SharedPublisherTests.publish
    existing_requirement = test_shared_publisher.SharedPublisherTests.existing_requirement

    def test_adopted_indexes_require_explicit_inclusion_and_never_change_content(self):
        self.config['link_access'] = 'corp_read'
        self.existing_requirement()
        self.publish()
        before = {k: v['content'] for k, v in self.cli.docs.items()}
        self.cli.calls.clear()
        self.assertEqual(self.app.sync_rules(execute=True)['updated'], 1)
        self.assertTrue(all(d['docid'] not in ('root', 'api', 'admin', 'mall', 'req') for _, d in self.cli.calls))
        self.cli.calls.clear()
        self.assertEqual(self.app.sync_rules(execute=True, include_shared=True)['updated'], 6)
        self.assertEqual([r for r, _ in self.cli.calls], [RULES] * 6)
        self.assertEqual(before, {k: v['content'] for k, v in self.cli.docs.items()})


if __name__ == '__main__':
    unittest.main()
