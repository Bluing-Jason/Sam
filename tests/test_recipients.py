"""Recipient routing tests use isolated settings and mocked SMTP only."""
import copy
import base64
import http.client
from types import SimpleNamespace
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('solar_bridge_recipients', ROOT / 'bridge.py')
bridge = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bridge)


class RecipientTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=ROOT / 'tests')
        self.addCleanup(self.temp.cleanup)
        self.old_path = bridge.EMAIL_CONFIG_FILE
        bridge.EMAIL_CONFIG_FILE = str(Path(self.temp.name) / 'email_config.json')
        self.addCleanup(setattr, bridge, 'EMAIL_CONFIG_FILE', self.old_path)
        self.cfg = {**copy.deepcopy(bridge.DEFAULT_EMAIL_CONFIG), 'enabled': True,
                    'smtp_host': 'smtp.example.invalid', 'smtp_user': 'sender@example.com',
                    'smtp_pass': 'test-only-secret', 'from_email': 'sender@example.com',
                    'recipients': ['legacy@example.com'], 'daily_report_times': ['09:00', '18:00']}
        Path(bridge.EMAIL_CONFIG_FILE).write_text(json.dumps(self.cfg), encoding='utf-8')
        self.rows = [
            {'email': 'alert@example.com', 'enabled': True, 'alerts': True, 'reports': False},
            {'email': 'report@example.com', 'enabled': True, 'alerts': False, 'reports': True},
            {'email': 'both@example.com', 'enabled': True, 'alerts': True, 'reports': True},
            {'email': 'disabled@example.com', 'enabled': False, 'alerts': True, 'reports': True},
            {'email': 'neither@example.com', 'enabled': True, 'alerts': False, 'reports': False},
        ]

    def handler(self, body=None):
        handler = object.__new__(bridge.ModbusBridgeHandler)
        handler._read_json_body = lambda: body
        handler.result = []
        handler._send_json = lambda data, status=200: handler.result.append((data, status))
        return handler

    def test_legacy_list_loads_with_all_flags_without_rewriting_file(self):
        before = Path(bridge.EMAIL_CONFIG_FILE).read_bytes()
        cfg = bridge._load_email_config()
        self.assertEqual(bridge._normalise_recipients(cfg['recipients']), [{'email':'legacy@example.com', 'enabled':True, 'alerts':True, 'reports':True}])
        self.assertEqual(before, Path(bridge.EMAIL_CONFIG_FILE).read_bytes())

    def test_structured_flags_and_disabled_choices_are_preserved(self):
        original = copy.deepcopy(self.rows)
        result = bridge._normalise_recipients(self.rows)
        self.assertEqual(result, original)
        self.assertEqual(self.rows, original)
        self.assertIsNot(result[0], self.rows[0])

    def test_message_types_select_only_opted_in_active_recipients(self):
        cfg = {'recipients': self.rows}
        self.assertEqual(bridge._recipient_addresses(cfg, 'alert'), ['alert@example.com','both@example.com'])
        self.assertEqual(bridge._recipient_addresses(cfg, 'report'), ['report@example.com','both@example.com'])
        self.assertEqual(bridge._recipient_addresses(cfg, 'test'), ['alert@example.com','report@example.com','both@example.com'])

    def test_invalid_addresses_flags_and_duplicates_are_rejected(self):
        invalid = [None, 'not a list', ['not-an-email'], ['one@example.com\r\nBcc:other@example.com'],
                   [{'email':'ok@example.com','enabled':'false'}], ['A@example.com','a@example.com']]
        for rows in invalid:
            with self.subTest(rows=rows), self.assertRaises(ValueError):
                bridge._normalise_recipients(rows)
        self.assertEqual(bridge._normalise_recipients([' ops+solar@example.com.hk '])[0]['email'], 'ops+solar@example.com.hk')

    def test_recipient_only_save_preserves_smtp_and_report_settings(self):
        handler = self.handler({'recipients': self.rows})
        handler._handle_email_recipients_set()
        data, status = handler.result[0]
        self.assertEqual(status, 200)
        self.assertTrue(data['success'])
        saved = json.loads(Path(bridge.EMAIL_CONFIG_FILE).read_text(encoding='utf-8'))
        for key in self.cfg:
            if key != 'recipients': self.assertEqual(saved[key], self.cfg[key], key)
        self.assertEqual(saved['recipients'], self.rows)
        self.assertNotIn('smtp_pass', data)

    def test_replacing_bad_legacy_list_does_not_reset_existing_smtp_config(self):
        old = {**self.cfg,'recipients':['legacy-invalid-address']}
        Path(bridge.EMAIL_CONFIG_FILE).write_text(json.dumps(old),encoding='utf-8')
        handler = self.handler({'recipients':self.rows})
        handler._handle_email_recipients_set()
        self.assertTrue(handler.result[0][0]['success'])
        saved = bridge._load_email_config()
        self.assertEqual(saved['smtp_pass'],'test-only-secret')
        self.assertEqual(saved['smtp_host'],'smtp.example.invalid')
        self.assertEqual(saved['daily_report_times'],['09:00','18:00'])

    def test_invalid_recipient_save_does_not_modify_previous_settings(self):
        before = Path(bridge.EMAIL_CONFIG_FILE).read_bytes()
        for body in ({}, {'recipients':['bad']}, {'recipients':{'email':'ok@example.com'}}):
            handler = self.handler(body)
            handler._handle_email_recipients_set()
            self.assertEqual(handler.result[0][1], 400)
            self.assertFalse(handler.result[0][0]['success'])
        self.assertEqual(before, Path(bridge.EMAIL_CONFIG_FILE).read_bytes())

    def test_smtp_envelope_and_visible_to_match_each_message_category(self):
        cfg = {**self.cfg, 'recipients': self.rows}
        for kind, expected in [('alert',['alert@example.com','both@example.com']),
                               ('report',['report@example.com','both@example.com']),
                               ('test',['alert@example.com','report@example.com','both@example.com'])]:
            with self.subTest(kind=kind), patch('smtplib.SMTP') as smtp:
                smtp.return_value.sendmail.return_value = {}
                self.assertEqual(bridge._send_email(cfg,'subject','body',message_type=kind),(True,None))
                sender, recipients, message = smtp.return_value.sendmail.call_args.args
                self.assertEqual(sender, cfg['from_email'])
                self.assertEqual(recipients, expected)
                self.assertIn('To: ' + ', '.join(expected), message)
                self.assertNotIn('disabled@example.com', message)

    def test_no_matching_recipients_never_opens_an_smtp_connection(self):
        cfg = {**self.cfg, 'recipients': [self.rows[1]]}
        with patch('smtplib.SMTP') as smtp, patch('smtplib.SMTP_SSL') as ssl:
            ok, error = bridge._send_email(cfg,'subject','body',message_type='alert')
            self.assertFalse(ok)
            self.assertIn('收件人',error)
            smtp.assert_not_called(); ssl.assert_not_called()

    def test_full_form_save_keeps_masked_password_and_recipient_flags(self):
        handler = self.handler({'config': {**self.cfg, 'smtp_pass':'***', 'recipients':self.rows}})
        handler._handle_email_config_set()
        self.assertTrue(handler.result[0][0]['success'])
        saved = bridge._load_email_config()
        self.assertEqual(saved['smtp_pass'],'test-only-secret')
        self.assertEqual(saved['recipients'],self.rows)
        handler = self.handler()
        handler._handle_email_config_get()
        self.assertEqual(handler.result[0][0]['config']['smtp_pass'],'***')
        self.assertEqual(handler.result[0][0]['config']['recipients'],self.rows)

    def test_alert_entry_point_skips_unsubscribed_without_consuming_cooldown(self):
        cfg = {**self.cfg, 'recipients':[self.rows[1]]}
        bridge.EMAIL_ALERT_STATE.clear()
        with patch.object(bridge,'_load_email_config',return_value=cfg), patch('threading.Thread') as worker:
            bridge._try_alert_email({'id':'test-device'},10,11,10000)
            worker.assert_not_called()
        self.assertNotIn('test-device',bridge.EMAIL_ALERT_STATE)

    def test_global_mail_switch_still_suppresses_automatic_alerts(self):
        cfg = {**self.cfg, 'enabled':False, 'recipients':self.rows}
        with patch.object(bridge,'_load_email_config',return_value=cfg), patch('threading.Thread') as worker:
            bridge._try_alert_email({'id':'test-device'},10,11,10000)
            worker.assert_not_called()

    def test_automatic_alert_and_daily_report_use_separate_categories(self):
        cfg = {**self.cfg, 'recipients':self.rows, 'daily_report_send_ok':True}
        bridge.EMAIL_ALERT_STATE.clear()
        def inline_thread(*args, **kwargs):
            thread = MagicMock()
            thread.start.side_effect = kwargs['target']
            return thread
        with patch.object(bridge,'_load_email_config',return_value=cfg), patch('threading.Thread',side_effect=inline_thread), patch.object(bridge,'_send_email',return_value=(True,None)) as send:
            bridge._try_alert_email({'id':'test-device','name':'Test'},10,11,10000)
            self.assertEqual(send.call_args.kwargs['message_type'],'alert')
            bridge._send_daily_report(cfg,[],[],[])
            self.assertEqual(send.call_args.kwargs['message_type'],'report')

    def test_recipient_route_requires_auth_and_saves_after_authorised_post(self):
        class MemorySocket:
            def __init__(self, request): self.request, self.sent = request, bytearray()
            def makefile(self, *args): return io.BytesIO(self.request)
            def sendall(self, data): self.sent.extend(data)
        def request(authorised):
            data = json.dumps({'recipients':self.rows}).encode()
            auth = 'Authorization: Basic ' + base64.b64encode(b'admin:test-password').decode() + '\r\n' if authorised else ''
            raw = ('POST /api/email_recipients HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n'
                   + auth + f'Content-Length: {len(data)}\r\nContent-Type: application/json\r\n\r\n').encode() + data
            connection = MemorySocket(raw)
            bridge.ModbusBridgeHandler(connection, ('127.0.0.1',12345), SimpleNamespace(server_name='localhost',server_port=8080))
            response = http.client.HTTPResponse(MemorySocket(bytes(connection.sent)))
            response.begin()
            return response
        before = Path(bridge.EMAIL_CONFIG_FILE).read_bytes()
        with patch.dict(bridge.CONFIG, {'auth_user':'admin','auth_pass':'test-password'}):
            self.assertEqual(request(False).status,401)
            self.assertEqual(Path(bridge.EMAIL_CONFIG_FILE).read_bytes(),before)
            response = request(True)
            self.assertEqual(response.status,200)
            self.assertTrue(json.loads(response.read())['success'])
        self.assertEqual(bridge._load_email_config()['recipients'],self.rows)

    def test_test_email_uses_union_and_report_test_requires_report_recipient(self):
        handler = self.handler({'config':{**self.cfg,'smtp_pass':'***','recipients':self.rows}})
        with patch.object(bridge,'_send_email',return_value=(True,None)) as send:
            handler._handle_email_test()
            self.assertTrue(handler.result[0][0]['success'])
            self.assertEqual(send.call_args.kwargs['message_type'],'test')
            self.assertEqual(send.call_args.args[0]['smtp_pass'],'test-only-secret')
        cfg = {**self.cfg,'recipients':[self.rows[0]]}
        with patch.object(bridge,'_load_email_config',return_value=cfg), patch.object(bridge,'_do_daily_health_check') as check:
            handler = self.handler()
            handler._handle_daily_report_test()
            self.assertFalse(handler.result[0][0]['success'])
            check.assert_not_called()


if __name__ == '__main__':
    unittest.main()
