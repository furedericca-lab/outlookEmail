import os
import pathlib
import re
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
BOOTSTRAP = (ROOT / 'outlook_web' / 'segments' / '01_bootstrap.py').read_text(encoding='utf-8')
PRIMARY = (ROOT / 'templates' / 'partials' / 'index' / 'dialogs-primary.html').read_text(encoding='utf-8')
MANAGEMENT = (ROOT / 'templates' / 'partials' / 'index' / 'dialogs-management.html').read_text(encoding='utf-8')
GROUPS_JS = (ROOT / 'static' / 'js' / 'index' / '02-groups.js').read_text(encoding='utf-8')

if 'DATABASE_PATH' not in os.environ:
    os.environ['DATABASE_PATH'] = os.path.join(tempfile.mkdtemp(prefix='outlookEmail-sina-'), 'test.db')
if 'SECRET_KEY' not in os.environ:
    os.environ['SECRET_KEY'] = 'test-secret-key'

import web_outlook_app


# 一个提供商收多个后缀，和 qq 同时吃 qq.com / foxmail.com 同构。新浪的特殊之处是每个后缀
# 有各自的服务器，所以主机必须按地址域名解析。VIP 后缀按运营方要求不接入。
SINA_DOMAIN_HOSTS = {
    'sina.com': ('imap.sina.com', 'smtp.sina.com'),
    'sina.cn': ('imap.sina.cn', 'smtp.sina.cn'),
}
SINA_EXCLUDED_DOMAINS = ('vip.sina.com', 'vip.sina.cn')


