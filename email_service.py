"""
Outbound transactional email — SendGrid REST API via plain requests, no SDK
dependency (same lightweight-client style as brokers/deriv_rest.py).

Templates use __TOKEN__ placeholders substituted with str.replace(), not
.format()/f-strings — the HTML contains literal CSS braces ({ }) that would
collide with Python's format-string syntax.
"""
import os
import logging

import requests

logger = logging.getLogger("EmailService")

SENDGRID_API_URL = "https://api.sendgrid.com/v3/mail/send"
LOGO_URL = "https://velau.onrender.com/static/velau-logo.png"


def send_email(to: str, subject: str, html: str, text: str):
    """Raises on failure — callers should catch and log, not let a broken
    email provider 500 an unrelated request (e.g. password reset should
    still create the reset code even if the send itself fails)."""
    api_key = os.getenv("SENDGRID_API_KEY", "")
    from_email = os.getenv("SENDGRID_FROM_EMAIL", "")
    if not api_key or not from_email:
        raise RuntimeError("SENDGRID_API_KEY / SENDGRID_FROM_EMAIL not configured.")

    resp = requests.post(
        SENDGRID_API_URL,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json={
            "personalizations": [{"to": [{"email": to}]}],
            "from": {"email": from_email, "name": "Velau"},
            "subject": subject,
            "content": [
                {"type": "text/plain", "value": text},
                {"type": "text/html", "value": html},
            ],
        },
        timeout=10,
    )
    if resp.status_code >= 400:
        raise RuntimeError(f"SendGrid {resp.status_code}: {resp.text[:300]}")
    logger.info(f"Sent '{subject}' to {to}")


_RESET_PASSWORD_HTML = """<!--[if mso]>
<noscript><xml><o:OfficeDocumentSettings><o:PixelsPerInch>96</o:PixelsPerInch></o:OfficeDocumentSettings></xml></noscript>
<![endif]-->
<meta name="color-scheme" content="light dark">
<meta name="supported-color-schemes" content="light dark">
<style>
  body, table, td { -webkit-text-size-adjust: 100%; -ms-text-size-adjust: 100%; }
  table, td { mso-table-lspace: 0pt; mso-table-rspace: 0pt; }
  img { border: 0; line-height: 100%; outline: none; text-decoration: none; }
  body { margin: 0; padding: 0; width: 100% !important; }
  a { text-decoration: none; }
  @media (prefers-color-scheme: dark) {
    .bg { background-color: #0B0D0A !important; }
    .ink { color: #EDE9E0 !important; }
    .muted { color: #9AA398 !important; }
    .rule { border-color: #262B22 !important; }
    .code-digits { color: #EDE9E0 !important; }
  }
</style>
<div style="display:none;max-height:0;overflow:hidden;mso-hide:all;opacity:0;">
  Your Velau verification code is ready — it expires in __EXPIRY_MINUTES__ minutes.
  &nbsp;&zwnj;&nbsp;&zwnj;&nbsp;&zwnj;&nbsp;&zwnj;&nbsp;&zwnj;&nbsp;&zwnj;&nbsp;&zwnj;&nbsp;&zwnj;&nbsp;&zwnj;&nbsp;&zwnj;
</div>
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" class="bg" style="background-color:#FFFFFF;">
  <tr>
    <td align="center" style="padding: 48px 24px;">
      <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="width:100%;max-width:560px;">

        <!-- Logo -->
        <tr>
          <td style="padding-bottom: 40px;">
            <img src="__LOGO_URL__" width="64" height="64" alt="Velau" style="display:block;width:64px;height:64px;border:0;">
          </td>
        </tr>

        <!-- Greeting -->
        <tr>
          <td class="ink" style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;font-size:22px;line-height:1.4;font-weight:800;color:#0F172A;padding-bottom:12px;">
            Reset your password
          </td>
        </tr>
        <tr>
          <td class="muted" style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;font-size:15px;line-height:1.6;color:#64748B;padding-bottom:36px;">
            We received a request to reset the password on your Velau account. Enter the code below in the app to continue, it's valid for __EXPIRY_MINUTES__ minutes.
          </td>
        </tr>

        <!-- Code — the one visual moment, no card/box -->
        <tr>
          <td align="center" class="rule" style="border-top:1px solid #E7E4DA;padding-top:28px;">
            <span class="code-digits" style="font-family:'SF Mono','Cascadia Mono',Consolas,Menlo,monospace;font-size:44px;font-weight:700;letter-spacing:0.28em;color:#0F172A;">
              __CODE__
            </span>
          </td>
        </tr>
        <tr>
          <td align="center" class="rule" style="border-bottom:1px solid #E7E4DA;padding-bottom:28px;">
          </td>
        </tr>
        <tr>
          <td align="center" class="muted" style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;font-size:13px;color:#64748B;padding-top:16px;padding-bottom:40px;">
            Expires <span class="ink" style="color:#0F172A;font-weight:700;">__EXPIRY_MINUTES__ minutes</span> from when this was sent
          </td>
        </tr>

        <!-- Security note -->
        <tr>
          <td class="muted" style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;font-size:13.5px;line-height:1.6;color:#64748B;padding-bottom:8px;">
            Didn't request this? You can safely ignore this email, your password won't change unless this code is used.
          </td>
        </tr>

        <!-- Footer -->
        <tr>
          <td style="padding-top: 44px;">
            <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">
              <tr>
                <td class="rule muted" style="border-top:1px solid #E7E4DA;padding-top:20px;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;font-size:12px;line-height:1.7;color:#94998E;">
                  Sent by Velau &nbsp;·&nbsp; automated account security message &nbsp;·&nbsp; not monitored for replies
                </td>
              </tr>
            </table>
          </td>
        </tr>

      </table>
    </td>
  </tr>
</table>
"""

_RESET_PASSWORD_TEXT = """Reset your password

We received a request to reset the password on your Velau account.

Your code: __CODE__

Enter this in the app to continue — it expires in __EXPIRY_MINUTES__ minutes.

Didn't request this? You can safely ignore this email, your password won't change unless this code is used.

— Velau (automated message, not monitored for replies)
"""


def send_password_reset_email(to: str, code: str, expiry_minutes: int):
    code_display = f"{code[:3]} {code[3:]}" if len(code) == 6 else code
    html = (
        _RESET_PASSWORD_HTML
        .replace("__LOGO_URL__", LOGO_URL)
        .replace("__CODE__", code_display)
        .replace("__EXPIRY_MINUTES__", str(expiry_minutes))
    )
    text = (
        _RESET_PASSWORD_TEXT
        .replace("__CODE__", code_display)
        .replace("__EXPIRY_MINUTES__", str(expiry_minutes))
    )
    send_email(to, "Reset your Velau password", html, text)
