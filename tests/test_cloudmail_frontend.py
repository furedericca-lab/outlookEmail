import os
import tempfile
import unittest
from pathlib import Path


if 'DATABASE_PATH' not in os.environ:
    _temp_dir = tempfile.mkdtemp(prefix='outlookEmail-cloudmail-frontend-')
    os.environ['DATABASE_PATH'] = os.path.join(_temp_dir, 'test.db')
if 'SECRET_KEY' not in os.environ:
    os.environ['SECRET_KEY'] = 'test-secret-key'

import web_outlook_app


ROOT_DIR = Path(__file__).resolve().parents[1]
TEMP_EMAILS_JS = (ROOT_DIR / 'static' / 'js' / 'index' / '03-temp-emails.js').read_text(encoding='utf-8')
SETTINGS_JS = (ROOT_DIR / 'static' / 'js' / 'index' / '07-settings.js').read_text(encoding='utf-8')
DIALOGS_MANAGEMENT = (ROOT_DIR / 'templates' / 'partials' / 'index' / 'dialogs-management.html').read_text(encoding='utf-8')


class CloudmailCreateDialogFrontendTests(unittest.TestCase):
    """The provider must be selectable and submittable from the existing create dialog."""

    def test_cloudmail_provider_tab_exists_and_is_wired(self):
        self.assertIn('id="providerCloudmail"', TEMP_EMAILS_JS)
        self.assertIn('name="tempEmailProvider" value="cloudmail"', TEMP_EMAILS_JS)
        self.assertIn("toggleTempEmailProvider('cloudmail')", TEMP_EMAILS_JS)

    def test_cloudmail_fields_exist(self):
        self.assertIn('id="cloudmailFields"', TEMP_EMAILS_JS)
        self.assertIn('id="cloudmailUsername"', TEMP_EMAILS_JS)
        self.assertIn('id="cloudmailDomain"', TEMP_EMAILS_JS)

    def test_toggle_shows_only_cloudmail_fields_for_that_provider(self):
        self.assertIn("if (provider === 'cloudmail') {", TEMP_EMAILS_JS)
        self.assertIn("cloudmailFields.style.display = 'block'", TEMP_EMAILS_JS)
        # The other three sections have to be hidden, and cloud-mail has to be reset for
        # them, otherwise switching back leaves two providers visible at once.
        self.assertIn("cloudmailFields.style.display = 'none'", TEMP_EMAILS_JS)

    def test_submit_posts_the_cloudmail_provider_payload(self):
        self.assertIn("} else if (provider === 'cloudmail') {", TEMP_EMAILS_JS)
        self.assertIn("document.getElementById('cloudmailUsername')", TEMP_EMAILS_JS)
        self.assertIn("document.getElementById('cloudmailDomain')", TEMP_EMAILS_JS)

    def test_list_badge_labels_the_provider_instead_of_falling_back_to_gptmail(self):
        self.assertIn("'cloud-mail' : 'GPTMail'", TEMP_EMAILS_JS)
        # An unlabelled provider would silently render as GPTMail.
        self.assertIn("email.provider === 'cloudmail' ? 'cloud-mail'", TEMP_EMAILS_JS)


class CloudmailSettingsFrontendTests(unittest.TestCase):
    def test_settings_card_exists_with_the_documented_fields(self):
        self.assertIn('id="settingsCloudmailSection"', DIALOGS_MANAGEMENT)
        for element_id in ('settingsCloudmailEnabled', 'settingsCloudmailBaseUrl',
                           'settingsCloudmailApiPrefix', 'settingsCloudmailAdminEmail',
                           'settingsCloudmailAdminPassword', 'settingsCloudmailDomain'):
            self.assertIn(f'id="{element_id}"', DIALOGS_MANAGEMENT)

    def test_the_password_field_is_a_password_input_and_never_prefilled(self):
        start = DIALOGS_MANAGEMENT.index('id="settingsCloudmailAdminPassword"')
        tag_start = DIALOGS_MANAGEMENT.rindex('<input', 0, start)
        tag = DIALOGS_MANAGEMENT[tag_start:DIALOGS_MANAGEMENT.index('>', start)]
        self.assertIn('type="password"', tag)
        self.assertNotIn('value=', tag, 'a stored credential must never be rendered into the page')

    def test_the_hint_names_the_failure_the_operator_otherwise_hits(self):
        # Filling the web UI address instead of the API address is the classic mistake,
        # so the form itself has to say it.
        self.assertIn('返回的不是 JSON', DIALOGS_MANAGEMENT)

    def test_settings_are_loaded_saved_and_tested(self):
        self.assertIn("async function loadCloudmailSettings()", SETTINGS_JS)
        self.assertIn("async function saveCloudmailSettings()", SETTINGS_JS)
        self.assertIn("async function testCloudmailConnection()", SETTINGS_JS)
        self.assertIn('await loadCloudmailSettings();', SETTINGS_JS)
        self.assertIn("fetch('/api/cloudmail/settings'", SETTINGS_JS)
        self.assertIn("fetch('/api/cloudmail/test'", SETTINGS_JS)

    def test_an_empty_password_is_not_sent_so_the_stored_one_survives(self):
        # The server treats "blank means keep" as the contract; the form must not send
        # an empty admin_password back on every save.
        block = SETTINGS_JS[SETTINGS_JS.index('async function saveCloudmailSettings()'):
                            SETTINGS_JS.index('async function testCloudmailConnection()')]
        self.assertIn('if (password) body.admin_password = password', block)
        self.assertNotIn("admin_password: '", block)


class CloudmailServedPageTests(unittest.TestCase):
    """Disk content is not the boundary; what the browser is served is."""

    def setUp(self):
        self.app = web_outlook_app.app
        self.app.config['TESTING'] = True
        self.app.config['WTF_CSRF_ENABLED'] = False
        self.client = self.app.test_client()
        with self.client.session_transaction() as sess:
            sess['logged_in'] = True

    def tearDown(self):
        self.app.config['TESTING'] = False

    def test_index_page_serves_the_cloudmail_settings_card(self):
        response = self.client.get('/')
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn('id="settingsCloudmailSection"', html)
        self.assertIn('id="settingsCloudmailBaseUrl"', html)
        self.assertNotIn('id="settingsCloudmailAdminPassword" value=', html)

    def test_served_scripts_contain_the_cloudmail_wiring(self):
        for path, markers in (
            ('/static/js/index/03-temp-emails.js', ('providerCloudmail', "} else if (provider === 'cloudmail') {")),
            ('/static/js/index/07-settings.js', ('loadCloudmailSettings', "fetch('/api/cloudmail/settings'")),
        ):
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 200)
                body = response.get_data(as_text=True)
                for marker in markers:
                    self.assertIn(marker, body)


if __name__ == '__main__':
    unittest.main()
