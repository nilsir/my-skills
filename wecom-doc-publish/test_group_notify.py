import inspect
import getpass
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, Mock

import publisher
from test_publisher import FakeCli, TAPD, PNG


SECRET_URL = 'https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=private-test-secret'


class NotificationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.source = self.root / 'design.md'
        self.source.write_text('# Example\nBody\n')
        self.secret = self.root / 'private' / 'webhook'
        self.secret.parent.mkdir(mode=0o700)
        self.secret.write_text(SECRET_URL)
        self.secret.chmod(0o600)
        self.config = {'state_path': str(self.root / 'state.json'), 'projects': {
            p: {'repo_root': str(self.root), 'maintainers': ['Alice'], 'readers': ['Bob']}
            for p in ('api', 'admin', 'mall')}, 'group_notification': {
                'enabled': True, 'webhook_file': str(self.secret)}}
        self.cli = FakeCli()
        self.sent = []

    def app(self, sender=None):
        def send(url, payload):
            # The marker must have reached disk BEFORE a request can start.
            sending = [e for e in self.events().values() if e['status'] == 'sending']
            self.assertEqual(len(sending), 1)
            self.assertTrue(sending[0]['publish_complete'])
            self.sent.append(payload)
            return {'errcode': 0}
        self.assertIn('notification_sender', inspect.signature(publisher.Publisher).parameters,
                      'Publisher must expose a notification sender boundary')
        return publisher.Publisher(self.config, cli=self.cli, notification_sender=sender or send)

    def events(self):
        return json.loads(Path(self.config['state_path']).read_text()).get('group_notifications', {})

    def publish(self, app=None, execute=True, **kwargs):
        return (app or self.app()).publish('api', TAPD, self.source, 'design', execute=execute, **kwargs)

    def test_first_updated_and_unchanged_across_instances(self):
        first = self.publish()
        self.assertEqual(first['notification']['status'], 'sent')
        self.assertEqual(self.publish()['notification']['status'], 'unchanged')
        self.source.write_text('# Example\nUpdated\n')
        self.assertTrue(self.publish()['changed'])
        self.assertEqual(len(self.sent), 2)
        self.assertEqual(len(self.events()), 2)
        self.assertIn('首次发布', self.sent[0]['markdown']['content'])
        self.assertIn('已更新', self.sent[1]['markdown']['content'])
        self.assertNotIn(SECRET_URL, Path(self.config['state_path']).read_text())

    def test_shared_document_one_notice_with_real_projects_links_and_permission_warning(self):
        self.cli.contacts_denied = True
        self.publish(also_projects=['mall', 'admin'])
        text = self.sent[0]['markdown']['content']
        for value in ('api', 'admin', 'mall', 'design.md', TAPD, 'https://doc.weixin.qq.com/smartpage/', '权限'):
            self.assertIn(value, text)
        self.assertEqual(len(self.sent), 1)
        self.assertNotIn('Body', text)
        self.assertNotIn('@', text)

    def test_asset_change_and_a_b_a_each_new_event(self):
        image = self.root / 'image.png'
        image.write_bytes(PNG)
        a = '# Design\n![x](image.png)\n'
        self.source.write_text(a)
        self.publish()
        image.write_bytes(PNG + b'new')
        self.publish()
        self.source.write_text('# B')
        self.publish()
        self.source.write_text(a)
        self.publish()
        self.assertEqual(len(self.sent), 4)
        self.assertEqual(len(self.events()), 4)

    def test_dry_init_sync_read_and_disabled_never_send(self):
        app = self.app()
        self.publish(app, execute=False)
        app.init(execute=True)
        app.sync_members(execute=True)
        self.assertEqual(self.events(), {})
        self.config['group_notification']['enabled'] = False
        result = self.publish(app)
        app.read(result['url'])
        self.assertEqual(self.events(), {})
        self.assertEqual(self.sent, [])

    def test_missing_secret_is_successful_doc_and_no_backfill(self):
        self.secret.unlink()
        result = self.publish()
        self.assertEqual(result['status'], 'published')
        self.assertEqual(result['notification']['status'], 'not_configured')
        self.secret.write_text(SECRET_URL)
        self.secret.chmod(0o600)
        self.publish()
        self.assertEqual(self.sent, [])
        event_id = next(iter(self.events()))
        before = Path(self.config['state_path']).read_bytes()
        self.assertEqual(self.app().notify_retry(event_id)['status'], 'dry_run')
        self.assertEqual(Path(self.config['state_path']).read_bytes(), before)
        self.assertEqual(self.app().notify_retry(event_id, execute=True)['status'], 'sent')

    def test_rejected_timeout_and_crash_do_not_automatically_retry(self):
        for outcome, expected in [('rejected', 'rejected'), ('timeout', 'unknown'), ('crash', 'sending')]:
            with self.subTest(outcome=outcome):
                self.source.write_text('# ' + outcome)
                def send(url, payload):
                    if outcome == 'timeout':
                        raise TimeoutError(SECRET_URL)
                    if outcome == 'crash':
                        raise KeyboardInterrupt()
                    return {'errcode': 93000, 'errmsg': SECRET_URL}
                if outcome == 'crash':
                    with self.assertRaises(KeyboardInterrupt):
                        self.publish(self.app(send))
                else:
                    self.assertEqual(self.publish(self.app(send))['notification']['status'], expected)
                event_id = list(self.events())[-1]
                self.assertEqual(self.events()[event_id]['status'], expected)
                self.publish()
                self.assertEqual(self.sent, [])
                if expected in ('unknown', 'sending'):
                    with self.assertRaises(publisher.PublisherError):
                        self.app().notify_retry(event_id, execute=True)
        self.assertNotIn('private-test-secret', Path(self.config['state_path']).read_text())

    def test_retry_rejects_sent_and_stale_hash_and_incomplete(self):
        self.publish()
        sent = next(iter(self.events()))
        with self.assertRaises(publisher.PublisherError):
            self.app().notify_retry(sent, execute=True)
        self.secret.unlink()
        self.source.write_text('# B')
        self.publish()
        stale = list(self.events())[-1]
        self.source.write_text('# C')
        self.publish()
        with self.assertRaises(publisher.PublisherError):
            self.app().notify_retry(stale, execute=True)

    def test_retry_rejects_current_document_with_uncertain_newer_body_write(self):
        rejected = self.publish(self.app(lambda url, payload: {'errcode': 93000}))
        event_id = rejected['notification']['event_id']
        self.source.write_text('# Newer body B')
        self.cli.uncertain_write = True
        with self.assertRaises(publisher.CliError):
            self.publish()
        state_path = Path(self.config['state_path'])
        before = state_path.read_bytes()
        for execute in (False, True):
            with self.subTest(execute=execute):
                with self.assertRaises(publisher.PublisherError):
                    self.app().notify_retry(event_id, execute=execute)
                self.assertEqual(state_path.read_bytes(), before)
        self.assertEqual(self.sent, [])

    def test_explicit_rejected_retry_sends_once_and_old_a_b_a_event_is_refused(self):
        original = self.source.read_text()
        result = self.publish(self.app(lambda url, payload: {'errcode': 93000, 'errmsg': SECRET_URL}))
        first = result['notification']['event_id']
        self.assertEqual(result['notification']['status'], 'rejected')
        self.assertEqual(self.app().notify_retry(first, execute=True)['status'], 'sent')
        with self.assertRaises(publisher.PublisherError):
            self.app().notify_retry(first, execute=True)
        self.secret.unlink()
        self.source.write_text('# B')
        self.publish()
        self.source.write_text(original)
        self.publish()
        latest = list(self.events())[-1]
        state = json.loads(Path(self.config['state_path']).read_text())
        # Reproduce a formerly rejected old A event with the SAME hash as current A.
        state['group_notifications'][first]['status'] = 'rejected'
        Path(self.config['state_path']).write_text(json.dumps(state))
        self.assertEqual(self.events()[first]['hash'], self.events()[latest]['hash'])
        with self.assertRaises(publisher.PublisherError):
            self.app().notify_retry(first, execute=True)
        self.assertEqual(len(self.sent), 1)

    def test_failed_body_and_failed_index_never_send_and_recovery_requires_retry(self):
        app = self.app()
        original = app._write
        def fail_body(doc, content, directory):
            if ' · design · ' in doc['title']:
                raise publisher.PublisherError('body failed')
            return original(doc, content, directory)
        with patch.object(app, '_write', side_effect=fail_body):
            with self.assertRaises(publisher.PublisherError):
                self.publish(app)
        self.assertEqual(self.events(), {})
        with patch.object(app, '_render_indexes', side_effect=publisher.PublisherError('index failed')):
            with self.assertRaises(publisher.PublisherError):
                self.publish(app)
        event_id = next(iter(self.events()))
        self.assertFalse(self.events()[event_id]['publish_complete'])
        with self.assertRaises(publisher.PublisherError):
            app.notify_retry(event_id, execute=True)
        self.assertEqual(self.publish(app)['status'], 'unchanged')
        self.assertTrue(self.events()[event_id]['publish_complete'])
        self.assertEqual(self.sent, [])
        self.assertEqual(app.notify_retry(event_id, execute=True)['status'], 'sent')

    def test_invalid_secret_url_is_redacted_and_does_not_fail_document(self):
        self.secret.write_text('https://evil.example/?key=private-test-secret')
        result = self.publish()
        self.assertEqual(result['notification']['status'], 'not_configured')
        self.assertNotIn('private-test-secret', json.dumps(result))
        self.assertEqual(self.sent, [])

    def test_configure_refuses_getpass_echo_fallback_before_input_or_save(self):
        app = self.app()
        # Simulate a TTY whose terminal control cannot disable echo.
        with patch('sys.stdin.isatty', return_value=True), \
                patch('getpass.os.open', side_effect=OSError('terminal unavailable')), \
                patch('getpass.termios.tcgetattr', side_effect=getpass.termios.error('no terminal control')), \
                patch('getpass._raw_input', return_value=SECRET_URL) as raw_input, \
                patch('group_notify.save_webhook') as save:
            with self.assertRaises(publisher.PublisherError):
                app.configure_group(execute=True)
            raw_input.assert_not_called()
            save.assert_not_called()
        self.assertEqual(self.sent, [])

    def test_configure_hidden_input_and_permissions_no_send(self):
        self.secret.unlink()
        app = self.app()
        with patch('sys.stdin.isatty', return_value=True), patch('getpass.getpass', return_value=SECRET_URL) as hidden:
            self.assertEqual(app.configure_group()['status'], 'dry_run')
            hidden.assert_not_called()
            self.assertEqual(app.configure_group(execute=True)['status'], 'configured')
        self.assertEqual(self.secret.read_text().strip(), SECRET_URL)
        self.assertEqual(self.secret.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.secret.parent.stat().st_mode & 0o777, 0o700)
        self.assertEqual(self.sent, [])
        with patch('sys.stdin.isatty', return_value=False):
            with self.assertRaises(publisher.PublisherError):
                app.configure_group(execute=True)


