import os
import tempfile
import unittest
from unittest.mock import patch


if 'DATABASE_PATH' not in os.environ:
    _temp_dir = tempfile.mkdtemp(prefix='outlookEmail-cloudmail-')
    os.environ['DATABASE_PATH'] = os.path.join(_temp_dir, 'test.db')
if 'SECRET_KEY' not in os.environ:
    os.environ['SECRET_KEY'] = 'test-secret-key'

import web_outlook_app


class _Response:
    """Minimal stand-in for requests.Response: cloud-mail always answers HTTP 200."""

    def __init__(self, payload=None, status_code=200, text=None):
        self._payload = payload
        self.status_code = status_code
        self.text = text if text is not None else ''
        self.headers = {'content-type': 'application/json'}

    def json(self):
        if self._payload is None:
            raise ValueError('not JSON')
        return self._payload


def _envelope(data=None, code=200, message='success'):
    return {'code': code, 'message': message, 'data': data}


class ContextTestCase(unittest.TestCase):
    """Settings and the database are request-scoped, so tests need an app context."""

    def setUp(self):
        super().setUp()
        context = web_outlook_app.app.app_context()
        context.push()
        self.addCleanup(context.pop)


class CloudmailRequestTestCase(ContextTestCase):
    """The adapter must judge success by the response envelope, never by HTTP status."""

    def setUp(self):
        super().setUp()
        web_outlook_app.set_setting('cloudmail_base_url', 'https://mail-api.invalid')
        web_outlook_app.set_setting('cloudmail_api_prefix', '/api')

    def test_rejects_a_page_instead_of_json(self):
        """Filling in the web UI address instead of the API address is the classic mistake.

        That host answers HTTP 200 with an HTML SPA shell, so the error has to say
        what to check instead of the old opaque "not valid JSON".
        """
        page = '<!doctype html><html lang="en"><head><meta charset="UTF-8"></head></html>'

        with patch.object(web_outlook_app.requests, 'post',
                          return_value=_Response(None, 200, page)):
            result = web_outlook_app.cloudmail_request('POST', '/public/emailList')

        self.assertFalse(result['success'])
        self.assertIn('不是 JSON', result['error'])
        self.assertIn('接口地址', result['error'])

    def test_business_failure_is_taken_from_the_envelope_although_http_is_200(self):
        with patch.object(web_outlook_app.requests, 'post',
                          return_value=_Response(_envelope(code=401, message='token验证失败'))):
            result = web_outlook_app.cloudmail_request('POST', '/public/emailList')

        self.assertFalse(result['success'])
        self.assertEqual(result['error'], 'token验证失败')

    def test_success_returns_the_envelope_data(self):
        with patch.object(web_outlook_app.requests, 'post',
                          return_value=_Response(_envelope({'token': 'tok-abc'}))):
            result = web_outlook_app.cloudmail_request('POST', '/public/genToken')

        self.assertTrue(result['success'])
        self.assertEqual(result['data'], {'token': 'tok-abc'})

    def test_missing_base_url_is_reported_without_any_request(self):
        web_outlook_app.set_setting('cloudmail_base_url', '')

        with patch.object(web_outlook_app.requests, 'post') as post:
            result = web_outlook_app.cloudmail_request('POST', '/public/emailList')

        self.assertFalse(result['success'])
        self.assertIn('未配置', result['error'])
        post.assert_not_called()

    def test_network_error_is_reported_as_a_clear_failure(self):
        with patch.object(web_outlook_app.requests, 'post', side_effect=OSError('dns down')):
            result = web_outlook_app.cloudmail_request('POST', '/public/addUser')

        self.assertFalse(result['success'])
        self.assertIn('dns down', result['error'])


