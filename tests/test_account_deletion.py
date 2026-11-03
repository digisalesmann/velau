# Account deletion (a Google Play requirement): POST /account/delete removes the
# user's data, keeps payment records anonymised, and refuses while the bot still
# has open trades. Also checks the public /privacy and /delete-account pages.

import os
import uuid

os.environ.pop("DATABASE_URL", None)

from fastapi.testclient import TestClient

import database as db
import main
from user_models import create_access_token

client = TestClient(main.app)


def _make_user():
    u = f"del-{uuid.uuid4().hex[:8]}@example.com"
    db.create_user(u, "x")
    db.execute("INSERT INTO fcm_tokens (token, username) VALUES (?, ?)", (f"tok-{u}", u))
    db.execute("INSERT INTO trade_results (contract_id, won, pnl, username) VALUES (?, ?, ?, ?)",
               (f"c-{u}", True, 5.0, u))
    db.execute("INSERT INTO signals (symbol, type, username) VALUES (?, ?, ?)", ("frxXAUUSD", "SELL", u))
    db.execute("INSERT INTO subscriptions (username, plan, payment_id) VALUES (?, ?, ?)",
               (u, "monthly", f"pay-{u}"))
    return u, {"Authorization": f"Bearer {create_access_token({'sub': u})}"}


def _count(table, u):
    return db.fetchone(f"SELECT COUNT(*) AS n FROM {table} WHERE username = ?", (u,))["n"]


def test_delete_removes_data_and_keeps_anonymised_payment():
    u, h = _make_user()
    assert client.post("/account/delete", json={"confirm": "nope"}, headers=h).status_code == 400
    r = client.post("/account/delete", json={"confirm": "DELETE"}, headers=h)
    assert r.status_code == 200, r.text
    for t in ("users", "fcm_tokens", "trade_results", "signals", "subscriptions"):
        assert _count(t, u) == 0, t
    assert db.fetchone("SELECT username FROM subscriptions WHERE payment_id = ?",
                       (f"pay-{u}",))["username"] == "deleted-user"
    # The old login no longer works.
    assert client.get("/deriv/status", headers=h).status_code == 401


def test_delete_refused_while_trades_open():
    u, h = _make_user()
    db.insert_trend_position(f"tp-{u}", u, "S20x2", 1, 4000.0, 2.0, 4030.0, 20.0, 26.0, 100, 1)
    r = client.post("/account/delete", json={"confirm": "DELETE"}, headers=h)
    assert r.status_code == 409 and db.user_exists(u)


def test_public_pages():
    p = client.get("/privacy")
    assert p.status_code == 200 and "Privacy Policy" in p.text and "Delete Account" in p.text
    d = client.get("/delete-account")
    assert d.status_code == 200 and "velau.app@gmail.com" in d.text


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"{name} passed.")
