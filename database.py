"""
database.py — persistent storage layer.

PostgreSQL on Render (DATABASE_URL), SQLite fallback for local dev.

Tables:
  users         — auth credentials + encrypted Deriv token per user
  signals       — strategy signals
  trade_results — trade outcomes per user
"""
import os
import logging
from contextlib import contextmanager

logger = logging.getLogger("Database")

DATABASE_URL  = os.getenv("DATABASE_URL", "")
_USE_POSTGRES = bool(DATABASE_URL)

if _USE_POSTGRES:
    import psycopg2
    import psycopg2.extras
    logger.info("✅ Using PostgreSQL (persistent)")
else:
    import sqlite3
    logger.warning("⚠️  DATABASE_URL not set — falling back to SQLite")


@contextmanager
def get_conn():
    if _USE_POSTGRES:
        conn = psycopg2.connect(DATABASE_URL)
        conn.autocommit = False
        cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    else:
        conn = _sqlite_conn()
        cur  = conn.cursor()
    try:
        yield conn, cur
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close()
        conn.close()


def _sqlite_conn():
    for path in ["/tmp/users.db", "/var/tmp/users.db",
                 os.path.expanduser("~/users.db")]:
        try:
            c = sqlite3.connect(path)
            c.row_factory = sqlite3.Row
            return c
        except Exception:
            continue
    raise RuntimeError("No writable SQLite path")


def _ph():
    return "%s" if _USE_POSTGRES else "?"


def init_db():
    with get_conn() as (conn, cur):
        if _USE_POSTGRES:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    username        TEXT PRIMARY KEY,
                    hashed_password TEXT NOT NULL,
                    deriv_token     TEXT DEFAULT NULL,
                    deriv_account   TEXT DEFAULT NULL,
                    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            # Add columns if they don't exist (for existing deployments).
            # Use savepoints so a failed ALTER doesn't abort the transaction.
            for col, defn in [
                ("deriv_token",   "TEXT DEFAULT NULL"),
                ("deriv_account", "TEXT DEFAULT NULL"),
                ("is_admin",      "BOOLEAN DEFAULT FALSE"),
                ("totp_secret",   "TEXT DEFAULT NULL"),
                ("totp_enabled",  "BOOLEAN DEFAULT FALSE"),
            ]:
                try:
                    cur.execute("SAVEPOINT add_col")
                    cur.execute(f"ALTER TABLE users ADD COLUMN {col} {defn}")
                    cur.execute("RELEASE SAVEPOINT add_col")
                except Exception:
                    cur.execute("ROLLBACK TO SAVEPOINT add_col")

            cur.execute("""
                CREATE TABLE IF NOT EXISTS signals (
                    id               SERIAL PRIMARY KEY,
                    symbol           TEXT,
                    type             TEXT,
                    price            REAL,
                    rsi              REAL,
                    bias             TEXT,
                    reason           TEXT,
                    confluence_score INTEGER DEFAULT 0,
                    username         TEXT DEFAULT NULL,
                    timestamp        TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS trade_results (
                    id          SERIAL PRIMARY KEY,
                    contract_id TEXT,
                    won         BOOLEAN,
                    pnl         REAL,
                    symbol      TEXT DEFAULT '1HZ100V',
                    username    TEXT DEFAULT NULL,
                    timestamp   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(contract_id, username)
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS subscriptions (
                    id           SERIAL PRIMARY KEY,
                    username     TEXT NOT NULL,
                    plan         TEXT NOT NULL,
                    status       TEXT NOT NULL DEFAULT 'pending',
                    payment_id   TEXT UNIQUE,
                    pay_address  TEXT,
                    pay_amount   REAL,
                    pay_currency TEXT,
                    price_usd    REAL,
                    expires_at   TIMESTAMP,
                    created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
        else:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    username        TEXT PRIMARY KEY,
                    hashed_password TEXT NOT NULL,
                    deriv_token     TEXT DEFAULT NULL,
                    deriv_account   TEXT DEFAULT NULL,
                    is_admin        INTEGER DEFAULT 0,
                    totp_secret     TEXT DEFAULT NULL,
                    totp_enabled    INTEGER DEFAULT 0,
                    created_at      DATETIME DEFAULT CURRENT_TIMESTAMP
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS signals (
                    id               INTEGER PRIMARY KEY AUTOINCREMENT,
                    symbol           TEXT,
                    type             TEXT,
                    price            REAL,
                    rsi              REAL,
                    bias             TEXT,
                    reason           TEXT,
                    confluence_score INTEGER DEFAULT 0,
                    username         TEXT DEFAULT NULL,
                    timestamp        DATETIME DEFAULT CURRENT_TIMESTAMP
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS trade_results (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    contract_id TEXT,
                    won         INTEGER,
                    pnl         REAL,
                    symbol      TEXT DEFAULT '1HZ100V',
                    username    TEXT DEFAULT NULL,
                    timestamp   DATETIME DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(contract_id, username)
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS subscriptions (
                    id           INTEGER PRIMARY KEY AUTOINCREMENT,
                    username     TEXT NOT NULL,
                    plan         TEXT NOT NULL,
                    status       TEXT NOT NULL DEFAULT 'pending',
                    payment_id   TEXT UNIQUE,
                    pay_address  TEXT,
                    pay_amount   REAL,
                    pay_currency TEXT,
                    price_usd    REAL,
                    expires_at   DATETIME,
                    created_at   DATETIME DEFAULT CURRENT_TIMESTAMP
                )
            """)
            for col, defn in [
                ("confluence_score", "INTEGER DEFAULT 0"),
                ("username",         "TEXT DEFAULT NULL"),
            ]:
                _add_col(cur, "signals", col, defn)
            for col, defn in [
                ("symbol",   "TEXT DEFAULT '1HZ100V'"),
                ("username", "TEXT DEFAULT NULL"),
            ]:
                _add_col(cur, "trade_results", col, defn)

    logger.info(f"✅ DB ready ({'PostgreSQL' if _USE_POSTGRES else 'SQLite'})")


