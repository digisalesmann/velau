"""
payments.py — plan pricing.

Payment collection is manual and admin-managed (crypto wallet or bank
transfer, reviewed by an admin in-app) — see database.py's
payment_methods/subscriptions tables and main.py's /subscription/* and
/admin/payment_methods, /admin/subscriptions/* endpoints.
"""

PLANS: dict[str, dict] = {
    "monthly": {
        "usd":   149.90,
        "label": "Monthly",
        "days":  30,
    },
    "yearly": {
        "usd":   1699.00,
        "label": "Yearly",
        "days":  365,
    },
    "lifetime": {
        "usd":   7800.00,
        "label": "Lifetime",
        "days":  None,  # never expires
    },
}
