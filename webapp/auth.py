from __future__ import annotations

from datetime import datetime, timezone
import os
import sqlite3

import bcrypt

from .config import WebAppConfig


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))


def ensure_bootstrap_users(conn: sqlite3.Connection, config: WebAppConfig) -> None:
    for user in config.bootstrap_users:
        password = os.environ.get(user.password_env_var, "").strip()
        if not password:
            continue
        existing = conn.execute(
            "SELECT id FROM users WHERE username = ?",
            (user.username,),
        ).fetchone()
        now = utc_now()
        if existing is None:
            conn.execute(
                """
                INSERT INTO users (username, password_hash, role, created_at, updated_at)
                VALUES (?, ?, 'admin', ?, ?)
                """,
                (user.username, hash_password(password), now, now),
            )
    conn.commit()


def authenticate_user(
    conn: sqlite3.Connection, username: str, password: str
) -> dict[str, str] | None:
    row = conn.execute(
        "SELECT username, password_hash, role FROM users WHERE username = ?",
        (username,),
    ).fetchone()
    if row is None:
        return None
    if not verify_password(password, row["password_hash"]):
        return None
    return {"username": row["username"], "role": row["role"]}


def change_password(
    conn: sqlite3.Connection,
    *,
    username: str,
    current_password: str,
    new_password: str,
) -> str | None:
    row = conn.execute(
        "SELECT password_hash FROM users WHERE username = ?",
        (username,),
    ).fetchone()
    if row is None:
        return "User not found."
    if not verify_password(current_password, row["password_hash"]):
        return "Current password is incorrect."
    if len(new_password) < 12:
        return "New password must be at least 12 characters long."
    now = utc_now()
    conn.execute(
        "UPDATE users SET password_hash = ?, updated_at = ? WHERE username = ?",
        (hash_password(new_password), now, username),
    )
    conn.commit()
    return None