def _add_col(cur, table, col, defn):
    try:
        cur.execute(f"ALTER TABLE {table} ADD COLUMN {col} {defn}")
    except Exception:
        pass


def fetchall(sql: str, params: tuple = ()) -> list[dict]:
    with get_conn() as (conn, cur):
        cur.execute(sql.replace("?", _ph()), params)
        return [dict(r) for r in cur.fetchall()]

def fetchone(sql: str, params: tuple = ()) -> dict | None:
    with get_conn() as (conn, cur):
        cur.execute(sql.replace("?", _ph()), params)
        r = cur.fetchone()
        return dict(r) if r else None

def execute(sql: str, params: tuple = ()):
    with get_conn() as (conn, cur):
        cur.execute(sql.replace("?", _ph()), params)


# ── User management ────────────────────────────────────────────────────────────

def get_user(username: str):
    return fetchone(
        "SELECT * FROM users WHERE username = ?", (username,)
    )

def user_exists(username: str) -> bool:
    return fetchone(
        "SELECT 1 FROM users WHERE username = ?", (username,)
    ) is not None

def create_user(username: str, hashed_password: str):
    execute(
        "INSERT INTO users (username, hashed_password) VALUES (?, ?)",
        (username, hashed_password),
    )

def save_deriv_token(username: str, token: str, account_id: str = ""):
    """Store the user's Deriv API token."""
    execute(
        "UPDATE users SET deriv_token = ?, deriv_account = ? WHERE username = ?",
        (str(token), str(account_id), str(username)),
    )

def get_deriv_token(username: str) -> str | None:
    """Retrieve the user's Deriv API token."""
    row = fetchone(
        "SELECT deriv_token FROM users WHERE username = ?", (username,)
    )
    return row["deriv_token"] if row else None

def is_admin(username: str) -> bool:
    row = fetchone("SELECT is_admin FROM users WHERE username = ?", (username,))
    if not row:
        return False
    val = row["is_admin"]
    return bool(val) if val is not None else False

def set_admin(username: str, value: bool):
    execute("UPDATE users SET is_admin = ? WHERE username = ?", (int(value), username))


# ── TOTP / 2FA ────────────────────────────────────────────────────────────────

def get_totp_data(username: str) -> dict | None:
    return fetchone(
        "SELECT totp_secret, totp_enabled FROM users WHERE username = ?",
        (username,),
    )

def save_totp_secret(username: str, secret: str):
    execute("UPDATE users SET totp_secret = ? WHERE username = ?", (secret, username))

def enable_totp(username: str):
    execute("UPDATE users SET totp_enabled = TRUE WHERE username = ?", (username,))

def disable_totp(username: str):
    execute(
        "UPDATE users SET totp_enabled = FALSE, totp_secret = NULL WHERE username = ?",
        (username,),
    )