class CloudmailTokenTestCase(ContextTestCase):
    def setUp(self):
        super().setUp()
        web_outlook_app.set_setting('cloudmail_base_url', 'https://mail-api.invalid')
        web_outlook_app.set_setting('cloudmail_admin_email', 'admin@invalid')
        web_outlook_app.set_setting_encrypted('cloudmail_admin_password', 'hunter2hunter2')
        web_outlook_app.set_setting_encrypted('cloudmail_public_token', '')

    def test_token_is_fetched_once_then_read_from_the_settings_cache(self):
        with patch.object(web_outlook_app.requests, 'post',
                          return_value=_Response(_envelope({'token': 'tok-abc'}))) as post:
            first = web_outlook_app.get_cloudmail_public_token()
            second = web_outlook_app.get_cloudmail_public_token()

        self.assertEqual(first, 'tok-abc')
        self.assertEqual(second, 'tok-abc')
        self.assertEqual(post.call_count, 1, 'the cached token must avoid a second round trip')

    def test_token_and_password_never_appear_in_the_settings_view(self):
        with patch.object(web_outlook_app.requests, 'post',
                          return_value=_Response(_envelope({'token': 'tok-abc'}))):
            web_outlook_app.get_cloudmail_public_token()

        view = web_outlook_app.cloudmail_settings_payload()
        self.assertTrue(view['public_token_configured'])
        self.assertTrue(view['admin_password_configured'])
        self.assertNotIn('admin_password', view)
        self.assertNotIn('public_token', view)
        self.assertNotIn('tok-abc', str(view))

    def test_missing_admin_credentials_short_circuit_without_a_request(self):
        web_outlook_app.set_setting('cloudmail_admin_email', '')

        with patch.object(web_outlook_app.requests, 'post') as post:
            self.assertIsNone(web_outlook_app.get_cloudmail_public_token(force_refresh=True))

        post.assert_not_called()


class CloudmailAddressTestCase(ContextTestCase):
    def setUp(self):
        super().setUp()
        web_outlook_app.set_setting('cloudmail_base_url', 'https://mail-api.invalid')
        web_outlook_app.set_setting('cloudmail_domain', 'mail.example')
        web_outlook_app.set_setting_encrypted('cloudmail_public_token', 'tok-abc')

    def test_created_address_carries_a_password_the_caller_can_store(self):
        with patch.object(web_outlook_app.requests, 'post',
                          return_value=_Response(_envelope(None))) as post:
            result = web_outlook_app.cloudmail_create_address(username='reader01', domain='mail.example')

        self.assertTrue(result['success'])
        self.assertEqual(result['address'], 'reader01@mail.example')
        self.assertGreaterEqual(len(result['password']), 8)

        sent = post.call_args.kwargs.get('json')
        self.assertEqual(list(sent['list'])[0]['email'], 'reader01@mail.example')
        self.assertEqual(post.call_args.args[0], 'https://mail-api.invalid/api/public/addUser')

    def test_rejects_an_unusable_local_part_before_calling_the_service(self):
        with patch.object(web_outlook_app.requests, 'post') as post:
            result = web_outlook_app.cloudmail_create_address(username='a b!', domain='mail.example')

        self.assertFalse(result['success'])
        self.assertIn('用户名', result['error'])
        post.assert_not_called()

    def test_domain_error_from_the_service_tells_the_operator_where_to_look(self):
        failure = _Response(_envelope(code=500, message='notEmailDomain'))
        with patch.object(web_outlook_app.requests, 'post', return_value=failure):
            result = web_outlook_app.cloudmail_create_address(username='reader02', domain='nope.invalid')

        self.assertFalse(result['success'])
        self.assertIn('domain', result['error'])

    def test_missing_domain_is_a_configuration_error(self):
        web_outlook_app.set_setting('cloudmail_domain', '')

        with patch.object(web_outlook_app.requests, 'post') as post:
            result = web_outlook_app.cloudmail_create_address(username='reader03')

        self.assertFalse(result['success'])
        self.assertIn('收信域名', result['error'])
        post.assert_not_called()


class CloudmailMessageTestCase(ContextTestCase):
    def test_rows_are_mapped_onto_the_shared_temp_mail_shape(self):
        rows = [{
            'emailId': 41,
            'sendEmail': 'noreply@site.invalid',
            'subject': 'Your code',
            'content': '<p>Use <b>123456</b></p>',
            'text': '',
            'createTime': '2026-09-06 10:20:30',
        }]

        messages = web_outlook_app.cloudmail_normalize_messages('reader01@mail.example', rows)

        self.assertEqual(len(messages), 1)
        message = messages[0]
        self.assertEqual(message['id'], '41')
        self.assertEqual(message['from_address'], 'noreply@site.invalid')
        self.assertEqual(message['subject'], 'Your code')
        self.assertTrue(message['has_html'])
        self.assertIn('<b>123456</b>', message['html_content'])
        self.assertIn('123456', message['content'])
        self.assertGreater(message['timestamp'], 0)

    def test_plain_text_rows_keep_the_text_and_report_no_html(self):
        messages = web_outlook_app.cloudmail_normalize_messages('a@mail.example', [{
            'emailId': 42, 'sendEmail': 'x@site.invalid', 'subject': 'plain',
            'content': 'no tags here', 'text': 'no tags here', 'createTime': 1700000000,
        }])

        self.assertFalse(messages[0]['has_html'])
        self.assertEqual(messages[0]['content'], 'no tags here')
        self.assertEqual(messages[0]['timestamp'], 1700000000)

    def test_fetch_requires_a_token_and_reports_a_useful_failure(self):
        web_outlook_app.set_setting_encrypted('cloudmail_public_token', '')
        web_outlook_app.set_setting('cloudmail_admin_email', '')

        with patch.object(web_outlook_app.requests, 'post') as post:
            result = web_outlook_app.fetch_cloudmail_temp_messages('reader01@mail.example', None)

        self.assertFalse(result['success'])
        self.assertIn('令牌', result['error'])
        post.assert_not_called()


