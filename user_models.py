"""
User and session management — now uses database.py for persistence.
PostgreSQL on Render, SQLite fallback for local dev.
"""
from fastapi import APIRouter, HTTPException, Depends, Request, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel
from jose import JWTError, jwt
from datetime import datetime, timedelta
from typing import Optional
import logging
import os

from pwdlib import PasswordHash
from pwdlib.hashers.bcrypt import BcryptHasher
from database import get_user, user_exists, create_user, is_admin as db_is_admin
import database as db
from rate_limit import login_limiter, register_limiter, forgot_password_limiter, reset_password_limiter

logger = logging.getLogger("Auth")
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

class ForgotPasswordRequest(BaseModel):
    username: str

class ResetPasswordRequest(BaseModel):
    username:     str
    code:         str
    new_password: str


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
async def register(user_in: UserIn, request: Request):
    register_limiter.check(request.client.host if request.client else "unknown")
    if len(user_in.password) < 8:
        raise HTTPException(status_code=400, detail="Password must be at least 8 characters.")
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
        logger.exception(f"Register error for {user_in.username}")
        raise HTTPException(status_code=500, detail="Registration failed. Please try again.")


@router.post("/login", response_model=Token)
async def login(user_in: UserIn):
    login_limiter.check(user_in.username.lower())
    try:
        user = get_user_from_db(user_in.username)
        if not user or not verify_password(user_in.password, user.hashed_password):
            raise HTTPException(status_code=401, detail="Incorrect username or password")
        login_limiter.reset(user_in.username.lower())
        token    = create_access_token(data={"sub": user.username})
        is_admin = db_is_admin(user.username)
        return {"access_token": token, "token_type": "bearer", "is_admin": is_admin}
    except HTTPException:
        raise
    except Exception:
        logger.exception(f"Login error for {user_in.username}")
        raise HTTPException(status_code=500, detail="Login failed. Please try again.")


@router.post("/auth/forgot-password")
async def forgot_password(req: ForgotPasswordRequest):
    """
    Always returns the same generic response regardless of whether the
    account exists — a distinguishable response here would let anyone probe
    which emails are registered (user enumeration).
    """
    import secrets
    import hashlib
    from datetime import datetime, timedelta

    forgot_password_limiter.check(req.username.lower())

    if user_exists_in_db(req.username):
        code = f"{secrets.randbelow(1_000_000):06d}"
        code_hash = hashlib.sha256(code.encode()).hexdigest()
        expires_at = datetime.utcnow() + timedelta(minutes=15)
        db.create_password_reset(req.username, code_hash, expires_at)
        try:
            from email_service import send_password_reset_email
            send_password_reset_email(req.username, code, expiry_minutes=15)
        except Exception:
            # The code is already stored — a delivery failure shouldn't
            # surface as an enumeration signal, and the user can retry.
            logger.exception(f"Password reset email failed for {req.username}")

    return {"ok": True}


@router.post("/auth/reset-password")
async def reset_password(req: ResetPasswordRequest):
    import hashlib
    from datetime import datetime

    reset_password_limiter.check(req.username.lower())

    if len(req.new_password) < 8:
        raise HTTPException(status_code=400, detail="Password must be at least 8 characters.")

    code_hash = hashlib.sha256(req.code.encode()).hexdigest()
    row = db.get_valid_password_reset(req.username, code_hash, datetime.utcnow())
    if not row:
        raise HTTPException(status_code=400, detail="Invalid or expired code.")

    db.set_password(req.username, get_password_hash(req.new_password))
    db.mark_password_reset_used(row["id"])
    reset_password_limiter.reset(req.username.lower())
    return {"ok": True}


def _get_firebase_app():
    """Lazily initialise Firebase Admin SDK (once per process)."""
    import firebase_admin
    from firebase_admin import credentials as fb_creds
    if not firebase_admin._apps:
        sa_json = os.getenv("FIREBASE_SERVICE_ACCOUNT_JSON", "")
        if sa_json:
            import json as _json
            cred = fb_creds.Certificate(_json.loads(sa_json))
        else:
            cred = fb_creds.ApplicationDefault()
        firebase_admin.initialize_app(cred)
    return firebase_admin.get_app()


@router.post("/auth/firebase", response_model=Token)
async def auth_firebase(req: FirebaseAuthRequest):
    """
    Verify a Firebase ID token and return a backend JWT.
    Creates the backend user on first login — no password sync needed.
    """
    import secrets
    from firebase_admin import auth as fb_auth
    try:
        _get_firebase_app()
        decoded = fb_auth.verify_id_token(req.firebase_token)
        email = decoded.get("email", "")
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
        logger.exception("Firebase auth error")
        raise HTTPException(status_code=500, detail="Sign-in failed. Please try again.")