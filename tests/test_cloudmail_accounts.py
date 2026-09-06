import os
import tempfile
import unittest
from unittest.mock import patch


if 'DATABASE_PATH' not in os.environ:
    _temp_dir = tempfile.mkdtemp(prefix='outlookEmail-cloudmail-accounts-')
    os.environ['DATABASE_PATH'] = os.path.join(_temp_dir, 'test.db')
if 'SECRET_KEY' not in os.environ:
    os.environ['SECRET_KEY'] = 'test-secret-key'

import web_outlook_app


class _Response:
    def __init__(self, payload=None, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.text = ''
        self.headers = {'content-type': 'application/json'}

    def json(self):
        if self._payload is None:
            raise ValueError('not JSON')
        return self._payload


def _envelope(data, code=200, message='success'):
    return {'code': code, 'message': message, 'data': data}


class ContextTestCase(unittest.TestCase):
    def setUp(self):
        super().setUp()
        context = web_outlook_app.app.app_context()
        context.push()
        self.addCleanup(context.pop)
        web_outlook_app.set_setting('cloudmail_enabled', 'true')
        web_outlook_app.set_setting('cloudmail_base_url', 'https://mail-api.invalid')
        web_outlook_app.set_setting('cloudmail_domain', 'mail.example')
        web_outlook_app.set_setting('cloudmail_api_prefix', '/api')
        web_outlook_app.set_setting('cloudmail_admin_email', 'admin@mail.example')
        web_outlook_app.set_setting_encrypted('cloudmail_admin_password', 'admin-password-1')
        web_outlook_app.set_setting_encrypted('cloudmail_admin_token', 'admin-jwt')
        web_outlook_app.set_setting_encrypted('cloudmail_public_token', 'public-token')


class CloudmailAccountListTests(ContextTestCase):
    """cloud-mail stores login identities in `user` and mailboxes in `account`.

    Listing the wrong table is what produced a confident but false conclusion during a
    live diagnosis - "the instance has one admin and no mailboxes" while the operator
    looked at thirty mailboxes - so the shape of that call is asserted here.
    """

    def test_the_account_table_is_listed_not_the_login_table(self):
        seen = {}

        def fake_get(url, headers=None, params=None, timeout=None):
            seen['url'] = url
            seen['params'] = params
            return _Response(_envelope([
                {'accountId': 1, 'email': 'Box01@Mail.Example', 'name': 'Box01', 'status': 0,
                 'allReceive': 0, 'latestEmailTime': '', 'createTime': '2026-07-22 17:32:04'},
            ]))

        with patch.object(web_outlook_app.requests, 'get', side_effect=fake_get):
            result = web_outlook_app.cloudmail_list_accounts(1, 200)

        self.assertTrue(result['success'], result)
        self.assertIn('/account/list', seen['url'])
        self.assertNotIn('/user/list', seen['url'])
        self.assertEqual(seen['params']['size'], 200)
        account = result['accounts'][0]
        self.assertEqual(account['email'], 'box01@mail.example')
        self.assertFalse(account['attached'])
        self.assertFalse(account['created_by_us'])

    def test_local_state_is_reported_per_account(self):
        web_outlook_app.add_temp_email('mine@mail.example', provider='cloudmail',
                                       cloudmail_password='generated-pw')
        web_outlook_app.add_temp_email('imported@mail.example', provider='cloudmail')
        rows = [{'accountId': i, 'email': email} for i, email in
                enumerate(['mine@mail.example', 'imported@mail.example', 'other@mail.example'], 1)]

        with patch.object(web_outlook_app.requests, 'get',
                          return_value=_Response(_envelope(rows))):
            accounts = web_outlook_app.cloudmail_list_accounts()['accounts']

        flags = {row['email']: (row['attached'], row['created_by_us']) for row in accounts}
        self.assertEqual(flags['mine@mail.example'], (True, True))
        # An imported mailbox is attached but must never be treated as ours to delete.
        self.assertEqual(flags['imported@mail.example'], (True, False))
        self.assertEqual(flags['other@mail.example'], (False, False))

    def test_listing_requires_the_provider_to_be_enabled(self):
        web_outlook_app.set_setting('cloudmail_enabled', 'false')
        with patch.object(web_outlook_app.requests, 'get') as get:
            result = web_outlook_app.cloudmail_list_accounts()
        self.assertFalse(result['success'])
        get.assert_not_called()


class CloudmailAttachTests(ContextTestCase):
    """Attaching is a local-only act: no credential is generated, stored, or sent."""

    def test_attach_stores_no_credential_and_touches_no_upstream_endpoint(self):
        with patch.object(web_outlook_app.requests, 'post') as post, \
                patch.object(web_outlook_app.requests, 'get') as get:
            result = web_outlook_app.cloudmail_attach_address('import1@mail.example')
            post.assert_not_called()
            get.assert_not_called()

        self.assertTrue(result['success'], result)
        self.assertFalse(result.get('created_by_us'))
        stored = web_outlook_app.get_temp_email_by_address('import1@mail.example')
        self.assertEqual(stored['provider'], 'cloudmail')
        self.assertFalse(stored.get('cloudmail_password'),
                         'an attached mailbox must not carry a credential we never obtained')

    def test_attach_is_idempotent(self):
        first = web_outlook_app.cloudmail_attach_address('same@mail.example')
        second = web_outlook_app.cloudmail_attach_address('same@mail.example')
        self.assertTrue(first['success'])
        self.assertTrue(second['already_attached'])

    def test_attach_refuses_a_domain_the_provider_was_not_configured_for(self):
        result = web_outlook_app.cloudmail_attach_address('stranger@somewhere-else.test')
        self.assertFalse(result['success'])
        self.assertIn('mail.example', result['error'])
        self.assertIsNone(web_outlook_app.get_temp_email_by_address('stranger@somewhere-else.test'))

    def test_attach_refuses_an_address_already_owned_by_another_provider(self):
        web_outlook_app.add_temp_email('taken@mail.example', provider='gptmail')
        result = web_outlook_app.cloudmail_attach_address('taken@mail.example')
        self.assertFalse(result['success'])
        self.assertIn('其它提供商', result['error'])

    def test_malformed_addresses_are_rejected(self):
        for value in ('', 'nope', '   '):
            with self.subTest(value=value):
                self.assertFalse(web_outlook_app.cloudmail_attach_address(value)['success'])


class CloudmailDeleteSemanticsTests(ContextTestCase):
    """Who created the mailbox decides whether cloud-mail is allowed to be touched."""

    def _cleanup(self, email_addr):
        temp_email = web_outlook_app.get_temp_email_by_address(email_addr)
        with patch.object(web_outlook_app, 'cloudmail_delete_address') as delete:
            web_outlook_app.cleanup_temp_email_provider_resource(temp_email)
        return delete

    def test_an_attached_mailbox_is_never_deleted_upstream(self):
        web_outlook_app.cloudmail_attach_address('keepme@mail.example')
        delete = self._cleanup('keepme@mail.example')
        delete.assert_not_called()

    def test_a_mailbox_we_created_is_still_deleted_upstream(self):
        web_outlook_app.add_temp_email('ourown@mail.example', provider='cloudmail',
                                       cloudmail_password='generated-pw')
        delete = self._cleanup('ourown@mail.example')
        delete.assert_called_once_with('ourown@mail.example')


class CloudmailAccountRouteTests(unittest.TestCase):
    def setUp(self):
        self.app = web_outlook_app.app
        self.app.config['TESTING'] = True
        self.app.config['WTF_CSRF_ENABLED'] = False
        self.client = self.app.test_client()

    def tearDown(self):
        self.app.config['TESTING'] = False

    def test_routes_require_a_session(self):
        with self.subTest('accounts'):
            self.assertEqual(self.client.get('/api/cloudmail/accounts').status_code, 401)
        with self.subTest('attach'):
            response = self.client.post('/api/cloudmail/attach', json={'email': 'x@mail.example'})
            self.assertEqual(response.status_code, 401)

    def test_attach_through_the_interface_shows_up_as_a_cloudmail_mailbox(self):
        with self.client.session_transaction() as sess:
            sess['logged_in'] = True

        response = self.client.post('/api/cloudmail/attach',
                                    json={'emails': ['viaui@mail.example', 'bad@nope.test']})
        payload = response.get_json()
        self.assertTrue(payload['success'])
        self.assertEqual(payload['attached_count'], 1)
        self.assertEqual(payload['failed'][0]['email'], 'bad@nope.test')

        listing = self.client.get('/api/temp-emails').get_json()
        rows = listing.get('emails') or listing.get('temp_emails') or []
        match = [row for row in rows
                 if str(row.get('email', '')).lower() == 'viaui@mail.example']
        self.assertTrue(match, 'the attached mailbox must show up in the mailbox list')
        self.assertEqual(match[0]['provider'], 'cloudmail')


if __name__ == '__main__':
    unittest.main()