class CloudmailDeleteTestCase(ContextTestCase):
    """Deleting upstream is a hard delete in cloud-mail, so lookup must be exact."""

    def setUp(self):
        super().setUp()
        web_outlook_app.set_setting('cloudmail_base_url', 'https://mail-api.invalid')
        web_outlook_app.set_setting('cloudmail_admin_email', 'admin@mail.example')
        web_outlook_app.set_setting_encrypted('cloudmail_admin_password', 'admin-password-1')
        web_outlook_app.set_setting_encrypted('cloudmail_admin_token', 'admin-jwt')

    @staticmethod
    def _user_rows(*rows):
        return _Response(_envelope({'list': [{'userId': uid, 'email': email} for uid, email in rows],
                                    'total': len(rows)}))

    def _capture_delete(self):
        calls = {}

        def fake_delete(url, headers=None, params=None, timeout=None):
            calls['url'] = url
            calls['params'] = params
            return _Response(_envelope(None))

        return calls, fake_delete

    def test_the_matching_user_id_is_resolved_and_used_for_the_delete(self):
        # /user/list matches by prefix, so a prefix neighbour must not be mistaken for a hit.
        listed = self._user_rows((8, 'reader01-extra@mail.example'), (7, 'reader01@mail.example'))
        calls, fake_delete = self._capture_delete()

        with patch.object(web_outlook_app.requests, 'get', return_value=listed), \
                patch.object(web_outlook_app.requests, 'delete', side_effect=fake_delete):
            result = web_outlook_app.cloudmail_delete_address('reader01@mail.example')

        self.assertTrue(result['success'], result)
        self.assertEqual(result['user_id'], 7)
        self.assertEqual(calls['url'], 'https://mail-api.invalid/api/user/delete')
        self.assertEqual(calls['params'], {'userIds': '7'})

    def test_a_prefix_only_match_is_refused_and_deletes_nothing(self):
        listed = self._user_rows((9, 'reader01-extra@mail.example'))

        with patch.object(web_outlook_app.requests, 'get', return_value=listed), \
                patch.object(web_outlook_app.requests, 'delete') as delete:
            result = web_outlook_app.cloudmail_delete_address('reader01@mail.example')

        self.assertFalse(result['success'])
        self.assertIn('不存在', result['error'])
        delete.assert_not_called()

    def test_ambiguous_rows_are_refused_instead_of_guessing_an_id(self):
        listed = self._user_rows((7, 'reader01@mail.example'), (8, 'Reader01@mail.example'))

        with patch.object(web_outlook_app.requests, 'get', return_value=listed), \
                patch.object(web_outlook_app.requests, 'delete') as delete:
            result = web_outlook_app.cloudmail_delete_address('reader01@mail.example')

        self.assertFalse(result['success'])
        self.assertIn('多条', result['error'])
        delete.assert_not_called()

    def test_the_admin_account_itself_is_never_deleted(self):
        listed = self._user_rows((1, 'admin@mail.example'))

        with patch.object(web_outlook_app.requests, 'get', return_value=listed), \
                patch.object(web_outlook_app.requests, 'delete') as delete:
            result = web_outlook_app.cloudmail_delete_address('admin@mail.example')

        self.assertFalse(result['success'])
        self.assertIn('管理员', result['error'])
        delete.assert_not_called()

    def test_an_expired_admin_session_relogs_in_once(self):
        expired = _Response(_envelope(code=401, message='身份认证失效,请重新登录'))
        listed = self._user_rows((7, 'reader01@mail.example'))
        responses = [expired, listed]
        seen = {'get': 0, 'login': 0}

        def fake_get(url, headers=None, params=None, timeout=None):
            seen['get'] += 1
            return responses.pop(0)

        def fake_post(url, headers=None, params=None, json=None, timeout=None):
            seen['login'] += 1
            return _Response(_envelope({'token': 'fresh-jwt'}))

        web_outlook_app.set_setting_encrypted('cloudmail_admin_token', '')

        with patch.object(web_outlook_app.requests, 'get', side_effect=fake_get), \
                patch.object(web_outlook_app.requests, 'post', side_effect=fake_post):
            found = web_outlook_app.cloudmail_find_user_id('reader01@mail.example')

        self.assertTrue(found['success'], found)
        self.assertEqual(found['user_id'], 7)
        self.assertEqual(seen['get'], 2, 'the 401 must be retried once with a fresh login')
        self.assertEqual(seen['login'], 2)

    def test_upstream_delete_failure_is_reported_to_the_caller(self):
        listed = self._user_rows((7, 'reader01@mail.example'))
        denied = _Response(_envelope(code=403, message='forbidden'))

        with patch.object(web_outlook_app.requests, 'get', return_value=listed), \
                patch.object(web_outlook_app.requests, 'delete', return_value=denied):
            result = web_outlook_app.cloudmail_delete_address('reader01@mail.example')

        self.assertFalse(result['success'])
        self.assertEqual(result['error'], 'forbidden')