class TransportTests(unittest.TestCase):
    def module(self):
        import group_notify
        return group_notify

    def test_url_only_standard_endpoint_and_exact_nonempty_key(self):
        mod = self.module()
        self.assertEqual(mod.validate_webhook(SECRET_URL), SECRET_URL)
        for bad in ['http://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=x',
                    SECRET_URL + '&extra=x', SECRET_URL + '#fragment', SECRET_URL + '&key=x', SECRET_URL + '&',
                    SECRET_URL.replace('qyapi.', 'user@qyapi.'), SECRET_URL.replace('?key=private-test-secret', '?key='),
                    SECRET_URL.replace('qyapi.weixin.qq.com', 'qyapi.weixin.qq.com:443')]:
            with self.subTest(bad=bad), self.assertRaises(ValueError) as error:
                mod.validate_webhook(bad)
            self.assertNotIn('private-test-secret', str(error.exception))

    def test_http_payload_timeout_and_redirect_disabled(self):
        mod = self.module()
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.read.return_value = b'{"errcode":0}'
        opener = Mock()
        opener.open.return_value = response
        with patch.object(mod.urllib.request, 'build_opener', return_value=opener) as build:
            self.assertEqual(mod.send(SECRET_URL, {'msgtype': 'markdown', 'markdown': {'content': '你好'}}), {'errcode': 0})
        request = opener.open.call_args.args[0]
        self.assertEqual(request.get_method(), 'POST')
        self.assertEqual(json.loads(request.data)['markdown']['content'], '你好')
        self.assertLessEqual(opener.open.call_args.kwargs['timeout'], 15)
        handler = build.call_args.args[0]
        self.assertIsNone(handler.redirect_request(None, None, 302, '', {}, SECRET_URL))
        self.assertEqual(opener.open.call_count, 1)

    def test_network_and_invalid_json_exceptions_never_expose_secret(self):
        mod = self.module()
        opener = Mock()
        opener.open.side_effect = OSError(SECRET_URL)
        with patch.object(mod.urllib.request, 'build_opener', return_value=opener):
            with self.assertRaises(Exception) as error:
                mod.send(SECRET_URL, {'msgtype': 'markdown', 'markdown': {'content': 'test'}})
        self.assertIsInstance(error.exception, ValueError)
        self.assertNotIn('private-test-secret', str(error.exception))
        self.assertIsNone(error.exception.__cause__)

    def test_only_integer_zero_success_and_size_limit(self):
        mod = self.module()
        for response in ({'errcode': '0'}, {'errcode': False}, {}, [], None, {'errcode': 0.0}):
            self.assertEqual(mod.response_status(response), 'unknown')
        self.assertEqual(mod.response_status({'errcode': 0}), 'sent')
        self.assertEqual(mod.response_status({'errcode': 1, 'errmsg': SECRET_URL}), 'rejected')
        with patch.object(mod.urllib.request, 'build_opener') as build:
            with self.assertRaises(ValueError):
                mod.send(SECRET_URL, {'msgtype': 'markdown', 'markdown': {'content': '中' * 1366}})
            build.assert_not_called()


if __name__ == '__main__':
    unittest.main()
