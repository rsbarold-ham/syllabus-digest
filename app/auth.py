import hashlib
import hmac
import secrets
from datetime import datetime, timedelta

from fastapi import Request, HTTPException

from .db import get_connection

PBKDF2_ITERATIONS = 200_000
RESET_TOKEN_LIFETIME = timedelta(hours=1)


def hash_password(password: str, salt: str | None = None) -> tuple[str, str]:
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt.encode("utf-8"), PBKDF2_ITERATIONS
    )
    return digest.hex(), salt


def verify_password(password: str, stored_hash: str, salt: str) -> bool:
    candidate, _ = hash_password(password, salt)
    return hmac.compare_digest(candidate, stored_hash)


def create_user(email: str, password: str) -> int:
    password_hash, salt = hash_password(password)
    magic_token = secrets.token_urlsafe(24)
    conn = get_connection()
    try:
        cur = conn.execute(
            "INSERT INTO users (email, password_hash, password_salt, magic_token) VALUES (?, ?, ?, ?)",
            (email.lower().strip(), password_hash, salt, magic_token),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def regenerate_magic_token(user_id: int) -> str:
    """Invalidates the old email-footer link (e.g. if it leaked) by issuing a
    fresh token. Existing sessions are unaffected -- this only changes what
    the *next* digest email links to."""
    token = secrets.token_urlsafe(24)
    conn = get_connection()
    try:
        conn.execute("UPDATE users SET magic_token = ? WHERE id = ?", (token, user_id))
        conn.commit()
    finally:
        conn.close()
    return token


def user_by_magic_token(token: str):
    if not token:
        return None
    conn = get_connection()
    try:
        return conn.execute("SELECT * FROM users WHERE magic_token = ?", (token,)).fetchone()
    finally:
        conn.close()


def set_password(user_id: int, new_password: str):
    password_hash, salt = hash_password(new_password)
    conn = get_connection()
    try:
        conn.execute(
            "UPDATE users SET password_hash = ?, password_salt = ? WHERE id = ?",
            (password_hash, salt, user_id),
        )
        conn.commit()
    finally:
        conn.close()


def create_reset_token(user_id: int) -> str:
    """Single-use, 1-hour-lived token for the forgot-password flow. Storing
    it (rather than e.g. a signed JWT) means it can be invalidated the
    moment it's used or superseded by a newer request."""
    token = secrets.token_urlsafe(32)
    expires = (datetime.utcnow() + RESET_TOKEN_LIFETIME).isoformat()
    conn = get_connection()
    try:
        conn.execute(
            "UPDATE users SET reset_token = ?, reset_token_expires = ? WHERE id = ?",
            (token, expires, user_id),
        )
        conn.commit()
    finally:
        conn.close()
    return token


def user_by_reset_token(token: str):
    """Returns the user row if the token exists and hasn't expired, else None."""
    if not token:
        return None
    conn = get_connection()
    try:
        row = conn.execute("SELECT * FROM users WHERE reset_token = ?", (token,)).fetchone()
    finally:
        conn.close()
    if not row or not row["reset_token_expires"]:
        return None
    if datetime.utcnow() > datetime.fromisoformat(row["reset_token_expires"]):
        return None
    return row


def clear_reset_token(user_id: int):
    conn = get_connection()
    try:
        conn.execute(
            "UPDATE users SET reset_token = NULL, reset_token_expires = NULL WHERE id = ?",
            (user_id,),
        )
        conn.commit()
    finally:
        conn.close()


def authenticate(email: str, password: str):
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT * FROM users WHERE email = ?", (email.lower().strip(),)
        ).fetchone()
    finally:
        conn.close()
    if row and verify_password(password, row["password_hash"], row["password_salt"]):
        return row
    return None


def get_current_user(request: Request):
    user_id = request.session.get("user_id")
    if not user_id:
        raise HTTPException(status_code=303, headers={"Location": "/login"})
    conn = get_connection()
    try:
        row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    finally:
        conn.close()
    if not row:
        raise HTTPException(status_code=303, headers={"Location": "/login"})
    return row