class SinaProviderTests(unittest.TestCase):
    def test_sina_is_one_provider_covering_both_suffixes(self):
        meta = web_outlook_app.MAIL_PROVIDERS.get('sina')
        self.assertIsNotNone(meta)
        self.assertNotIn('sina-cn', web_outlook_app.MAIL_PROVIDERS,
                         '两个后缀是一个提供商的两个域名，不是两个提供商（与 qq/foxmail 同构）')
        self.assertEqual(meta['label'], 'Sina',
                         '运营方要求下拉里只写 Sina，服务器与后缀细节留在提示文案里')
        self.assertEqual(meta['imap_port'], 993)
        self.assertEqual(meta['account_type'], 'imap')

    def test_both_suffixes_infer_the_same_provider(self):
        for domain in SINA_DOMAIN_HOSTS:
            with self.subTest(domain=domain):
                self.assertEqual(web_outlook_app.infer_provider_from_email('someone@%s' % domain), 'sina')

    def test_the_imap_host_follows_the_address_suffix(self):
        for domain, (imap_host, _) in SINA_DOMAIN_HOSTS.items():
            with self.subTest(domain=domain):
                meta = web_outlook_app.get_provider_meta('sina', 'someone@%s' % domain)
                self.assertEqual(meta['key'], 'sina', '提供商仍是 sina，只是主机按后缀走')
                self.assertEqual(meta['imap_host'], imap_host)
        self.assertEqual(web_outlook_app.MAIL_PROVIDERS['sina']['domain_hosts'],
                         {domain: hosts[0] for domain, hosts in SINA_DOMAIN_HOSTS.items()})

    def test_the_qq_precedent_still_holds(self):
        # 用户要求 sina 对齐的既有行为：一个键收多域名，且这些域名共用一台服务器。
        self.assertEqual(web_outlook_app.infer_provider_from_email('a@qq.com'), 'qq')
        self.assertEqual(web_outlook_app.infer_provider_from_email('a@foxmail.com'), 'qq')
        self.assertEqual(web_outlook_app.get_provider_meta('qq', 'a@qq.com')['imap_host'],
                         web_outlook_app.get_provider_meta('qq', 'a@foxmail.com')['imap_host'])

    def test_a_mismatched_choice_follows_the_address_domain(self):
        self.assertEqual(web_outlook_app.get_provider_meta('163', 'someone@qq.com')['imap_host'], 'imap.qq.com')

    def test_unknown_domains_still_keep_the_selected_provider(self):
        # Google Workspace 之类不在映射表里的域名不能被降级成 custom，
        # 只有域名自己推出明确提供商时才允许覆盖下拉选择。
        self.assertEqual(web_outlook_app.get_provider_meta('gmail', 'someone@example.com')['imap_host'], 'imap.gmail.com')
        self.assertEqual(web_outlook_app.get_provider_meta('sina', 'someone@example.com')['imap_host'], 'imap.sina.com')

    def test_the_vip_suffixes_stay_out_and_do_not_hijack_a_host(self):
        for domain in SINA_EXCLUDED_DOMAINS:
            with self.subTest(domain=domain):
                self.assertNotIn(domain, web_outlook_app.DOMAIN_PROVIDER_MAP)
                # 未接入的后缀必须落到 custom 由用户自己填主机，而不是套用 @sina.com 那台。
                self.assertEqual(web_outlook_app.infer_provider_from_email('someone@%s' % domain), 'custom')
                self.assertNotIn('value="sina-vip"', PRIMARY)
                self.assertNotIn('value="sina-vip"', MANAGEMENT)

    def test_the_pop3_only_protocol_is_not_recorded_as_if_it_worked(self):
        # 新浪同时公布 POP3 服务器，但本仓库没有 POP3 收信实现，不能把死配置塞进预设让人以为能用。
        self.assertNotIn('poplib', BOOTSTRAP)
        self.assertNotIn('pop3_host', web_outlook_app.MAIL_PROVIDERS['sina'])

    def test_the_forms_offer_sina_once(self):
        self.assertEqual(PRIMARY.count('value="sina"'), 2, '两个 IMAP 表单各一个 Sina 选项')
        self.assertEqual(MANAGEMENT.count('value="sina"'), 1, 'SMTP 转发表单一个 Sina 选项')
        self.assertEqual(PRIMARY.count('value="sina-cn"'), 0)

    def test_the_js_side_mirrors_the_server_registry(self):
        self.assertIn("            sina: 'Sina',\n", GROUPS_JS)
        self.assertNotIn("'sina-cn':", GROUPS_JS)
        self.assertIn("SMTP_FORWARD_PROVIDER_OPTIONS = ['outlook', 'qq', '163', '126', 'yahoo', 'aliyun', 'sina', 'custom'];", GROUPS_JS)
        self.assertIn('id="settingsSmtpFromEmail" placeholder="noreply@example.com" onchange="syncSmtpProviderUI(false)"', MANAGEMENT,
                      '发件人邮箱变化必须触发按后缀刷新 SMTP 主机')

    def test_smtp_host_per_suffix_agrees_with_the_imap_host(self):
        match = re.search(r"(?m)^\s+sina: \{(.*)\},$", GROUPS_JS)
        self.assertIsNotNone(match, 'JS 里缺少 Sina 的 SMTP 预设')
        block = match.group(1)
        self.assertIn("host: 'smtp.sina.com'", block)
        self.assertIn("domainHosts: { 'sina.com': 'smtp.sina.com', 'sina.cn': 'smtp.sina.cn' }", block)
        self.assertIn("port: '465'", block)
        self.assertIn("useSsl: true", block)
        # 同一后缀的 IMAP 与 SMTP 必须成对出现，防止只改一边的复制粘贴错误。
        for domain, (imap_host, smtp_host) in SINA_DOMAIN_HOSTS.items():
            self.assertEqual('smtp' + imap_host[len('imap'):], smtp_host)
            self.assertIn("'%s': '%s'" % (domain, smtp_host), block)

    def test_every_registry_key_is_reachable_from_the_forms(self):
        keys = [key for key in web_outlook_app.MAIL_PROVIDERS if key != 'custom']
        for key in keys:
            with self.subTest(key=key):
                self.assertGreaterEqual(PRIMARY.count('value="%s"' % key), 1,
                                        '服务端有该提供商但界面上选不到')
                self.assertIn(key, GROUPS_JS, '服务端有该提供商但 JS 标签缺失')


if __name__ == '__main__':
    unittest.main()
