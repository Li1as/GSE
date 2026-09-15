"""Verify SMTP TLS and authentication only: no MAIL, RCPT or DATA commands."""
import json
import smtplib
import ssl
from assistant import ROOT


def main():
    client = None
    try:
        config = json.loads((ROOT / 'mail.local.json').read_text())
        client = smtplib.SMTP_SSL(config['smtp_host'], config['smtp_port'], timeout=25,
                                  context=ssl.create_default_context())
        client.ehlo_or_helo_if_needed()
        client.login(config['address'], config['password'])
        print('SMTP TLS 与认证通过；未发送 MAIL、RCPT 或 DATA。')
        return 0
    except (OSError, ValueError, KeyError) as error:
        print('SMTP 验证失败：' + type(error).__name__)
        return 1
    finally:
        if client:
            client.close()


if __name__ == '__main__': raise SystemExit(main())
