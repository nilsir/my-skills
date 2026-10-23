"""Environment key configuration without contacting a real robot."""
import json
import os
import unittest
from urllib.parse import parse_qs, urlsplit
from unittest.mock import patch

import group_notify
import test_group_notify


class EnvironmentKeyTests(unittest.TestCase):
    def test_key_variable_builds_fixed_wecom_endpoint(self):
        with patch.dict(os.environ, {'WECOM_WEBHOOK_KEY': 'example-only-key'}, clear=True):
            url = group_notify.read_webhook({'webhook_key_env': 'WECOM_WEBHOOK_KEY'})
        self.assertEqual(urlsplit(url).netloc, 'qyapi.weixin.qq.com')
        self.assertEqual(urlsplit(url).path, '/cgi-bin/webhook/send')
        self.assertEqual(parse_qs(urlsplit(url).query), {'key': ['example-only-key']})

    def test_environment_takes_precedence_over_private_file(self):
        with patch.dict(os.environ, {'TEAM_BOT_KEY': 'new-example-key'}, clear=True):
            url = group_notify.read_webhook({'webhook_key_env': 'TEAM_BOT_KEY',
                                            'webhook_file': '/missing/private-file'})
        self.assertEqual(parse_qs(urlsplit(url).query)['key'], ['new-example-key'])

    def test_unset_environment_keeps_existing_private_file_working(self):
        fixture = test_group_notify.NotificationTests('test_first_updated_and_unchanged_across_instances')
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        settings = dict(fixture.config['group_notification'], webhook_key_env='WECOM_WEBHOOK_KEY')
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(group_notify.read_webhook(settings), test_group_notify.SECRET_URL)

    def test_missing_invalid_and_empty_keys_fail_without_disclosing_values(self):
        for value in ('', ' ', 'contains\nnewline', 'https://example.invalid/?key=secret', 'x&key=y', 'x#y'):
            with self.subTest(value=value), patch.dict(os.environ, {'WECOM_WEBHOOK_KEY': value}, clear=True):
                with self.assertRaises(ValueError) as caught:
                    group_notify.read_webhook({'webhook_key_env': 'WECOM_WEBHOOK_KEY',
                                               'webhook_file': '/missing/private-file'})
                self.assertEqual(str(caught.exception), 'Group webhook is not configured or invalid')

    def test_custom_variable_name_must_be_a_shell_identifier(self):
        with patch.dict(os.environ, {'bad-name': 'example-only-key'}, clear=True):
            with self.assertRaises(ValueError):
                group_notify.read_webhook({'webhook_key_env': 'bad-name'})

    def test_bad_env_never_falls_back_to_a_valid_old_robot(self):
        fixture = test_group_notify.NotificationTests('test_first_updated_and_unchanged_across_instances')
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        settings = dict(fixture.config['group_notification'], webhook_key_env='WECOM_WEBHOOK_KEY')
        for value in ('', 'invalid&key=value'):
            with self.subTest(value=value), patch.dict(os.environ, {'WECOM_WEBHOOK_KEY': value}, clear=True):
                with self.assertRaisesRegex(ValueError, '^Group webhook is not configured or invalid$'):
                    group_notify.read_webhook(settings)


class EnvironmentPublicationTests(unittest.TestCase):
    setUp = test_group_notify.NotificationTests.setUp
    app = test_group_notify.NotificationTests.app
    events = test_group_notify.NotificationTests.events
    publish = test_group_notify.NotificationTests.publish

    def test_first_update_and_unchanged_use_env_without_persisting_key(self):
        self.config['group_notification'] = {'enabled': True, 'webhook_key_env': 'WECOM_WEBHOOK_KEY'}
        received = []

        def send(url, payload):
            received.append(url)
            return {'errcode': 0}

        with patch.dict(os.environ, {'WECOM_WEBHOOK_KEY': 'env-only-test-key'}, clear=True):
            app = self.app(send)
            self.assertEqual(self.publish(app)['notification']['status'], 'sent')
            self.assertEqual(self.publish(app)['notification']['status'], 'unchanged')
            self.source.write_text('# Updated\n')
            self.assertEqual(self.publish(app)['notification']['status'], 'sent')
        self.assertEqual(len(received), 2)
        self.assertTrue(all(parse_qs(urlsplit(url).query)['key'] == ['env-only-test-key'] for url in received))
        self.assertNotIn('env-only-test-key', self.root.joinpath('state.json').read_text())
        self.assertNotIn('env-only-test-key', json.dumps(self.config))

    def test_missing_key_publishes_document_without_sending(self):
        self.config['group_notification'] = {'enabled': True, 'webhook_key_env': 'WECOM_WEBHOOK_KEY'}
        with patch.dict(os.environ, {}, clear=True):
            result = self.publish()
        self.assertEqual(result['status'], 'published')
        self.assertEqual(result['notification']['status'], 'not_configured')
        self.assertEqual(self.sent, [])


if __name__ == '__main__':
    unittest.main()
