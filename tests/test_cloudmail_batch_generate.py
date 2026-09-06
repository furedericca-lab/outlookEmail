import os
import tempfile
import unittest
from unittest.mock import patch


if 'DATABASE_PATH' not in os.environ:
    _temp_dir = tempfile.mkdtemp(prefix='outlookEmail-cloudmail-batch-')
    os.environ['DATABASE_PATH'] = os.path.join(_temp_dir, 'test.db')
if 'SECRET_KEY' not in os.environ:
    os.environ['SECRET_KEY'] = 'test-secret-key'

import web_outlook_app


class ContextTestCase(unittest.TestCase):
    def setUp(self):
        super().setUp()
        context = web_outlook_app.app.app_context()
        context.push()
        self.addCleanup(context.pop)
        web_outlook_app.set_setting('cloudmail_enabled', 'true')
        web_outlook_app.set_setting('cloudmail_domain', 'mail.example')


def _fake_creator(prefix='bx', fail_usernames=()):
    calls = []

    def create(username=None, domain=None):
        name = username or ('%s%d' % (prefix, len(calls) + 1))
        calls.append((name, domain))
        if name in fail_usernames:
            return {'success': False, 'error': '上游拒绝: %s' % name}
        return {'success': True, 'address': '%s@%s' % (name, domain or 'mail.example'),
                'password': 'pw-' + name}

    return calls, create


class CloudmailBatchValidationTests(ContextTestCase):
    """Batch input is validated before a single upstream call is made."""

    def test_count_bounds_are_enforced(self):
        calls, create = _fake_creator()
        with patch.object(web_outlook_app, 'cloudmail_create_address', side_effect=create):
            for bad_count in (0, 51, 'abc', None):
                with self.subTest(count=bad_count):
                    result = web_outlook_app.generate_cloudmail_temp_emails_batch({'count': bad_count})
                    self.assertFalse(result['success'])
                    self.assertIn('1-50', result['error'])
        self.assertEqual(calls, [], 'validation must happen before touching the upstream')

    def test_provided_usernames_must_match_the_count(self):
        result = web_outlook_app.generate_cloudmail_temp_emails_batch(
            {'count': 3, 'usernames': ['aa1', 'bb2']})
        self.assertFalse(result['success'])
        self.assertIn('一致', result['error'])

    def test_illegal_username_is_rejected_before_creation(self):
        calls, create = _fake_creator()
        with patch.object(web_outlook_app, 'cloudmail_create_address', side_effect=create):
            result = web_outlook_app.generate_cloudmail_temp_emails_batch(
                {'count': 2, 'usernames': ['good1', 'Bad Name!']})
        self.assertFalse(result['success'])
        self.assertIn('不合法', result['error'])
        self.assertEqual(calls, [])

    def test_disabled_provider_never_reaches_the_upstream(self):
        web_outlook_app.set_setting('cloudmail_enabled', 'false')
        calls, create = _fake_creator()
        with patch.object(web_outlook_app, 'cloudmail_create_address', side_effect=create):
            result = web_outlook_app.generate_cloudmail_temp_emails_batch({'count': 2})
        self.assertFalse(result['success'])
        self.assertEqual(calls, [])


