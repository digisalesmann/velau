"""
User and session management — now uses database.py for persistence.
PostgreSQL on Render, SQLite fallback for local dev.
"""
from fastapi import APIRouter, HTTPException, Depends, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel
from jose import JWTError, jwt
from datetime import datetime, timedelta
from typing import Optional
import os

from pwdlib import PasswordHash
from pwdlib.hashers.bcrypt import BcryptHasher
from database import get_user, user_exists, create_user

pwd_context = PasswordHash([BcryptHasher()])
SECRET_KEY  = os.getenv("SECRET_KEY", "REPLACE_WITH_A_SECURE_RANDOM_KEY")
ALGORITHM   = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 60 * 24 * 7  # 7 days

security = HTTPBearer()
router   = APIRouter()

# Keep DB_PATH for legacy references in main.py
DB_PATH = os.getenv("DATABASE_URL", "/tmp/users.db")


# ── Models ─────────────────────────────────────────────────────────────────────
class User(BaseModel):
    username:        str
    hashed_password: str

class UserIn(BaseModel):
    username: str
    password: str

class Token(BaseModel):
    access_token: str
    token_type:   str


# ── Password ───────────────────────────────────────────────────────────────────
def verify_password(plain: str, hashed: str) -> bool:
    return pwd_context.verify(plain, hashed)

def get_password_hash(password: str) -> str:
    return pwd_context.hash(password)


# ── JWT ────────────────────────────────────────────────────────────────────────
def create_access_token(data: dict, expires_delta: Optional[timedelta] = None) -> str:
    to_encode = data.copy()
    expire    = datetime.utcnow() + (
        expires_delta or timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    )
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)


# ── DB wrappers ────────────────────────────────────────────────────────────────
def get_user_from_db(username: str) -> Optional[User]:
    row = get_user(username)
    if row:
        return User(username=row["username"], hashed_password=row["hashed_password"])
    return None

def create_user_in_db(username: str, hashed_password: str):
    create_user(username, hashed_password)

def user_exists_in_db(username: str) -> bool:
    return user_exists(username)


# ── Auth dependency ────────────────────────────────────────────────────────────
async def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(security),
) -> User:
    exc = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload  = jwt.decode(credentials.credentials, SECRET_KEY, algorithms=[ALGORITHM])
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


# ── Routes ─────────────────────────────────────────────────────────────────────
@router.post("/register", response_model=Token)
async def register(user_in: UserIn):
    try:
        if user_exists_in_db(user_in.username):
            raise HTTPException(status_code=400, detail="Username already registered")
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
            raise HTTPException(status_code=401, detail="Incorrect username or password")
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