class CloudmailPagingTestCase(ContextTestCase):
    """emailList only has page numbers; the adapter owes callers correct offset semantics."""

    def setUp(self):
        super().setUp()
        web_outlook_app.set_setting('cloudmail_base_url', 'https://mail-api.invalid')
        web_outlook_app.set_setting_encrypted('cloudmail_public_token', 'tok-abc')

    def _stream(self, total: int):
        return [{'emailId': i, 'sendEmail': 'svc@site.invalid', 'subject': f'msg {i}',
                 'content': f'body {i}', 'text': f'body {i}', 'createTime': 1700000000 + i}
                for i in range(total)]

    def test_an_aligned_offset_reads_that_page_directly(self):
        requested = []
        stream = self._stream(100)

        def fake_post(url, headers=None, params=None, json=None, timeout=None):
            requested.append(dict(json))
            page = int(json['num'])
            size = int(json['size'])
            return _Response(_envelope({'list': stream[(page - 1) * size:page * size], 'total': total}))

        total = len(stream)
        with patch.object(web_outlook_app.requests, 'post', side_effect=fake_post):
            result = web_outlook_app.fetch_cloudmail_temp_messages('reader01@mail.example', None,
                                                                   limit=50, offset=50)

        self.assertTrue(result['success'], result)
        self.assertEqual(requested, [{'toEmail': 'reader01@mail.example', 'num': 2, 'size': 50}])
        self.assertEqual(len(result['messages']), 50)
        self.assertEqual(result['messages'][0]['id'], '50')

    def test_an_unaligned_offset_walks_pages_and_slices_once(self):
        pages = []
        stream = self._stream(150)

        def fake_post(url, headers=None, params=None, json=None, timeout=None):
            payload = dict(json)
            pages.append(payload['num'])
            size = int(payload['size'])
            page = int(payload['num'])
            return _Response(_envelope({'list': stream[(page - 1) * size:page * size], 'total': len(stream)}))

        with patch.object(web_outlook_app.requests, 'post', side_effect=fake_post):
            result = web_outlook_app.fetch_cloudmail_temp_messages('reader01@mail.example', None,
                                                                   limit=50, offset=60)

        self.assertTrue(result['success'], result)
        self.assertEqual(len(result['messages']), 50)
        self.assertEqual(result['messages'][0]['id'], '60')
        self.assertEqual(pages[-1], 3, 'must stop once offset+limit is covered')
        self.assertLessEqual(len(pages), web_outlook_app.CLOUDMAIL_MAX_PAGES)

    def test_the_page_size_ceiling_matches_what_the_service_honours(self):
        self.assertLessEqual(web_outlook_app.CLOUDMAIL_PAGE_SIZE, 50)
        self.assertGreaterEqual(web_outlook_app.CLOUDMAIL_MAX_PAGES, 1)


