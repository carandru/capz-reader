from __future__ import annotations

import hashlib
import secrets
import time
from typing import Any

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response

from .config import COOKIE_SECURE, SESSION_DAYS
from .db import db
from .schemas import ChangePasswordBody, LoginBody, SetupBody
from .state import write_lock
from .utils import utc_now

public_router = APIRouter(prefix="/api/auth", tags=["auth"])
ph = PasswordHasher()


def cleanup_sessions():
    with write_lock, db() as con:
        con.execute("DELETE FROM sessions WHERE expires_at < ?", (time.time(),))


def auth_state(request: Request) -> dict[str, Any] | None:
    token = request.cookies.get("nas_reader_session")
    if not token:
        return None
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    with db(readonly=True) as con:
        row = con.execute(
            "SELECT u.id,u.username,s.expires_at FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.token_hash=?",
            (token_hash,),
        ).fetchone()
    if not row or row["expires_at"] < time.time():
        return None
    return dict(row)


def require_user(request: Request):
    user = auth_state(request)
    if not user:
        raise HTTPException(401, "Login required")
    return user


def require_ajax(x_requested_with: str | None = Header(default=None)):
    if x_requested_with != "NASPDFReader":
        raise HTTPException(403, "Missing request guard")


def _login(body: LoginBody, response: Response):
    with db(readonly=True) as con:
        row = con.execute("SELECT * FROM users WHERE username=?", (body.username,)).fetchone()
    if not row:
        raise HTTPException(401, "Invalid username or password")
    try:
        ph.verify(row["password_hash"], body.password)
    except VerifyMismatchError:
        raise HTTPException(401, "Invalid username or password")
    token = secrets.token_urlsafe(48)
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    expires = time.time() + SESSION_DAYS * 86400
    with write_lock, db() as con:
        con.execute(
            "INSERT OR REPLACE INTO sessions(token_hash,user_id,expires_at) VALUES(?,?,?)",
            (token_hash, row["id"], expires),
        )
    response.set_cookie(
        "nas_reader_session",
        token,
        max_age=SESSION_DAYS * 86400,
        httponly=True,
        samesite="lax",
        secure=COOKIE_SECURE,
        path="/",
    )
    return {"ok": True, "username": row["username"]}


@public_router.get("/status")
def auth_status(request: Request):
    with db(readonly=True) as con:
        setup_required = con.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0
    user = auth_state(request)
    return {
        "setup_required": setup_required,
        "authenticated": bool(user),
        "username": user["username"] if user else None,
    }


@public_router.post("/setup", dependencies=[Depends(require_ajax)])
def setup(body: SetupBody, response: Response):
    with write_lock, db() as con:
        if con.execute("SELECT COUNT(*) FROM users").fetchone()[0]:
            raise HTTPException(409, "Setup already completed")
        con.execute(
            "INSERT INTO users(id,username,password_hash,created_at) VALUES(1,?,?,?)",
            (body.username, ph.hash(body.password), utc_now()),
        )
    return _login(LoginBody(username=body.username, password=body.password), response)


@public_router.post("/login", dependencies=[Depends(require_ajax)])
def login(body: LoginBody, response: Response):
    return _login(body, response)


# Define the protected router after require_user exists so the router itself is fail-closed.
protected_router = APIRouter(
    prefix="/api/auth",
    tags=["auth"],
    dependencies=[Depends(require_user)],
)


@protected_router.post("/change-password", dependencies=[Depends(require_ajax)])
def change_password(body: ChangePasswordBody, request: Request, user: dict[str, Any] = Depends(require_user)):
    with db(readonly=True) as con:
        row = con.execute("SELECT password_hash FROM users WHERE id=?", (user["id"],)).fetchone()
    if not row:
        raise HTTPException(404, "User not found")
    try:
        ph.verify(row["password_hash"], body.current_password)
    except VerifyMismatchError:
        raise HTTPException(401, "Current password is incorrect")
    current_token = request.cookies.get("nas_reader_session")
    current_hash = hashlib.sha256(current_token.encode()).hexdigest() if current_token else ""
    with write_lock, db() as con:
        con.execute("UPDATE users SET password_hash=? WHERE id=?", (ph.hash(body.new_password), user["id"]))
        con.execute("DELETE FROM sessions WHERE user_id=? AND token_hash<>?", (user["id"], current_hash))
    return {"ok": True}


@protected_router.post("/logout", dependencies=[Depends(require_ajax)])
def logout(request: Request, response: Response):
    token = request.cookies.get("nas_reader_session")
    if token:
        with write_lock, db() as con:
            con.execute(
                "DELETE FROM sessions WHERE token_hash=?",
                (hashlib.sha256(token.encode()).hexdigest(),),
            )
    response.delete_cookie("nas_reader_session", path="/")
    return {"ok": True}
