"""
User and session management for FastAPI backend.

Auth fix: passlib is unmaintained and broken with bcrypt>=4.0 on Python 3.14
(AttributeError: module 'bcrypt' has no attribute '__about__').
Replaced with pwdlib which is actively maintained and works correctly.
"""
from fastapi import APIRouter, HTTPException, Depends, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel
from jose import JWTError, jwt
from datetime import datetime, timedelta
from typing import Optional
import os
import sqlite3

from pwdlib import PasswordHash
from pwdlib.hashers.bcrypt import BcryptHasher

pwd_context = PasswordHash([BcryptHasher()])

SECRET_KEY = os.getenv("SECRET_KEY", "REPLACE_WITH_A_SECURE_RANDOM_KEY")
ALGORITHM  = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 60 * 24 * 7  # 7 days

security = HTTPBearer()
router   = APIRouter()


# ── DB PATH ────────────────────────────────────────────────────────────────────
def get_db_path() -> str:
    for path in [
        "/tmp/users.db",
        "/var/tmp/users.db",
        os.path.expanduser("~/users.db"),
    ]:
        try:
            conn = sqlite3.connect(path)
            conn.execute("CREATE TABLE IF NOT EXISTS _test (id INTEGER)")
            conn.execute("DROP TABLE _test")
            conn.close()
            print(f"Using DB at: {path}")
            return path
        except Exception as e:
            print(f"Cannot use {path}: {e}")
    raise RuntimeError("No writable path found for SQLite DB")


DB_PATH = get_db_path()


def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


# ── SCHEMA ─────────────────────────────────────────────────────────────────────
def init_db():
    conn = get_db()

    conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            username        TEXT PRIMARY KEY,
            hashed_password TEXT NOT NULL
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS signals (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol          TEXT,
            type            TEXT,
            price           REAL,
            rsi             REAL,
            bias            TEXT,
            reason          TEXT,
            timestamp       DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # Safe migration for existing deployments — SQLite doesn't support
    # ALTER TABLE ... ADD COLUMN IF NOT EXISTS so we catch the error
    try:
        conn.execute(
            "ALTER TABLE signals ADD COLUMN confluence_score INTEGER DEFAULT 0"
        )
        print("Migrated: added confluence_score to signals")
    except sqlite3.OperationalError:
        pass

    conn.commit()
    conn.close()
    print("DB initialized successfully")


init_db()


# ── MODELS ─────────────────────────────────────────────────────────────────────
class User(BaseModel):
    username:        str
    hashed_password: str

class UserIn(BaseModel):
    username: str
    password: str

class Token(BaseModel):
    access_token: str
    token_type:   str


# ── PASSWORD ───────────────────────────────────────────────────────────────────
def verify_password(plain: str, hashed: str) -> bool:
    return pwd_context.verify(plain, hashed)

def get_password_hash(password: str) -> str:
    return pwd_context.hash(password)


# ── JWT ────────────────────────────────────────────────────────────────────────
def create_access_token(
    data: dict, expires_delta: Optional[timedelta] = None
) -> str:
    to_encode = data.copy()
    expire    = datetime.utcnow() + (
        expires_delta or timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    )
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)


# ── DB HELPERS ─────────────────────────────────────────────────────────────────
def get_user_from_db(username: str) -> Optional[User]:
    conn = get_db()
    row  = conn.execute(
        "SELECT username, hashed_password FROM users WHERE username = ?",
        (username,),
    ).fetchone()
    conn.close()
    if row:
        return User(username=row["username"], hashed_password=row["hashed_password"])
    return None

def create_user_in_db(username: str, hashed_password: str):
    conn = get_db()
    conn.execute(
        "INSERT INTO users (username, hashed_password) VALUES (?, ?)",
        (username, hashed_password),
    )
    conn.commit()
    conn.close()

def user_exists_in_db(username: str) -> bool:
    conn = get_db()
    row  = conn.execute(
        "SELECT 1 FROM users WHERE username = ?", (username,)
    ).fetchone()
    conn.close()
    return row is not None


# ── AUTH DEPENDENCY ────────────────────────────────────────────────────────────
async def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(security),
) -> User:
    exc = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload  = jwt.decode(
            credentials.credentials, SECRET_KEY, algorithms=[ALGORITHM]
        )
        username = payload.get("sub")
        if not username:
            raise exc
    except JWTError as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Invalid token: {e}",
        )
    user = get_user_from_db(username)
    if not user:
        raise exc
    return user


# ── ROUTES ─────────────────────────────────────────────────────────────────────
@router.post("/register", response_model=Token)
async def register(user_in: UserIn):
    try:
        if user_exists_in_db(user_in.username):
            raise HTTPException(
                status_code=400, detail="Username already registered"
            )
        hashed = get_password_hash(user_in.password)
        create_user_in_db(user_in.username, hashed)
        token = create_access_token(data={"sub": user_in.username})
        return {"access_token": token, "token_type": "bearer"}
    except HTTPException:
        raise
    except Exception:
        import traceback
        raise HTTPException(
            status_code=500,
            detail=f"Register error: {traceback.format_exc()}",
        )


@router.post("/login", response_model=Token)
async def login(user_in: UserIn):
    try:
        user = get_user_from_db(user_in.username)
        if not user or not verify_password(user_in.password, user.hashed_password):
            raise HTTPException(
                status_code=401, detail="Incorrect username or password"
            )
        token = create_access_token(data={"sub": user.username})
        return {"access_token": token, "token_type": "bearer"}
    except HTTPException:
        raise
    except Exception:
        import traceback
        raise HTTPException(
            status_code=500,
            detail=f"Login error: {traceback.format_exc()}",
        )