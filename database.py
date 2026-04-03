"""
database.py — persistent storage layer.

PostgreSQL on Render (DATABASE_URL), SQLite fallback for local dev.

Key fix: all numeric values are cast to native Python float()/int()/str()
before insert. PostgreSQL rejects numpy scalar types (np.float64, np.int64)
that pandas returns from DataFrame .iloc[] row access — it tries to interpret
the type name as a schema, producing "schema 'np' does not exist".
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


# ── Connection ─────────────────────────────────────────────────────────────────

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
    import sqlite3
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


# ── Schema ─────────────────────────────────────────────────────────────────────

def init_db():
    with get_conn() as (conn, cur):
        cur.execute("""
            CREATE TABLE IF NOT EXISTS users (
                username        TEXT PRIMARY KEY,
                hashed_password TEXT NOT NULL,
                created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        if _USE_POSTGRES:
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
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS trade_results (
                    id          SERIAL PRIMARY KEY,
                    contract_id TEXT UNIQUE,
                    won         BOOLEAN,
                    pnl         REAL,
                    symbol      TEXT DEFAULT '1HZ100V',
                    timestamp   TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
        else:
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
                    timestamp        DATETIME DEFAULT CURRENT_TIMESTAMP
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS trade_results (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    contract_id TEXT UNIQUE,
                    won         INTEGER,
                    pnl         REAL,
                    symbol      TEXT DEFAULT '1HZ100V',
                    timestamp   DATETIME DEFAULT CURRENT_TIMESTAMP
                )
            """)
            _add_col(cur, "signals",       "confluence_score", "INTEGER DEFAULT 0")
            _add_col(cur, "trade_results", "symbol",           "TEXT DEFAULT '1HZ100V'")

    logger.info(f"✅ DB ready ({'PostgreSQL' if _USE_POSTGRES else 'SQLite'})")


def _add_col(cur, table, col, defn):
    try:
        cur.execute(f"ALTER TABLE {table} ADD COLUMN {col} {defn}")
    except Exception:
        pass


# ── Helpers ────────────────────────────────────────────────────────────────────

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


# ── Domain ─────────────────────────────────────────────────────────────────────

def insert_signal(symbol, sig_type, price, rsi, bias, reason, confluence_score=0):
    """
    Cast all values to native Python types before insert.
    pandas returns np.float64/np.int64 from DataFrame rows — PostgreSQL
    rejects these and misreads the type name as a schema identifier.
    """
    execute(
        """
        INSERT INTO signals (symbol, type, price, rsi, bias, reason, confluence_score)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            str(symbol),
            str(sig_type),
            float(price),
            float(rsi),
            str(bias),
            str(reason),
            int(confluence_score),
        ),
    )

def get_signals(limit: int = 30) -> list[dict]:
    return fetchall(
        "SELECT * FROM signals ORDER BY timestamp DESC LIMIT ?", (limit,)
    )

def get_latest_bias() -> str:
    row = fetchone("SELECT bias FROM signals ORDER BY timestamp DESC LIMIT 1")
    return row["bias"] if row else "Neutral"

def insert_trade_result(contract_id, won, pnl, symbol="1HZ100V"):
    if _USE_POSTGRES:
        execute(
            """
            INSERT INTO trade_results (contract_id, won, pnl, symbol)
            VALUES (?, ?, ?, ?)
            ON CONFLICT (contract_id) DO UPDATE SET won=EXCLUDED.won, pnl=EXCLUDED.pnl
            """,
            (str(contract_id), bool(won), float(pnl), str(symbol)),
        )
    else:
        execute(
            "INSERT OR REPLACE INTO trade_results (contract_id, won, pnl, symbol) VALUES (?, ?, ?, ?)",
            (str(contract_id), int(won), float(pnl), str(symbol)),
        )

def get_trade_stats() -> dict:
    row = fetchone("""
        SELECT COUNT(*) AS total,
               SUM(CASE WHEN won THEN 1 ELSE 0 END) AS wins,
               COALESCE(SUM(pnl), 0) AS total_pnl
        FROM trade_results
    """)
    if not row or not row["total"]:
        return {"win_rate": 0.0, "total_trades": 0, "total_pnl": 0.0}
    total = int(row["total"])
    wins  = int(row["wins"] or 0)
    return {
        "win_rate":     round(wins / total * 100, 1),
        "total_trades": total,
        "total_pnl":    round(float(row["total_pnl"]), 2),
    }

def get_user(username):
    return fetchone(
        "SELECT username, hashed_password FROM users WHERE username = ?",
        (username,),
    )

def user_exists(username):
    return fetchone(
        "SELECT 1 FROM users WHERE username = ?", (username,)
    ) is not None

def create_user(username, hashed_password):
    execute(
        "INSERT INTO users (username, hashed_password) VALUES (?, ?)",
        (username, hashed_password),
    )


init_db()