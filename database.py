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
    from psycopg2.pool import ThreadedConnectionPool
    logger.info("✅ Using PostgreSQL (persistent)")
    # Reused across requests. Opening a fresh TCP+TLS+auth connection to
    # Supabase on every single query (the previous behavior) blocks the
    # single-worker event loop for the full handshake each time — under any
    # burst of concurrent requests (e.g. the admin panel firing 5 endpoints
    # at once on refresh) those blocking connects serialize one after
    # another and can pile up past a client's request timeout.
    #
    # minconn=0 — Supabase has paused/slept on this project before (see the
    # keepalive cron). A nonzero minconn opens connections immediately at
    # import time, which would crash the entire app on startup if Supabase
    # happened to be unreachable at that exact moment. With 0, the pool only
    # connects lazily on first use, same failure mode as before (one request
    # fails, not the whole process) but with pooling for every call after.
    _pool = ThreadedConnectionPool(0, 10, DATABASE_URL)
else:
    import sqlite3
    logger.warning("⚠️  DATABASE_URL not set — falling back to SQLite")


@contextmanager
def get_conn():
    if _USE_POSTGRES:
        conn = _pool.getconn()
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
        if _USE_POSTGRES:
            _pool.putconn(conn)
        else:
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
                ("display_name",  "TEXT DEFAULT NULL"),
                ("avatar_url",    "TEXT DEFAULT NULL"),
                ("bot_enabled",   "BOOLEAN DEFAULT TRUE"),
                ("trade_account_type", "TEXT DEFAULT 'real'"),
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
            for col, defn in [
                ("executed",      "BOOLEAN DEFAULT NULL"),
                ("features_json", "TEXT DEFAULT NULL"),
            ]:
                try:
                    cur.execute("SAVEPOINT add_col")
                    cur.execute(f"ALTER TABLE signals ADD COLUMN {col} {defn}")
                    cur.execute("RELEASE SAVEPOINT add_col")
                except Exception:
                    cur.execute("ROLLBACK TO SAVEPOINT add_col")
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
            for col, defn in [
                ("payment_method_id", "INTEGER DEFAULT NULL"),
                ("method_type",       "TEXT DEFAULT NULL"),
                ("proof_reference",   "TEXT DEFAULT NULL"),
                ("proof_image_url",   "TEXT DEFAULT NULL"),
                ("reviewed_by",       "TEXT DEFAULT NULL"),
                ("reviewed_at",       "TIMESTAMP DEFAULT NULL"),
                ("rejection_reason",  "TEXT DEFAULT NULL"),
            ]:
                try:
                    cur.execute("SAVEPOINT add_col")
                    cur.execute(f"ALTER TABLE subscriptions ADD COLUMN {col} {defn}")
                    cur.execute("RELEASE SAVEPOINT add_col")
                except Exception:
                    cur.execute("ROLLBACK TO SAVEPOINT add_col")
            cur.execute("""
                CREATE TABLE IF NOT EXISTS fcm_tokens (
                    token      TEXT PRIMARY KEY,
                    username   TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS bot_control (
                    id             INTEGER PRIMARY KEY CHECK (id = 1),
                    global_enabled BOOLEAN NOT NULL DEFAULT TRUE,
                    updated_by     TEXT,
                    updated_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            cur.execute("""
                INSERT INTO bot_control (id, global_enabled) VALUES (1, TRUE)
                ON CONFLICT (id) DO NOTHING
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS user_risk_state (
                    username           TEXT PRIMARY KEY,
                    consecutive_losses INTEGER  NOT NULL DEFAULT 0,
                    circuit_broken     BOOLEAN  NOT NULL DEFAULT FALSE,
                    daily_pnl          REAL     NOT NULL DEFAULT 0,
                    opening_balance    REAL     DEFAULT NULL,
                    last_reset_date    DATE     DEFAULT NULL,
                    updated_at         TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS password_resets (
                    id         SERIAL PRIMARY KEY,
                    username   TEXT NOT NULL,
                    code_hash  TEXT NOT NULL,
                    expires_at TIMESTAMP NOT NULL,
                    used       BOOLEAN NOT NULL DEFAULT FALSE,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS payment_methods (
                    id                  SERIAL PRIMARY KEY,
                    type                TEXT NOT NULL,
                    label               TEXT NOT NULL,
                    enabled             BOOLEAN NOT NULL DEFAULT TRUE,
                    sort_order          INTEGER NOT NULL DEFAULT 0,
                    instructions        TEXT DEFAULT NULL,
                    crypto_address      TEXT DEFAULT NULL,
                    crypto_network      TEXT DEFAULT NULL,
                    crypto_currency     TEXT DEFAULT NULL,
                    bank_name           TEXT DEFAULT NULL,
                    bank_account_name   TEXT DEFAULT NULL,
                    bank_account_number TEXT DEFAULT NULL,
                    bank_routing_number TEXT DEFAULT NULL,
                    bank_swift          TEXT DEFAULT NULL,
                    bank_iban           TEXT DEFAULT NULL,
                    bank_currency       TEXT DEFAULT NULL,
                    created_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            for col, defn in [
                ("logo_url", "TEXT DEFAULT NULL"),
            ]:
                try:
                    cur.execute("SAVEPOINT add_col")
                    cur.execute(f"ALTER TABLE payment_methods ADD COLUMN {col} {defn}")
                    cur.execute("RELEASE SAVEPOINT add_col")
                except Exception:
                    cur.execute("ROLLBACK TO SAVEPOINT add_col")
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
                    display_name    TEXT DEFAULT NULL,
                    avatar_url      TEXT DEFAULT NULL,
                    bot_enabled     INTEGER DEFAULT 1,
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
            cur.execute("""
                CREATE TABLE IF NOT EXISTS fcm_tokens (
                    token      TEXT PRIMARY KEY,
                    username   TEXT NOT NULL,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS bot_control (
                    id             INTEGER PRIMARY KEY CHECK (id = 1),
                    global_enabled INTEGER NOT NULL DEFAULT 1,
                    updated_by     TEXT,
                    updated_at     DATETIME DEFAULT CURRENT_TIMESTAMP
                )
            """)
            cur.execute("""
                INSERT OR IGNORE INTO bot_control (id, global_enabled) VALUES (1, 1)
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS user_risk_state (
                    username           TEXT PRIMARY KEY,
                    consecutive_losses INTEGER NOT NULL DEFAULT 0,
                    circuit_broken     INTEGER NOT NULL DEFAULT 0,
                    daily_pnl          REAL    NOT NULL DEFAULT 0,
                    opening_balance    REAL    DEFAULT NULL,
                    last_reset_date    TEXT    DEFAULT NULL,
                    updated_at         DATETIME DEFAULT CURRENT_TIMESTAMP
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS password_resets (
                    id         INTEGER PRIMARY KEY AUTOINCREMENT,
                    username   TEXT NOT NULL,
                    code_hash  TEXT NOT NULL,
                    expires_at DATETIME NOT NULL,
                    used       INTEGER NOT NULL DEFAULT 0,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS payment_methods (
                    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                    type                TEXT NOT NULL,
                    label               TEXT NOT NULL,
                    enabled             INTEGER NOT NULL DEFAULT 1,
                    sort_order          INTEGER NOT NULL DEFAULT 0,
                    instructions        TEXT DEFAULT NULL,
                    crypto_address      TEXT DEFAULT NULL,
                    crypto_network      TEXT DEFAULT NULL,
                    crypto_currency     TEXT DEFAULT NULL,
                    bank_name           TEXT DEFAULT NULL,
                    bank_account_name   TEXT DEFAULT NULL,
                    bank_account_number TEXT DEFAULT NULL,
                    bank_routing_number TEXT DEFAULT NULL,
                    bank_swift          TEXT DEFAULT NULL,
                    bank_iban           TEXT DEFAULT NULL,
                    bank_currency       TEXT DEFAULT NULL,
                    created_at          DATETIME DEFAULT CURRENT_TIMESTAMP,
                    updated_at          DATETIME DEFAULT CURRENT_TIMESTAMP
                )
            """)
            for col, defn in [
                ("display_name", "TEXT DEFAULT NULL"),
                ("avatar_url",   "TEXT DEFAULT NULL"),
                ("bot_enabled",  "INTEGER DEFAULT 1"),
                ("trade_account_type", "TEXT DEFAULT 'real'"),
            ]:
                _add_col(cur, "users", col, defn)
            for col, defn in [
                ("confluence_score", "INTEGER DEFAULT 0"),
                ("username",         "TEXT DEFAULT NULL"),
                ("executed",         "INTEGER DEFAULT NULL"),
                ("features_json",    "TEXT DEFAULT NULL"),
            ]:
                _add_col(cur, "signals", col, defn)
            for col, defn in [
                ("symbol",   "TEXT DEFAULT '1HZ100V'"),
                ("username", "TEXT DEFAULT NULL"),
            ]:
                _add_col(cur, "trade_results", col, defn)
            for col, defn in [
                ("payment_method_id", "INTEGER DEFAULT NULL"),
                ("method_type",       "TEXT DEFAULT NULL"),
                ("proof_reference",   "TEXT DEFAULT NULL"),
                ("proof_image_url",   "TEXT DEFAULT NULL"),
                ("reviewed_by",       "TEXT DEFAULT NULL"),
                ("reviewed_at",       "DATETIME DEFAULT NULL"),
                ("rejection_reason",  "TEXT DEFAULT NULL"),
            ]:
                _add_col(cur, "subscriptions", col, defn)
            for col, defn in [
                ("logo_url", "TEXT DEFAULT NULL"),
            ]:
                _add_col(cur, "payment_methods", col, defn)

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
    # Pass the raw bool, not int(value) — psycopg2 does client-side SQL text
    # substitution, so int(True) becomes the literal `1` in the query text,
    # and Postgres rejects assigning a bare integer to a BOOLEAN column
    # ("column is of type boolean but expression is of type integer").
    # SQLite has no such restriction either way. bool works natively on both.
    execute("UPDATE users SET is_admin = ? WHERE username = ?", (bool(value), username))

def set_password(username: str, hashed_password: str):
    execute(
        "UPDATE users SET hashed_password = ? WHERE username = ?",
        (hashed_password, username),
    )


# ── Profile ──────────────────────────────────────────────────────────────────

def update_display_name(username: str, display_name: str):
    execute(
        "UPDATE users SET display_name = ? WHERE username = ?",
        (display_name, username),
    )

def update_avatar_url(username: str, avatar_url: str):
    execute(
        "UPDATE users SET avatar_url = ? WHERE username = ?",
        (avatar_url, username),
    )


# ── Password reset ─────────────────────────────────────────────────────────────

def create_password_reset(username: str, code_hash: str, expires_at):
    """Invalidate any prior unused codes for this user, then store the new one —
    only the most recently requested code is ever valid."""
    execute(
        "DELETE FROM password_resets WHERE username = ? AND used = ?",
        (username, False),
    )
    execute(
        "INSERT INTO password_resets (username, code_hash, expires_at) VALUES (?, ?, ?)",
        (username, code_hash, expires_at),
    )

def get_valid_password_reset(username: str, code_hash: str, now) -> dict | None:
    return fetchone(
        "SELECT * FROM password_resets WHERE username = ? AND code_hash = ? "
        "AND used = ? AND expires_at > ?",
        (username, code_hash, False, now),
    )

def mark_password_reset_used(reset_id: int):
    execute("UPDATE password_resets SET used = ? WHERE id = ?", (True, reset_id))


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
    """Return all users who have connected a Deriv account, with their
    current circuit-breaker state (defaults to not-broken if no risk-state
    row exists yet)."""
    default = "FALSE" if _USE_POSTGRES else "0"
    return fetchall(f"""
        SELECT u.username, u.deriv_token, u.deriv_account, u.bot_enabled,
               u.trade_account_type,
               COALESCE(s.circuit_broken, {default}) AS circuit_broken
        FROM users u
        LEFT JOIN user_risk_state s ON s.username = u.username
        WHERE u.deriv_token IS NOT NULL AND u.deriv_token != ''
    """)

def set_trade_account_type(username: str, account_type: str):
    """account_type must be 'demo' or 'real' — validated by the caller."""
    execute(
        "UPDATE users SET trade_account_type = ? WHERE username = ?",
        (account_type, username),
    )


# ── Bot control ─────────────────────────────────────────────────────────────────

def get_global_bot_enabled() -> bool:
    row = fetchone("SELECT global_enabled FROM bot_control WHERE id = 1")
    if not row:
        return True
    return bool(row["global_enabled"])

def set_global_bot_enabled(enabled: bool, updated_by: str):
    # Raw bool, not int() — see set_admin for why int() breaks on Postgres.
    execute(
        "UPDATE bot_control SET global_enabled = ?, updated_by = ?, "
        "updated_at = CURRENT_TIMESTAMP WHERE id = 1",
        (bool(enabled), updated_by),
    )

def set_user_bot_enabled(username: str, enabled: bool):
    # Raw bool, not int() — see set_admin for why int() breaks on Postgres.
    execute(
        "UPDATE users SET bot_enabled = ? WHERE username = ?",
        (bool(enabled), username),
    )


# ── Per-user risk state (circuit breaker / daily PnL) ────────────────────────────
# Isolated per user so one account's losing streak can't pause trading for
# everyone else — see project_bot_control_switches memory for the history.

def get_user_risk_state(username: str) -> dict:
    row = fetchone("SELECT * FROM user_risk_state WHERE username = ?", (username,))
    if not row:
        return {
            "username": username,
            "consecutive_losses": 0,
            "circuit_broken": False,
            "daily_pnl": 0.0,
            "opening_balance": None,
            "last_reset_date": None,
        }
    return row


def reset_daily_risk_flags(usernames: list[str], today_iso: str):
    """Cheap, DB-only daily reset (no Deriv API call) run once per cycle over
    every connected user, not just ones who end up trading — otherwise a
    circuit-broken user who's excluded from trading would never get a chance
    to be unstuck at UTC midnight."""
    if not usernames:
        return
    with get_conn() as (conn, cur):
        if _USE_POSTGRES:
            cur.executemany(
                "INSERT INTO user_risk_state (username) VALUES (%s) "
                "ON CONFLICT (username) DO NOTHING",
                [(u,) for u in usernames],
            )
            placeholders = ",".join(["%s"] * len(usernames))
            cur.execute(
                f"""UPDATE user_risk_state
                    SET daily_pnl = 0, consecutive_losses = 0, circuit_broken = FALSE,
                        opening_balance = NULL, last_reset_date = %s,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE username IN ({placeholders})
                      AND (last_reset_date IS NULL OR last_reset_date <> %s)""",
                [today_iso, *usernames, today_iso],
            )
        else:
            cur.executemany(
                "INSERT OR IGNORE INTO user_risk_state (username) VALUES (?)",
                [(u,) for u in usernames],
            )
            placeholders = ",".join(["?"] * len(usernames))
            cur.execute(
                f"""UPDATE user_risk_state
                    SET daily_pnl = 0, consecutive_losses = 0, circuit_broken = 0,
                        opening_balance = NULL, last_reset_date = ?,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE username IN ({placeholders})
                      AND (last_reset_date IS NULL OR last_reset_date <> ?)""",
                [today_iso, *usernames, today_iso],
            )


def maybe_set_opening_balance(username: str, balance: float):
    """Stamps the day's opening balance the first time it's known — a single
    atomic UPDATE, no read-modify-write, safe regardless of concurrent callers."""
    if _USE_POSTGRES:
        execute(
            "INSERT INTO user_risk_state (username) VALUES (?) "
            "ON CONFLICT (username) DO NOTHING", (username,)
        )
    else:
        execute("INSERT OR IGNORE INTO user_risk_state (username) VALUES (?)", (username,))
    execute(
        "UPDATE user_risk_state SET opening_balance = ? "
        "WHERE username = ? AND opening_balance IS NULL",
        (float(balance), username),
    )


def record_trade_settlement(
    username: str, won: bool, profit: float,
    max_consecutive_losses: int, max_daily_drawdown_pct: float,
) -> dict:
    """
    Atomically updates one user's consecutive_losses/daily_pnl/circuit_broken
    after a trade settles. Does NOT touch trade_results — that's handled
    separately by insert_trade_result, unchanged.

    Uses SELECT ... FOR UPDATE (Postgres) to hold the row lock for the whole
    transaction, so two settlements for the SAME user overlapping in time
    (possible since contract monitors are fire-and-forget and can outlive a
    single trade cycle) are serialized by the database, not by Python.
    """
    with get_conn() as (conn, cur):
        if _USE_POSTGRES:
            cur.execute(
                "INSERT INTO user_risk_state (username) VALUES (%s) "
                "ON CONFLICT (username) DO NOTHING", (username,)
            )
            cur.execute(
                "SELECT consecutive_losses, circuit_broken, daily_pnl, opening_balance "
                "FROM user_risk_state WHERE username = %s FOR UPDATE",
                (username,),
            )
        else:
            cur.execute(
                "INSERT OR IGNORE INTO user_risk_state (username) VALUES (?)", (username,)
            )
            cur.execute(
                "SELECT consecutive_losses, circuit_broken, daily_pnl, opening_balance "
                "FROM user_risk_state WHERE username = ?",
                (username,),
            )

        old = dict(cur.fetchone())
        new_losses    = 0 if won else int(old["consecutive_losses"]) + 1
        new_daily_pnl = float(old["daily_pnl"]) + float(profit)
        was_broken    = bool(old["circuit_broken"])
        circuit_broken, newly_tripped = was_broken, False

        if not was_broken:
            if not won and new_losses >= max_consecutive_losses:
                circuit_broken, newly_tripped = True, True
            elif old["opening_balance"] and float(old["opening_balance"]) > 0 and new_daily_pnl < 0:
                dd = abs(new_daily_pnl) / float(old["opening_balance"]) * 100
                if dd >= max_daily_drawdown_pct:
                    circuit_broken, newly_tripped = True, True

        ph = "%s" if _USE_POSTGRES else "?"
        cur.execute(
            f"UPDATE user_risk_state SET consecutive_losses = {ph}, daily_pnl = {ph}, "
            f"circuit_broken = {ph} WHERE username = {ph}",
            (
                new_losses,
                new_daily_pnl,
                circuit_broken if _USE_POSTGRES else int(circuit_broken),
                username,
            ),
        )

    return {
        "consecutive_losses": new_losses,
        "daily_pnl": new_daily_pnl,
        "circuit_broken": circuit_broken,
        "newly_tripped": newly_tripped,
    }


def reset_user_circuit_breaker(username: str):
    """Admin manual unstick — clears one user's breaker without waiting for
    the UTC daily reset. Also clears daily_pnl/opening_balance, not just the
    breaker flag: leaving daily_pnl deeply negative would immediately re-trip
    the drawdown check on the very next settlement, making a partial reset
    pointless."""
    if _USE_POSTGRES:
        execute(
            "INSERT INTO user_risk_state (username) VALUES (?) "
            "ON CONFLICT (username) DO NOTHING", (username,)
        )
    else:
        execute("INSERT OR IGNORE INTO user_risk_state (username) VALUES (?)", (username,))
    execute(
        "UPDATE user_risk_state SET circuit_broken = ?, consecutive_losses = ?, "
        "daily_pnl = ?, opening_balance = NULL WHERE username = ?",
        (0, 0, 0.0, username),
    )


# ── FCM push tokens ─────────────────────────────────────────────────────────────
# Persisted (not in-memory) so tokens survive process restarts/redeploys —
# otherwise a Render restart silently drops everyone's push notifications
# until they reopen the app and re-register.

def save_fcm_token(username: str, token: str):
    execute(
        """INSERT INTO fcm_tokens (token, username) VALUES (?, ?)
           ON CONFLICT (token) DO UPDATE SET username = excluded.username""",
        (token, username),
    )

def delete_fcm_token(token: str):
    execute("DELETE FROM fcm_tokens WHERE token = ?", (token,))

def get_fcm_tokens(username: str = None) -> list[str]:
    if username:
        rows = fetchall("SELECT token FROM fcm_tokens WHERE username = ?", (username,))
    else:
        rows = fetchall("SELECT token FROM fcm_tokens")
    return [r["token"] for r in rows]


# ── Signals ────────────────────────────────────────────────────────────────────

def insert_signal(symbol, sig_type, price, rsi, bias, reason,
                  confluence_score=0, username=None, features_json=None) -> int | None:
    """Returns the new row's id so the caller can later record whether a
    BUY/SELL signal actually resulted in an executed trade (see
    update_signal_executed) — the signal is saved before that's known.

    features_json captures the full indicator vector (EMAs, MACD, ADX/DI,
    BB%, ATR, HTF biases, BOS direction) behind this signal's score, not
    just the derived score/reason — needed so a future model can be fit
    against real outcomes once enough live trade history accumulates."""
    params = (str(symbol), str(sig_type), float(price), float(rsi),
              str(bias), str(reason), int(confluence_score),
              str(username) if username else None,
              str(features_json) if features_json else None)
    with get_conn() as (conn, cur):
        if _USE_POSTGRES:
            cur.execute(
                """
                INSERT INTO signals
                  (symbol, type, price, rsi, bias, reason, confluence_score, username, features_json)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id
                """,
                params,
            )
            row = cur.fetchone()
            return row["id"] if row else None
        else:
            cur.execute(
                """
                INSERT INTO signals
                  (symbol, type, price, rsi, bias, reason, confluence_score, username, features_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                params,
            )
            return cur.lastrowid

def update_signal_executed(signal_id: int, executed: bool):
    execute("UPDATE signals SET executed = ? WHERE id = ?", (bool(executed), signal_id))

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
    username: str, plan: str, payment_id: str, payment_method_id: int, method_type: str,
    pay_address: str, pay_amount: float, pay_currency: str, price_usd: float,
):
    execute(
        """
        INSERT INTO subscriptions
          (username, plan, status, payment_id, payment_method_id, method_type,
           pay_address, pay_amount, pay_currency, price_usd)
        VALUES (?, ?, 'pending', ?, ?, ?, ?, ?, ?, ?)
        """,
        (username, plan, payment_id, payment_method_id, method_type,
         pay_address, pay_amount, pay_currency, price_usd),
    )


def get_subscription_by_payment(payment_id: str) -> dict | None:
    return fetchone(
        "SELECT * FROM subscriptions WHERE payment_id = ?", (payment_id,)
    )


def get_subscription_by_id(sub_id: int) -> dict | None:
    return fetchone(
        "SELECT * FROM subscriptions WHERE id = ?", (sub_id,)
    )


def get_pending_subscription_for_user(username: str) -> dict | None:
    """A subscription this user has already started paying for (created or
    proof submitted, not yet resolved) — used to block starting a second
    order while one is already in flight."""
    return fetchone(
        """
        SELECT * FROM subscriptions
        WHERE username = ? AND status IN ('pending', 'pending_review')
        ORDER BY created_at DESC LIMIT 1
        """,
        (username,),
    )


def cancel_pending_subscription(payment_id: str, username: str):
    """Mark a pending subscription as cancelled. Only cancels if still pending and owned by user.
    Deliberately does NOT cancel 'pending_review' — once proof is submitted,
    only admin approve/reject should resolve it, preserving an audit trail."""
    execute(
        "UPDATE subscriptions SET status = 'cancelled' WHERE payment_id = ? AND username = ? AND status = 'pending'",
        (payment_id, username),
    )


def submit_payment_proof(payment_id: str, reference: str, image_url: str | None):
    """Moves an order into the admin review queue. Allowed from 'pending' (first
    submission) or 'rejected' (correcting and resubmitting on the same order) —
    clears any prior rejection so it reads as a fresh review request."""
    execute(
        """
        UPDATE subscriptions
        SET status = 'pending_review', proof_reference = ?, proof_image_url = ?,
            rejection_reason = NULL, reviewed_by = NULL, reviewed_at = NULL
        WHERE payment_id = ? AND status IN ('pending', 'rejected')
        """,
        (reference, image_url, payment_id),
    )


def _expiry_for_plan(plan: str):
    """Shared by admin_approve_subscription and admin_grant_subscription."""
    from datetime import datetime, timedelta
    from payments import PLANS
    days = PLANS[plan]["days"]
    return datetime.utcnow() + timedelta(days=days) if days else None


def admin_approve_subscription(sub_id: int, plan: str, admin_username: str):
    """Approve a pending_review order — activates the subscription with the correct expiry."""
    expires_at = _expiry_for_plan(plan)
    execute(
        """
        UPDATE subscriptions
        SET status = 'active', expires_at = ?, reviewed_by = ?, reviewed_at = CURRENT_TIMESTAMP
        WHERE id = ?
        """,
        (expires_at, admin_username, sub_id),
    )


def admin_reject_subscription(sub_id: int, admin_username: str, reason: str):
    execute(
        """
        UPDATE subscriptions
        SET status = 'rejected', rejection_reason = ?, reviewed_by = ?, reviewed_at = CURRENT_TIMESTAMP
        WHERE id = ?
        """,
        (reason, admin_username, sub_id),
    )


# ── Payment methods ────────────────────────────────────────────────────────────

def get_payment_methods(enabled_only: bool = True) -> list[dict]:
    if enabled_only:
        return fetchall(
            "SELECT * FROM payment_methods WHERE enabled = ? ORDER BY sort_order, id",
            (True,),
        )
    return fetchall("SELECT * FROM payment_methods ORDER BY sort_order, id")


def get_payment_method(method_id: int) -> dict | None:
    return fetchone("SELECT * FROM payment_methods WHERE id = ?", (method_id,))


def create_payment_method(
    type: str, label: str, enabled: bool = True, sort_order: int = 0,
    instructions: str = None,
    crypto_address: str = None, crypto_network: str = None, crypto_currency: str = None,
    bank_name: str = None, bank_account_name: str = None, bank_account_number: str = None,
    bank_routing_number: str = None, bank_swift: str = None, bank_iban: str = None,
    bank_currency: str = None, logo_url: str = None,
) -> int | None:
    params = (
        type, label, bool(enabled), int(sort_order), instructions,
        crypto_address, crypto_network, crypto_currency,
        bank_name, bank_account_name, bank_account_number,
        bank_routing_number, bank_swift, bank_iban, bank_currency, logo_url,
    )
    with get_conn() as (conn, cur):
        if _USE_POSTGRES:
            cur.execute(
                """
                INSERT INTO payment_methods
                  (type, label, enabled, sort_order, instructions,
                   crypto_address, crypto_network, crypto_currency,
                   bank_name, bank_account_name, bank_account_number,
                   bank_routing_number, bank_swift, bank_iban, bank_currency, logo_url)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id
                """,
                params,
            )
            row = cur.fetchone()
            return row["id"] if row else None
        else:
            cur.execute(
                """
                INSERT INTO payment_methods
                  (type, label, enabled, sort_order, instructions,
                   crypto_address, crypto_network, crypto_currency,
                   bank_name, bank_account_name, bank_account_number,
                   bank_routing_number, bank_swift, bank_iban, bank_currency, logo_url)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                params,
            )
            return cur.lastrowid


def update_payment_method(
    method_id: int, type: str, label: str, enabled: bool, sort_order: int,
    instructions: str = None,
    crypto_address: str = None, crypto_network: str = None, crypto_currency: str = None,
    bank_name: str = None, bank_account_name: str = None, bank_account_number: str = None,
    bank_routing_number: str = None, bank_swift: str = None, bank_iban: str = None,
    bank_currency: str = None, logo_url: str = None,
):
    execute(
        """
        UPDATE payment_methods
        SET type = ?, label = ?, enabled = ?, sort_order = ?, instructions = ?,
            crypto_address = ?, crypto_network = ?, crypto_currency = ?,
            bank_name = ?, bank_account_name = ?, bank_account_number = ?,
            bank_routing_number = ?, bank_swift = ?, bank_iban = ?, bank_currency = ?,
            logo_url = ?, updated_at = CURRENT_TIMESTAMP
        WHERE id = ?
        """,
        (type, label, bool(enabled), int(sort_order), instructions,
         crypto_address, crypto_network, crypto_currency,
         bank_name, bank_account_name, bank_account_number,
         bank_routing_number, bank_swift, bank_iban, bank_currency, logo_url,
         method_id),
    )


def delete_payment_method(method_id: int):
    """Safe to delete regardless of history — subscriptions snapshot their own
    copy of the destination details (pay_address/pay_currency/method_type) at
    creation time, so deleting the source method never corrupts past orders."""
    execute("DELETE FROM payment_methods WHERE id = ?", (method_id,))


# ── Admin queries ──────────────────────────────────────────────────────────────

def admin_get_users() -> list[dict]:
    """All users with their latest subscription status."""
    if _USE_POSTGRES:
        return fetchall("""
            SELECT u.username, u.deriv_account, u.created_at, u.avatar_url,
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
        SELECT u.username, u.deriv_account, u.created_at, u.avatar_url,
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
    expires_at = _expiry_for_plan(plan)
    execute(
        """
        INSERT INTO subscriptions
          (username, plan, status, payment_id, pay_address, pay_amount, pay_currency, price_usd, expires_at)
        VALUES (?, ?, 'active', ?, '', 0, 'manual', 0, ?)
        """,
        (username, plan, f"manual_{username}_{int(_time.time())}", expires_at),
    )


def get_admin_usernames() -> list[str]:
    rows = fetchall("SELECT username FROM users WHERE is_admin = ?", (True,))
    return [r["username"] for r in rows]


init_db()