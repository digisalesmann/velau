"""Public web pages required for the Google Play listing.

/privacy         the privacy policy (Play's "Privacy policy" URL)
/delete-account  how to delete an account (Play's "Delete account" URL)

Keep the policy text in sync with mobile/lib/screens/privacy_policy_screen.dart.
"""
from html import escape

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

router = APIRouter()

CONTACT = "velau.app@gmail.com"

_POLICY = [
    (None, "Velau is a trading tool that connects to your own broker account. This policy explains exactly what we collect, why, and who we share it with."),
    ("1. Information We Collect", [
        "Account information: email address, hashed password, and (if you use Google Sign-In) your name and email from your Google account.",
        "Broker credentials: your Deriv API token, stored on our secured servers with restricted access, used solely to place trades and read your account balance/history on your behalf.",
        "Trading data: signals generated, trades placed, contract outcomes, and profit/loss history associated with your account.",
        "Device & push data: your Firebase Cloud Messaging token, used to deliver trade and account notifications.",
        "Payment data: for subscription payments, the payment method you chose, transaction references, and the payment screenshot you upload as proof. We do not collect card numbers.",
        "Usage & diagnostic data: app version, device type, crash logs, and general usage patterns to help us fix bugs and improve the service.",
    ]),
    ("2. How We Use Your Information", "We use the information above to: operate and maintain your account; execute trades you've authorized on connected broker accounts; send you push notifications about trade activity; process subscription payments; detect fraud and abuse; provide customer support; and improve the reliability and performance of the service."),
    ("3. Legal Basis for Processing", "We process your data on the basis of: performance of our contract with you (providing the trading service you signed up for); your consent (e.g. push notifications, optional biometric login); and our legitimate interest in keeping the service secure and reliable."),
    ("4. Who We Share Data With", [
        "We do not sell your personal data. We share limited data only with the service providers that make Velau work:",
        "Deriv: our servers use your API token to execute trades and read your balance on your behalf. Velau never has your Deriv password.",
        "Firebase (Google): authentication, push notifications, and crash diagnostics.",
        "Google Sign-In: if you choose to sign in with Google, Google authenticates your identity on our behalf.",
        "Our hosting and database providers: store your account, trade history, and app data securely.",
        "Payment providers: process and verify subscription payments.",
        "Law enforcement or regulators: only where required by applicable law.",
    ]),
    ("5. Data Retention", f"We retain your account and trading data for as long as your account is active. You can delete your account at any time from Account > Delete Account in the app, or by emailing {CONTACT} from your registered email. Deletion permanently removes your account, Deriv token, trade history, signals and notification tokens. Subscription payment records are kept for accounting and legal purposes, with your email removed."),
    ("6. Data Security", "We use industry-standard measures to protect your data, including encrypted connections (HTTPS), restricted access to stored broker tokens, password hashing, optional two-factor authentication, and optional biometric app lock. No system is 100% secure, and we cannot guarantee absolute security of information transmitted over the internet."),
    ("7. International Data Transfers", "Velau is offered globally. Your information may be processed and stored on servers located outside your country of residence, including in jurisdictions with different data protection laws than your own. By using Velau, you consent to this transfer and processing."),
    ("8. Your Rights", f"Depending on your location, you may have the right to: access the personal data we hold about you; request correction of inaccurate data; request deletion of your data; object to or restrict certain processing; and request a copy of your data in a portable format. To exercise any of these rights, contact us at {CONTACT}. We will respond within a reasonable timeframe."),
    ("9. Children's Privacy", "Velau is not intended for individuals under 18 years of age. We do not knowingly collect personal information from minors. If you believe a minor has provided us with personal data, please contact us so we can delete it."),
    ("10. Local Storage on Your Device", "The app stores some data locally on your device (such as your login session and preferences) to keep you signed in and remember your settings. This data is cleared if you log out or uninstall the app."),
    ("11. Changes to This Policy", "We may update this Privacy Policy from time to time to reflect changes in our practices or for legal reasons. Material changes will be communicated via in-app notification. Continued use of Velau after changes take effect constitutes acceptance of the revised policy."),
    ("12. Contact Us", f"If you have questions about this Privacy Policy or how your data is handled, contact us at {CONTACT}."),
]

_STYLE = """
:root { color-scheme: light dark; --bg:#fff; --fg:#1a1a1a; --muted:#666; --accent:#c9a227; }
@media (prefers-color-scheme: dark) { :root { --bg:#111; --fg:#eee; --muted:#aaa; } }
body { background:var(--bg); color:var(--fg); font:16px/1.6 -apple-system,system-ui,Segoe UI,Roboto,sans-serif;
       max-width:720px; margin:0 auto; padding:32px 16px 64px; }
h1 { margin-bottom:4px; } h2 { font-size:18px; margin-top:28px; }
.muted { color:var(--muted); font-size:14px; } a { color:var(--accent); }
ol li, ul li { margin-bottom:8px; }
"""


def _page(title: str, body: str) -> HTMLResponse:
    return HTMLResponse(
        f"<!doctype html><html lang='en'><head><meta charset='utf-8'>"
        f"<meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"<title>{escape(title)} | Velau</title><style>{_STYLE}</style></head>"
        f"<body>{body}</body></html>"
    )


@router.get("/privacy", response_class=HTMLResponse, include_in_schema=False)
def privacy_page():
    parts = ["<h1>Privacy Policy</h1><p class='muted'>Velau. Last updated: November 2026.</p>"]
    for heading, content in _POLICY:
        if heading:
            parts.append(f"<h2>{escape(heading)}</h2>")
        if isinstance(content, list):
            parts.append("<ul>" + "".join(f"<li>{escape(c)}</li>" for c in content) + "</ul>")
        else:
            parts.append(f"<p>{escape(content)}</p>")
    return _page("Privacy Policy", "".join(parts))


@router.get("/delete-account", response_class=HTMLResponse, include_in_schema=False)
def delete_account_page():
    body = f"""
<h1>Delete your Velau account</h1>
<p class='muted'>Velau</p>
<h2>In the app</h2>
<ol>
  <li>Open Velau and sign in.</li>
  <li>Go to <b>Account</b>, then tap <b>Delete Account</b>.</li>
  <li>Type <b>DELETE</b> to confirm.</li>
</ol>
<p>If the bot has trades open on your account, deletion waits until they close.
You can also close them yourself on Deriv first.</p>
<h2>By email</h2>
<p>If you can't open the app, email <a href='mailto:{CONTACT}?subject=Delete%20my%20Velau%20account'>{CONTACT}</a>
from the email address you registered with and ask us to delete your account.
We complete requests within 7 days.</p>
<h2>What gets deleted</h2>
<ul>
  <li>Your account and login details</li>
  <li>Your connected Deriv API token</li>
  <li>Your trade history, signals and risk settings</li>
  <li>Your notification tokens</li>
</ul>
<h2>What we keep</h2>
<p>Subscription payment records are kept for accounting and legal purposes, with your email removed.
Your Deriv account and its money are separate from Velau and are not affected.</p>
<p><a href='/privacy'>Privacy Policy</a></p>
"""
    return _page("Delete your account", body)
