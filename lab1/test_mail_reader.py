import imaplib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock
from email.message import EmailMessage

from mail_reader import Reader, MailError, MAX_BYTES, parse_message, save_snapshot, thread_candidates


def sample():
    msg = EmailMessage()
    msg['Subject'] = '课程报名'
    msg['Message-ID'] = '<one@example.test>'
    msg.set_content('请提供课程成绩。')
    msg.add_alternative('<p>HTML 备用内容</p>', subtype='html')
    msg.add_attachment(b'attachment', maintype='application', subtype='pdf', filename='../成绩单.pdf')
    return msg.as_bytes()


class MailTests(unittest.TestCase):
    def reader(self):
        conn = Mock()
        conn.select.return_value = ('OK', [b'1'])
        conn.response.return_value = ('UIDVALIDITY', [b'42'])
        reader = Reader({'address': 'a@example.test', 'password': 'secret',
                         'imap_host': 'example.test', 'imap_port': 993},
                        factory=Mock(return_value=conn))
        return reader, conn

    def test_mime_plain_and_attachment(self):
        result = parse_message(sample())
        self.assertEqual(result['Subject'], '课程报名')
        self.assertIn('请提供课程成绩', result['body'])
        self.assertNotIn('HTML', result['body'])
        self.assertEqual(result['attachments'][0]['filename'], '../成绩单.pdf')
        self.assertEqual(result['attachments'][0]['size'], 10)

    def test_html_no_active_content(self):
        msg = EmailMessage()
        msg.set_content('<p>正文</p><script>secret()</script><img src="https://example.test/pixel">', subtype='html')
        text = parse_message(msg.as_bytes())['body']
        self.assertIn('正文', text)
        self.assertNotIn('secret', text)
        self.assertNotIn('https', text)

    def test_readonly_and_peek(self):
        reader, conn = self.reader()
        reader.select('INBOX')
        raw = sample()
        conn.uid.side_effect = [('OK', [b'1 (RFC822.SIZE 1000)']),
                                ('OK', [(b'1 (BODY[] {1000}', raw), b')'])]
        result, actual = reader.fetch('7')
        conn.select.assert_called_once_with('"INBOX"', readonly=True)
        self.assertEqual(conn.uid.call_args.args, ('FETCH', '7', '(BODY.PEEK[])'))
        self.assertEqual(result['uidvalidity'], '42')
        self.assertEqual(actual, raw)
        conn.store.assert_not_called()
        conn.expunge.assert_not_called()

    def test_size_limit_before_body(self):
        reader, conn = self.reader()
        conn.uid.return_value = ('OK', [('1 (RFC822.SIZE %s)' % (MAX_BYTES + 1)).encode()])
        with self.assertRaises(MailError):
            reader.fetch('1')
        self.assertEqual(conn.uid.call_count, 1)

    def test_invalid_uid_no_network(self):
        reader, conn = self.reader()
        with self.assertRaises(MailError):
            reader.fetch('1:*')
        conn.uid.assert_not_called()

    def test_auth_redacted_and_not_retried(self):
        factory = Mock()
        factory.return_value.login.side_effect = imaplib.IMAP4.error('secret')
        with self.assertRaises(MailError) as raised:
            Reader({'address': 'a', 'password': 'secret', 'imap_host': 'h', 'imap_port': 993}, factory)
        self.assertNotIn('secret', str(raised.exception))
        factory.return_value.login.assert_called_once()
        factory.return_value.logout.assert_called_once()

    def test_thread_not_subject_only(self):
        selected = parse_message(sample())
        unrelated = dict(selected, **{'Message-ID': '<other@example.test>'})
        reply = dict(unrelated, **{'In-Reply-To': selected['Message-ID']})
        self.assertEqual(thread_candidates(selected, [unrelated, reply]), [reply])

    def test_snapshot_identity_and_no_attachment_extraction(self):
        raw = sample()
        parsed = parse_message(raw)
        parsed.update(account='a', folder='INBOX', uidvalidity='42', uid='7')
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            directory = save_snapshot(root, parsed, raw)
            self.assertEqual(save_snapshot(root, parsed, raw), directory)
            self.assertEqual((directory / 'message.eml').read_bytes(), raw)
            self.assertEqual(len(list(directory.iterdir())), 2)
            self.assertEqual((directory / 'message.json').stat().st_mode & 0o777, 0o600)
            parsed['uidvalidity'] = '43'
            self.assertNotEqual(save_snapshot(root, parsed, raw), directory)


if __name__ == '__main__':
    unittest.main()