def get_all_users_with_tokens() -> list[dict]:
    """Return all users who have connected a Deriv account."""
    return fetchall(
        "SELECT username, deriv_token, deriv_account FROM users "
        "WHERE deriv_token IS NOT NULL AND deriv_token != ''"
    )


# ── Signals ────────────────────────────────────────────────────────────────────

def insert_signal(symbol, sig_type, price, rsi, bias, reason,
                  confluence_score=0, username=None):
    execute(
        """
        INSERT INTO signals
          (symbol, type, price, rsi, bias, reason, confluence_score, username)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (str(symbol), str(sig_type), float(price), float(rsi),
         str(bias), str(reason), int(confluence_score),
         str(username) if username else None),
    )

def get_signals(limit: int = 30, username: str = None) -> list[dict]:
    """Return signals for this user OR legacy signals with no username."""
    if username:
        return fetchall(
            """
            SELECT * FROM signals
            WHERE username = ? OR username IS NULL
            ORDER BY timestamp DESC LIMIT ?
            """,
            (username, limit),
        )
    return fetchall(
        "SELECT * FROM signals ORDER BY timestamp DESC LIMIT ?", (limit,)
    )

def get_latest_bias(username: str = None) -> str:
    if username:
        row = fetchone(
            """
            SELECT bias FROM signals
            WHERE username = ? OR username IS NULL
            ORDER BY timestamp DESC LIMIT 1
            """,
            (username,),
        )
    else:
        row = fetchone(
            "SELECT bias FROM signals ORDER BY timestamp DESC LIMIT 1"
        )
    return row["bias"] if row else "Neutral"


# ── Trade results ──────────────────────────────────────────────────────────────

def insert_trade_result(contract_id, won, pnl,
                        symbol="1HZ100V", username=None):
    if _USE_POSTGRES:
        execute(
            """
            INSERT INTO trade_results (contract_id, won, pnl, symbol, username)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT (contract_id, username)
            DO UPDATE SET won=EXCLUDED.won, pnl=EXCLUDED.pnl
            """,
            (str(contract_id), bool(won), float(pnl),
             str(symbol), str(username) if username else None),
        )
    else:
        execute(
            """
            INSERT OR REPLACE INTO trade_results
              (contract_id, won, pnl, symbol, username)
            VALUES (?, ?, ?, ?, ?)
            """,
            (str(contract_id), int(won), float(pnl),
             str(symbol), str(username) if username else None),
        )

def get_trade_stats(username: str = None) -> dict:
    if username:
        row = fetchone(
            """
            SELECT COUNT(*) AS total,
                   SUM(CASE WHEN won THEN 1 ELSE 0 END) AS wins,
                   COALESCE(SUM(pnl), 0) AS total_pnl
            FROM trade_results
            WHERE username = ? OR username IS NULL
            """,
            (username,),
        )
    else:
        row = fetchone(
            """
            SELECT COUNT(*) AS total,
                   SUM(CASE WHEN won THEN 1 ELSE 0 END) AS wins,
                   COALESCE(SUM(pnl), 0) AS total_pnl
            FROM trade_results
            """
        )
    if not row or not row["total"]:
        return {"win_rate": 0.0, "total_trades": 0, "total_pnl": 0.0}
    total = int(row["total"])
    wins  = int(row["wins"] or 0)
    return {
        "win_rate":     round(wins / total * 100, 1),
        "total_trades": total,
        "total_pnl":    round(float(row["total_pnl"]), 2),
    }


# ── Subscriptions ──────────────────────────────────────────────────────────────

def get_active_subscription(username: str) -> dict | None:
    """Return the user's active subscription row, or None."""
    if _USE_POSTGRES:
        return fetchone(
            """
            SELECT * FROM subscriptions
            WHERE username = ?
              AND status = 'active'
              AND (expires_at IS NULL OR expires_at > NOW())
            ORDER BY created_at DESC LIMIT 1
            """,
            (username,),
        )
    return fetchone(
        """
        SELECT * FROM subscriptions
        WHERE username = ?
          AND status = 'active'
          AND (expires_at IS NULL OR expires_at > datetime('now'))
        ORDER BY created_at DESC LIMIT 1
        """,
        (username,),
    )


def create_pending_subscription(
    username: str, plan: str, payment_id: str,
    pay_address: str, pay_amount: float, pay_currency: str, price_usd: float,
):
    execute(
        """
        INSERT INTO subscriptions
          (username, plan, status, payment_id, pay_address, pay_amount, pay_currency, price_usd)
        VALUES (?, ?, 'pending', ?, ?, ?, ?, ?)
        """,
        (username, plan, payment_id, pay_address, pay_amount, pay_currency, price_usd),
    )


def get_subscription_by_payment(payment_id: str) -> dict | None:
    return fetchone(
        "SELECT * FROM subscriptions WHERE payment_id = ?", (payment_id,)
    )


def cancel_pending_subscription(payment_id: str, username: str):
    """Mark a pending subscription as cancelled. Only cancels if still pending and owned by user."""
    execute(
        "UPDATE subscriptions SET status = 'cancelled' WHERE payment_id = ? AND username = ? AND status = 'pending'",
        (payment_id, username),
    )


def activate_subscription(payment_id: str, plan: str):
    """Set subscription to active with the correct expiry for the plan."""
    from datetime import datetime, timedelta
    from payments import PLANS

    days = PLANS[plan]["days"]
    expires_at = datetime.utcnow() + timedelta(days=days) if days else None

    if expires_at:
        execute(
            "UPDATE subscriptions SET status = 'active', expires_at = ? WHERE payment_id = ?",
            (expires_at, payment_id),
        )
    else:
        execute(
            "UPDATE subscriptions SET status = 'active', expires_at = NULL WHERE payment_id = ?",
            (payment_id,),
        )


# ── Admin queries ──────────────────────────────────────────────────────────────

def admin_get_users() -> list[dict]:
    """All users with their latest subscription status."""
    if _USE_POSTGRES:
        return fetchall("""
            SELECT u.username, u.deriv_account, u.created_at,
                   s.plan, s.status AS sub_status, s.expires_at, s.price_usd
            FROM users u
            LEFT JOIN (
                SELECT DISTINCT ON (username) *
                FROM subscriptions
                ORDER BY username, created_at DESC
            ) s ON s.username = u.username
            ORDER BY u.created_at DESC
        """)
    return fetchall("""
        SELECT u.username, u.deriv_account, u.created_at,
               s.plan, s.status AS sub_status, s.expires_at, s.price_usd
        FROM users u
        LEFT JOIN (
            SELECT * FROM subscriptions
            GROUP BY username
            HAVING created_at = MAX(created_at)
        ) s ON s.username = u.username
        ORDER BY u.created_at DESC
    """)


def admin_get_subscriptions() -> list[dict]:
    """All subscriptions, newest first."""
    return fetchall("SELECT * FROM subscriptions ORDER BY created_at DESC")


def admin_get_stats() -> dict:
    """Aggregate stats for the admin overview."""
    users_row    = fetchone("SELECT COUNT(*) AS cnt FROM users")
    total_users  = int(users_row["cnt"]) if users_row else 0

    active_row   = fetchone("SELECT COUNT(*) AS cnt FROM subscriptions WHERE status = 'active'")
    active_subs  = int(active_row["cnt"]) if active_row else 0

    revenue_row  = fetchone("SELECT COALESCE(SUM(price_usd), 0) AS total FROM subscriptions WHERE status = 'active'")
    total_revenue = float(revenue_row["total"]) if revenue_row else 0.0

    plan_rows = fetchall("SELECT plan, COUNT(*) AS cnt FROM subscriptions WHERE status = 'active' GROUP BY plan")
    by_plan   = {r["plan"]: int(r["cnt"]) for r in plan_rows}

    return {
        "total_users":   total_users,
        "active_subs":   active_subs,
        "total_revenue": round(total_revenue, 2),
        "by_plan":       by_plan,
    }


def admin_revoke_subscription(sub_id: int):
    execute("UPDATE subscriptions SET status = 'revoked' WHERE id = ?", (sub_id,))


def admin_grant_subscription(username: str, plan: str):
    """Manually grant a free subscription (no payment required)."""
    import time as _time
    from datetime import datetime, timedelta
    from payments import PLANS
    days       = PLANS[plan]["days"]
    expires_at = datetime.utcnow() + timedelta(days=days) if days else None
    execute(
        """
        INSERT INTO subscriptions
          (username, plan, status, payment_id, pay_address, pay_amount, pay_currency, price_usd, expires_at)
        VALUES (?, ?, 'active', ?, '', 0, 'manual', 0, ?)
        """,
        (username, plan, f"manual_{username}_{int(_time.time())}", expires_at),
    )


init_db()