"""
User and session management for FastAPI backend.
"""
from fastapi import APIRouter, HTTPException, Depends, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel
from passlib.context import CryptContext
from jose import JWTError, jwt
from datetime import datetime, timedelta
from typing import Optional
import os
import sqlite3

SECRET_KEY = os.getenv("SECRET_KEY", "REPLACE_WITH_A_SECURE_RANDOM_KEY")
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 60 * 24

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
security = HTTPBearer()
router = APIRouter()

# --- SQLite with multiple fallback paths ---
def get_db_path():
    for path in ["/tmp/users.db", "/var/tmp/users.db", os.path.expanduser("~/users.db")]:
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

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            username TEXT PRIMARY KEY,
            hashed_password TEXT NOT NULL
        )
    """)
    conn.commit()
    conn.close()
    print("DB initialized successfully")

init_db()


# --- Models ---
class User(BaseModel):
    username: str
    hashed_password: str

class UserIn(BaseModel):
    username: str
    password: str

class Token(BaseModel):
    access_token: str
    token_type: str


# --- Helpers ---
def verify_password(plain_password, hashed_password):
    return pwd_context.verify(plain_password, hashed_password)

def get_password_hash(password):
    return pwd_context.hash(password)

def create_access_token(data: dict, expires_delta: Optional[timedelta] = None):
    to_encode = data.copy()
    expire = datetime.utcnow() + (expires_delta or timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES))
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)

def get_user_from_db(username: str) -> Optional[User]:
    conn = get_db()
    row = conn.execute(
        "SELECT username, hashed_password FROM users WHERE username = ?", (username,)
    ).fetchone()
    conn.close()
    if row:
        return User(username=row["username"], hashed_password=row["hashed_password"])
    return None

def create_user_in_db(username: str, hashed_password: str):
    conn = get_db()
    conn.execute(
        "INSERT INTO users (username, hashed_password) VALUES (?, ?)",
        (username, hashed_password)
    )
    conn.commit()
    conn.close()

def user_exists_in_db(username: str) -> bool:
    conn = get_db()
    row = conn.execute(
        "SELECT 1 FROM users WHERE username = ?", (username,)
    ).fetchone()
    conn.close()
    return row is not None


# --- Auth dependency ---
async def get_current_user(credentials: HTTPAuthorizationCredentials = Depends(security)):
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    token = credentials.credentials
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        username: str = payload.get("sub")
        if username is None:
            raise credentials_exception
    except JWTError as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Invalid authentication token: {e}",
        )
    user = get_user_from_db(username)
    if user is None:
        raise credentials_exception
    return user


# --- Routes ---
@router.post("/register", response_model=Token)
async def register(user_in: UserIn):
    try:
        if user_exists_in_db(user_in.username):
            raise HTTPException(status_code=400, detail="Username already registered")
        hashed_password = get_password_hash(user_in.password)
        create_user_in_db(user_in.username, hashed_password)
        access_token = create_access_token(data={"sub": user_in.username})
        return {"access_token": access_token, "token_type": "bearer"}
    except HTTPException:
        raise
    except Exception as e:
        import traceback
        raise HTTPException(status_code=500, detail=f"Register error: {traceback.format_exc()}")


@router.post("/login", response_model=Token)
async def login(user_in: UserIn):
    try:
        user = get_user_from_db(user_in.username)
        if not user or not verify_password(user_in.password, user.hashed_password):
            raise HTTPException(status_code=401, detail="Incorrect username or password")
        access_token = create_access_token(data={"sub": user.username})
        return {"access_token": access_token, "token_type": "bearer"}
    except HTTPException:
        raise
    except Exception as e:
        import traceback
        raise HTTPException(status_code=500, detail=f"Login error: {traceback.format_exc()}")