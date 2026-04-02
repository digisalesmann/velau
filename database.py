"""
database.py — persistent storage layer.

Uses PostgreSQL (via DATABASE_URL env var on Render) with automatic
fallback to SQLite for local development. All table creation and
migrations are handled here so user_models.py stays clean.

Install: pip install psycopg2-binary
"""
import os
import sqlite3
import logging
from contextlib import contextmanager

logger = logging.getLogger("Database")

DATABASE_URL = os.getenv("DATABASE_URL", "")
_USE_POSTGRES = bool(DATABASE_URL)

if _USE_POSTGRES:
    import psycopg2
    import psycopg2.extras
    logger.info("✅ Using PostgreSQL (persistent)")
else:
    logger.warning("⚠️  DATABASE_URL not set — falling back to SQLite (data resets on redeploy)")


# ── Connection factory ─────────────────────────────────────────────────────────

def _get_postgres():
    """Return a psycopg2 connection."""
    conn = psycopg2.connect(DATABASE_URL)
    conn.autocommit = False
    return conn

def _get_sqlite():
    """Return a sqlite3 connection with row_factory set."""
    for path in ["/tmp/users.db", "/var/tmp/users.db", os.path.expanduser("~/users.db")]:
        try:
            conn = sqlite3.connect(path)
            conn.row_factory = sqlite3.Row
            return conn
        except Exception:
            continue
    raise RuntimeError("No writable SQLite path found")

@contextmanager
def get_conn():
    """
    Context manager that yields a connection and cursor,
    commits on success, rolls back on exception.

    Usage:
        with get_conn() as (conn, cur):
            cur.execute("SELECT 1")
    """
    if _USE_POSTGRES:
        conn = _get_postgres()
        cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    else:
        conn = _get_sqlite()
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


# ── Placeholder token ─────────────────────────────────────────────────────────
# PostgreSQL uses %s, SQLite uses ?
def _ph():
    return "%s" if _USE_POSTGRES else "?"


# ── Schema init ───────────────────────────────────────────────────────────────

def init_db():
    """Create all tables and run safe migrations. Idempotent."""
    with get_conn() as (conn, cur):

        # Users
        cur.execute("""
            CREATE TABLE IF NOT EXISTS users (
                username        TEXT PRIMARY KEY,
                hashed_password TEXT NOT NULL,
                created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Signals — bot's internal monologue
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
                timestamp        TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """ if _USE_POSTGRES else """
            CREATE TABLE IF NOT EXISTS signals (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol           TEXT,
                type             TEXT,
                price            REAL,
                rsi              REAL,
                bias             TEXT,
                reason           TEXT,
                confluence_score INTEGER DEFAULT 0,
                timestamp        DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Trade results — settled contract outcomes
        cur.execute("""
            CREATE TABLE IF NOT EXISTS trade_results (
                id          SERIAL PRIMARY KEY,
                contract_id TEXT UNIQUE,
                won         BOOLEAN,
                pnl         REAL,
                symbol      TEXT DEFAULT '1HZ100V',
                timestamp   TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """ if _USE_POSTGRES else """
            CREATE TABLE IF NOT EXISTS trade_results (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                contract_id TEXT UNIQUE,
                won         INTEGER,
                pnl         REAL,
                symbol      TEXT DEFAULT '1HZ100V',
                timestamp   DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Safe migrations for existing SQLite deployments
        if not _USE_POSTGRES:
            _safe_add_column(cur, "signals", "confluence_score", "INTEGER DEFAULT 0")
            _safe_add_column(cur, "trade_results", "symbol", "TEXT DEFAULT '1HZ100V'")

    logger.info(f"✅ DB initialized ({'PostgreSQL' if _USE_POSTGRES else 'SQLite'})")


def _safe_add_column(cur, table: str, column: str, definition: str):
    """Add a column if it doesn't exist (SQLite only)."""
    try:
        cur.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
    except Exception:
        pass  # Already exists


# ── Query helpers ─────────────────────────────────────────────────────────────

def fetchall(sql: str, params: tuple = ()) -> list[dict]:
    with get_conn() as (conn, cur):
        cur.execute(sql.replace("?", _ph()), params)
        rows = cur.fetchall()
        return [dict(r) for r in rows]

def fetchone(sql: str, params: tuple = ()) -> dict | None:
    with get_conn() as (conn, cur):
        cur.execute(sql.replace("?", _ph()), params)
        row = cur.fetchone()
        return dict(row) if row else None

def execute(sql: str, params: tuple = ()):
    with get_conn() as (conn, cur):
        cur.execute(sql.replace("?", _ph()), params)


# ── Domain helpers ────────────────────────────────────────────────────────────

def insert_signal(
    symbol: str, sig_type: str, price: float,
    rsi: float, bias: str, reason: str, confluence_score: int = 0
):
    execute(
        """
        INSERT INTO signals (symbol, type, price, rsi, bias, reason, confluence_score)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (symbol, sig_type, round(price, 4), round(rsi, 2), bias, reason, confluence_score),
    )

def get_signals(limit: int = 30) -> list[dict]:
    return fetchall(
        "SELECT * FROM signals ORDER BY timestamp DESC LIMIT ?",
        (limit,),
    )

def get_latest_bias() -> str:
    row = fetchone("SELECT bias FROM signals ORDER BY timestamp DESC LIMIT 1")
    return row["bias"] if row else "Neutral"

def insert_trade_result(contract_id: str, won: bool, pnl: float, symbol: str = "1HZ100V"):
    execute(
        """
        INSERT INTO trade_results (contract_id, won, pnl, symbol)
        VALUES (?, ?, ?, ?)
        ON CONFLICT (contract_id) DO UPDATE SET won=EXCLUDED.won, pnl=EXCLUDED.pnl
        """ if _USE_POSTGRES else """
        INSERT OR REPLACE INTO trade_results (contract_id, won, pnl, symbol)
        VALUES (?, ?, ?, ?)
        """,
        (contract_id, won, round(pnl, 2), symbol),
    )

def get_trade_stats() -> dict:
    """Return win_rate, total_trades, total_pnl from trade_results."""
    row = fetchone("""
        SELECT
            COUNT(*)                                       AS total,
            SUM(CASE WHEN won THEN 1 ELSE 0 END)          AS wins,
            COALESCE(SUM(pnl), 0)                          AS total_pnl
        FROM trade_results
    """)
    if not row or not row["total"]:
        return {"win_rate": 0.0, "total_trades": 0, "total_pnl": 0.0}
    total = row["total"]
    wins  = row["wins"] or 0
    return {
        "win_rate":    round(wins / total * 100, 1),
        "total_trades": total,
        "total_pnl":   round(row["total_pnl"], 2),
    }


# ── User helpers ──────────────────────────────────────────────────────────────

def get_user(username: str) -> dict | None:
    return fetchone(
        "SELECT username, hashed_password FROM users WHERE username = ?",
        (username,),
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


# Initialise on import
init_db()