class CloudmailRouteTestCase(ContextTestCase):
    def setUp(self):
        super().setUp()
        self.app = web_outlook_app.app
        self.app.config['TESTING'] = True
        self.app.config['WTF_CSRF_ENABLED'] = False
        self.client = self.app.test_client()
        with self.client.session_transaction() as sess:
            sess['logged_in'] = True
        web_outlook_app.set_setting('cloudmail_base_url', 'https://mail-api.invalid')
        web_outlook_app.set_setting('cloudmail_domain', 'mail.example')

    def tearDown(self):
        self.app.config['TESTING'] = False

    def test_unauthenticated_settings_are_refused(self):
        client = self.app.test_client()
        self.assertEqual(client.get('/api/cloudmail/settings').status_code, 401)

    def test_settings_save_rejects_a_base_url_without_a_scheme(self):
        response = self.client.post('/api/cloudmail/settings',
                                    json={'base_url': 'mail-api.invalid', 'enabled': True})
        self.assertFalse(response.get_json()['success'])
        self.assertEqual(web_outlook_app.get_cloudmail_base_url(), 'https://mail-api.invalid')

    def test_saving_an_empty_password_keeps_the_stored_one(self):
        web_outlook_app.set_setting_encrypted('cloudmail_admin_password', 'keepme-12345')

        response = self.client.post('/api/cloudmail/settings', json={'admin_password': '   '})

        self.assertTrue(response.get_json()['success'])
        self.assertEqual(web_outlook_app.get_cloudmail_admin_password(), 'keepme-12345')

    def test_saving_a_new_password_replaces_it_and_drops_the_cached_token(self):
        web_outlook_app.set_setting_encrypted('cloudmail_admin_password', 'old-password-1')
        web_outlook_app.set_setting_encrypted('cloudmail_public_token', 'stale-token')

        response = self.client.post('/api/cloudmail/settings', json={'admin_password': 'new-password-22'})

        self.assertTrue(response.get_json()['success'])
        self.assertEqual(web_outlook_app.get_cloudmail_admin_password(), 'new-password-22')
        self.assertEqual(web_outlook_app.get_setting_decrypted('cloudmail_public_token', ''), '')

    def test_generate_creates_a_local_record_for_the_cloudmail_provider(self):
        created = {'success': True, 'address': 'brandnew@mail.example', 'password': 'pw-generated-123'}

        with patch.object(web_outlook_app, 'is_cloudmail_enabled', return_value=True), \
                patch.object(web_outlook_app, 'cloudmail_create_address', return_value=created) as create:
            response = self.client.post('/api/temp-emails/generate', json={'provider': 'cloudmail'})

        payload = response.get_json()
        self.assertTrue(payload['success'], payload)
        self.assertEqual(payload['email'], 'brandnew@mail.example')
        create.assert_called_once()

        row = web_outlook_app.get_temp_email_by_address('brandnew@mail.example')
        self.assertEqual(row['provider'], 'cloudmail')
        stored = web_outlook_app.get_db().execute(
            'SELECT cloudmail_password FROM temp_emails WHERE email = ?',
            ('brandnew@mail.example',),
        ).fetchone()
        self.assertTrue(stored['cloudmail_password'])
        self.assertNotEqual(stored['cloudmail_password'], 'pw-generated-123',
                            'the address password must be stored encrypted')

    def test_generate_refuses_when_the_provider_is_disabled(self):
        with patch.object(web_outlook_app, 'is_cloudmail_enabled', return_value=False), \
                patch.object(web_outlook_app, 'cloudmail_create_address') as create:
            response = self.client.post('/api/temp-emails/generate', json={'provider': 'cloudmail'})

        self.assertFalse(response.get_json()['success'])
        create.assert_not_called()

    def test_provider_specific_fetch_is_reachable_through_the_messages_route(self):
        web_outlook_app.add_temp_email('listed@mail.example', provider='cloudmail')
        fetched = {'success': True, 'method': 'cloud-mail', 'messages': [{
            'id': '77', 'from_address': 'svc@site.invalid', 'subject': 'code 864213',
            'content': 'code 864213', 'html_content': '', 'has_html': 0, 'timestamp': 1700000000,
        }]}

        with patch.object(web_outlook_app, 'fetch_cloudmail_temp_messages', return_value=fetched):
            response = self.client.get('/api/temp-emails/listed@mail.example/messages')

        payload = response.get_json()
        self.assertTrue(payload['success'], payload)
        self.assertEqual(payload['method'], 'cloud-mail')
        self.assertEqual(payload['count'], 1)
        self.assertIn('864213', payload['emails'][0]['subject'])


if __name__ == '__main__':
    unittest.main()
