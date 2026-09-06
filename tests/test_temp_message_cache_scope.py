import os
import sqlite3
import tempfile
import unittest


if 'DATABASE_PATH' not in os.environ:
    _temp_dir = tempfile.mkdtemp(prefix='outlookEmail-message-scope-')
    os.environ['DATABASE_PATH'] = os.path.join(_temp_dir, 'test.db')
if 'SECRET_KEY' not in os.environ:
    os.environ['SECRET_KEY'] = 'test-secret-key'

import web_outlook_app


def _unique_indexes():
    connection = sqlite3.connect(web_outlook_app.DATABASE)
    try:
        indexes = []
        for row in connection.execute('PRAGMA index_list(temp_email_messages)').fetchall():
            name, is_unique, origin = row[1], bool(row[2]), row[3] if len(row) > 3 else ''
            if not is_unique:
                continue
            columns = [column[2] for column in connection.execute(
                'PRAGMA index_info(%s)' % name).fetchall()]
            indexes.append(columns)
        return indexes
    finally:
        connection.close()


def _mailbox_row(message_id, content):
    return {
        'id': message_id,
        'from_address': 'sender@example.com',
        'subject': 'subject of ' + message_id,
        'content': content,
        'html_content': '',
        'has_html': 0,
        'timestamp': 1788674463,
    }


class MessageCacheScopeTests(unittest.TestCase):
    """Cached temp messages are keyed by (mailbox, message id), never by id alone.

    Providers number messages independently - cloud-mail hands back bare integers - so a
    global UNIQUE(message_id) plus INSERT OR REPLACE silently destroyed the other
    mailbox's cached body, which looked like "the list shows the mail but opening it has
    no content".
    """

    def setUp(self):
        with web_outlook_app.app.app_context():
            web_outlook_app.init_db()

    def test_message_id_is_no_longer_globally_unique(self):
        indexes = _unique_indexes()
        self.assertNotIn(['message_id'], indexes,
                         'a single-column UNIQUE(message_id) still allows cross-mailbox overwrite')
        self.assertIn(['email_address', 'message_id'], indexes)

    def test_two_mailboxes_keep_their_own_body_for_the_same_message_id(self):
        with web_outlook_app.app.app_context():
            web_outlook_app.add_temp_email('left@mail.example', provider='cloudmail')
            web_outlook_app.add_temp_email('right@mail.example', provider='cloudmail')

            web_outlook_app.save_temp_email_messages(
                'left@mail.example', [_mailbox_row('91', 'body of left')])
            web_outlook_app.save_temp_email_messages(
                'right@mail.example', [_mailbox_row('91', 'body of right')])

            left = web_outlook_app.get_temp_email_message_by_id_for_address('91', 'left@mail.example')
            right = web_outlook_app.get_temp_email_message_by_id_for_address('91', 'right@mail.example')

            self.assertIsNotNone(left, 'the first cached body must survive the second save')
            self.assertIsNotNone(right)
            self.assertEqual(left['content'], 'body of left')
            self.assertEqual(right['content'], 'body of right')

    def test_the_scoped_lookup_does_not_return_a_neighbours_row(self):
        with web_outlook_app.app.app_context():
            web_outlook_app.add_temp_email('solo@mail.example', provider='cloudmail')
            web_outlook_app.save_temp_email_messages(
                'solo@mail.example', [_mailbox_row('77', 'solo body')])
            self.assertIsNone(
                web_outlook_app.get_temp_email_message_by_id_for_address('77', 'nobody@mail.example'))

    def test_detail_endpoint_returns_the_requested_mailbox_body(self):
        app = web_outlook_app.app
        app.config['TESTING'] = True
        app.config['WTF_CSRF_ENABLED'] = False
        client = app.test_client()
        with client.session_transaction() as sess:
            sess['logged_in'] = True

        with app.app_context():
            web_outlook_app.add_temp_email('a@mail.example', provider='cloudmail')
            web_outlook_app.add_temp_email('b@mail.example', provider='cloudmail')
            web_outlook_app.save_temp_email_messages(
                'a@mail.example', [_mailbox_row('99', 'A owns this body')])
            web_outlook_app.save_temp_email_messages(
                'b@mail.example', [_mailbox_row('99', 'B owns this body')])

        first = client.get('/api/temp-emails/a@mail.example/messages/99').get_json()
        second = client.get('/api/temp-emails/b@mail.example/messages/99').get_json()

        self.assertTrue(first.get('success'), first)
        self.assertTrue(second.get('success'), second)
        self.assertEqual(first['email']['body'], 'A owns this body')
        self.assertEqual(second['email']['body'], 'B owns this body')
        app.config['TESTING'] = False


if __name__ == '__main__':
    unittest.main()