class CloudmailBatchCreateTests(ContextTestCase):
    """The response shape must match the Cloudflare batch so the UI needs no second path."""

    def test_random_batch_creates_rows_that_we_may_delete_upstream(self):
        calls, create = _fake_creator()
        with patch.object(web_outlook_app, 'cloudmail_create_address', side_effect=create):
            result = web_outlook_app.generate_cloudmail_temp_emails_batch({'count': 3, 'domain': ''})

        self.assertTrue(result['success'], result)
        self.assertEqual(result['created_count'], 3)
        self.assertEqual(result['failed_count'], 0)
        self.assertEqual(result['failures'], [])
        self.assertIn('tagged_count', result)
        self.assertIn('emails', result)
        for key in ('success', 'emails', 'created_count', 'failed_count', 'failures', 'tagged_count'):
            self.assertIn(key, result, 'batch response keys must mirror the Cloudflare batch')
        # domain 留空要落到配置里的收信域，而不是把空串发给上游。
        self.assertEqual({domain for _, domain in calls}, {'mail.example'})
        for address in result['emails']:
            stored = web_outlook_app.get_temp_email_by_address(address)
            self.assertEqual(stored['provider'], 'cloudmail')
            self.assertTrue(stored.get('cloudmail_password'),
                            'batch-created mailboxes are ours, so they must carry a credential')

    def test_custom_usernames_are_used_in_order(self):
        calls, create = _fake_creator()
        with patch.object(web_outlook_app, 'cloudmail_create_address', side_effect=create):
            result = web_outlook_app.generate_cloudmail_temp_emails_batch(
                {'count': 2, 'domain': 'mail.example', 'usernames': ['zeta9', 'alpha1']})
        self.assertEqual([name for name, _ in calls], ['zeta9', 'alpha1'])
        self.assertEqual(result['emails'], ['zeta9@mail.example', 'alpha1@mail.example'])

    def test_partial_failure_is_reported_per_item_not_swallowed(self):
        calls, create = _fake_creator(fail_usernames=('bad9',))
        with patch.object(web_outlook_app, 'cloudmail_create_address', side_effect=create):
            result = web_outlook_app.generate_cloudmail_temp_emails_batch(
                {'count': 3, 'usernames': ['good1', 'bad9', 'good2']})
        self.assertTrue(result['success'])
        self.assertEqual(result['created_count'], 2)
        self.assertEqual(result['failed_count'], 1)
        self.assertEqual(result['failures'][0]['username'], 'bad9')
        self.assertEqual(result['failures'][0]['index'], 2)
        self.assertIn('上游拒绝', result['failures'][0]['error'])

    def test_total_failure_reports_the_first_error(self):
        def refuse(*args, **kwargs):
            return {'success': False, 'error': '域名不在服务端配置里'}

        with patch.object(web_outlook_app, 'cloudmail_create_address', side_effect=refuse):
            result = web_outlook_app.generate_cloudmail_temp_emails_batch({'count': 2})
        self.assertFalse(result['success'])
        self.assertEqual(result['created_count'], 0)
        self.assertIn('域名不在服务端配置里', result['error'])

    def test_selected_tags_are_bound_to_the_created_batch(self):
        seen = {}

        def fake_bind(ids, tag_ids):
            seen['ids'] = list(ids)
            seen['tag_ids'] = list(tag_ids or [])
            return len(ids)

        tag_calls, tag_create = _fake_creator(prefix='tgbind')
        with patch.object(web_outlook_app, 'cloudmail_create_address', side_effect=tag_create), \
                patch.object(web_outlook_app, 'bind_temp_email_tags', side_effect=fake_bind):
            result = web_outlook_app.generate_cloudmail_temp_emails_batch(
                {'count': 2, 'tag_ids': [7, 9]})

        self.assertEqual(sorted(seen['tag_ids']), [7, 9])
        self.assertEqual(len(seen['ids']), 2)
        self.assertEqual(result['tagged_count'], 2)


class CloudmailBatchRouteTests(unittest.TestCase):
    def setUp(self):
        self.app = web_outlook_app.app
        self.app.config['TESTING'] = True
        self.app.config['WTF_CSRF_ENABLED'] = False
        self.client = self.app.test_client()
        with self.client.session_transaction() as sess:
            sess['logged_in'] = True

    def tearDown(self):
        self.app.config['TESTING'] = False

    def test_the_batch_endpoint_accepts_the_cloudmail_provider(self):
        with patch.object(web_outlook_app, 'generate_cloudmail_temp_emails_batch',
                          return_value={'success': True, 'emails': ['x@mail.example'],
                                        'created_count': 1, 'failed_count': 0,
                                        'failures': [], 'tagged_count': 0}) as handler:
            response = self.client.post('/api/temp-emails/generate-batch',
                                        json={'provider': 'cloudmail', 'count': 1}).get_json()
        self.assertTrue(response['success'])
        self.assertTrue(handler.called)

    def test_unknown_providers_are_still_refused(self):
        response = self.client.post('/api/temp-emails/generate-batch',
                                    json={'provider': 'carrier-pigeon', 'count': 1}).get_json()
        self.assertFalse(response['success'])
        self.assertIn('cloud-mail', response['error'])


if __name__ == '__main__':
    unittest.main()
