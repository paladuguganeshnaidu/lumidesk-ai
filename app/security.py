from __future__ import annotations
import secrets
from fastapi import HTTPException, Request


def csrf_token(request: Request) -> str:
    token = request.session.get("csrf")
    if not token:
        token = secrets.token_urlsafe(32)
        request.session["csrf"] = token
    return token


def require_csrf(request: Request) -> None:
    expected = request.session.get("csrf")
    supplied = request.headers.get("x-csrf-token", "")
    if not expected or not supplied or not secrets.compare_digest(expected, supplied):
        raise HTTPException(status_code=403, detail="Your session security token expired. Refresh and try again.")


def current_user(request: Request) -> dict:
    """Reject revoked, deleted and pre-reset login cookies, even if IDs get reused."""
    from .db import SessionEpoch, SessionLocal, User

    user = request.session.get("user")
    if not user:
        raise HTTPException(status_code=401, detail="Please sign in with Google to continue.")
    with SessionLocal() as db:
        epoch = db.get(SessionEpoch, 1)
        account = db.get(User, user.get("id")) if isinstance(user.get("id"), int) else None
        if (epoch is None or not request.session.get("session_epoch")
                or not secrets.compare_digest(str(request.session["session_epoch"]), epoch.value)
                or account is None or account.email != user.get("email")):
            request.session.clear()
            raise HTTPException(status_code=401, detail="Your session has expired. Sign in again.")
        if not account.active:
            raise HTTPException(status_code=403, detail="This account is currently disabled.")
        # Check the database, not a role supplied by a browser or an old cookie.
        return {"id": account.id, "email": account.email, "name": account.name,
                "picture": account.picture, "role": account.role, "active": account.active}


def require_admin(request: Request) -> dict:
    user = current_user(request)
    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Administrator access is required.")
    return user
