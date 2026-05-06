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
from database import get_user, user_exists, create_user, is_admin as db_is_admin

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

class FirebaseAuthRequest(BaseModel):
    firebase_token: str
    is_admin:     bool = False


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
        return {"access_token": token, "token_type": "bearer", "is_admin": False}
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
        token    = create_access_token(data={"sub": user.username})
        is_admin = db_is_admin(user.username)
        return {"access_token": token, "token_type": "bearer", "is_admin": is_admin}
    except HTTPException:
        raise
    except Exception:
        import traceback
        raise HTTPException(
            status_code=500,
            detail=f"Login error: {traceback.format_exc()}",
        )


FIREBASE_API_KEY = os.getenv(
    "FIREBASE_API_KEY",
    "AIzaSyCO9rb8XgAUAR_9b5q40xlXe_q1gTcnw_E",   # public key, same as in APK
)

@router.post("/auth/firebase", response_model=Token)
async def auth_firebase(req: FirebaseAuthRequest):
    """
    Verify a Firebase ID token and return a backend JWT.
    Creates the backend user on first login — no password sync needed.
    """
    import secrets
    import requests as http_req
    try:
        resp = http_req.post(
            f"https://identitytoolkit.googleapis.com/v1/accounts:lookup"
            f"?key={FIREBASE_API_KEY}",
            json={"idToken": req.firebase_token},
            timeout=10,
        )
        data = resp.json()
        if "error" in data:
            raise HTTPException(status_code=401, detail="Invalid Firebase token")
        users = data.get("users", [])
        if not users:
            raise HTTPException(status_code=401, detail="Firebase user not found")
        email = users[0].get("email", "")
        if not email:
            raise HTTPException(status_code=400, detail="No email on Firebase account")

        # Create backend user on first login (random password — never used for login)
        if not user_exists_in_db(email):
            create_user_in_db(email, get_password_hash(secrets.token_hex(32)))

        token    = create_access_token(data={"sub": email})
        is_admin = db_is_admin(email)
        return {"access_token": token, "token_type": "bearer", "is_admin": is_admin}
    except HTTPException:
        raise
    except Exception:
        import traceback
        raise HTTPException(status_code=500, detail=f"Firebase auth error: {traceback.format_exc()}")