"""Authentication (bcrypt, lockout, password policy) and role-based access control."""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import bcrypt
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from erpdm.config import get_settings
from erpdm.constants import ROLE_PERMISSIONS, Role
from erpdm.db.models import User
from erpdm.services.audit import audit


class AuthError(Exception):
    pass


@dataclass(frozen=True)
class AuthenticatedUser:
    id: int
    username: str
    role: str
    full_name: str | None
    must_change_password: bool = False

    def can(self, permission: str) -> bool:
        return permission in ROLE_PERMISSIONS.get(self.role, set())


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt(rounds=12)).decode("utf-8")


def verify_password(password: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode("utf-8"), hashed.encode("utf-8"))
    except ValueError:
        return False


def validate_password_policy(password: str) -> list[str]:
    s = get_settings()
    problems = []
    if len(password) < s.min_password_length:
        problems.append(f"at least {s.min_password_length} characters")
    if not re.search(r"[A-Z]", password):
        problems.append("an upper-case letter")
    if not re.search(r"[a-z]", password):
        problems.append("a lower-case letter")
    if not re.search(r"\d", password):
        problems.append("a digit")
    if len(password.encode("utf-8")) > 72:
        problems.append("at most 72 bytes (bcrypt limit)")
    return problems


def _utc(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def create_user(session: Session, username: str, password: str, role: str = Role.MODELLER.value,
                email: str | None = None, full_name: str | None = None, actor: str | None = None,
                must_change_password: bool = False) -> User:
    username = username.strip().lower()
    if not re.match(r"^[a-z0-9_.\-]{3,80}$", username):
        raise AuthError("Username must be 3-80 chars: a-z, 0-9, '.', '_' or '-'.")
    if role not in ROLE_PERMISSIONS:
        raise AuthError(f"Unknown role {role!r}")
    problems = validate_password_policy(password)
    if problems:
        raise AuthError("Password needs " + ", ".join(problems) + ".")
    if session.scalar(select(User).where(User.username == username)):
        raise AuthError("Username already exists.")
    user = User(username=username, password_hash=hash_password(password), role=role, email=email,
                full_name=full_name, must_change_password=must_change_password)
    session.add(user)
    session.flush()
    audit(session, actor or username, "user.create", "user", user.id, {"role": role})
    return user


def authenticate(session: Session, username: str, password: str) -> AuthenticatedUser:
    s = get_settings()
    username = (username or "").strip().lower()
    user = session.scalar(select(User).where(User.username == username))
    now = datetime.now(timezone.utc)
    if user is None or not user.is_active:
        # constant-ish time: still run a hash comparison
        verify_password(password or "", "$2b$12$" + "a" * 53)
        audit(session, username, "auth.failed", "user", None, {"reason": "unknown_or_inactive"})
        raise AuthError("Invalid username or password.")
    locked = _utc(user.locked_until)
    if locked and locked > now:
        audit(session, username, "auth.locked", "user", user.id)
        raise AuthError("Account temporarily locked after repeated failures. Try again later.")
    if not verify_password(password or "", user.password_hash):
        user.failed_attempts += 1
        if user.failed_attempts >= s.max_failed_logins:
            user.locked_until = now + timedelta(minutes=s.lockout_minutes)
            user.failed_attempts = 0
        audit(session, username, "auth.failed", "user", user.id, {"reason": "bad_password"})
        raise AuthError("Invalid username or password.")
    user.failed_attempts = 0
    user.locked_until = None
    user.last_login = now
    audit(session, username, "auth.login", "user", user.id)
    return AuthenticatedUser(user.id, user.username, user.role, user.full_name, user.must_change_password)


def change_password(session: Session, user_id: int, old: str, new: str) -> None:
    user = session.get(User, user_id)
    if user is None or not verify_password(old, user.password_hash):
        raise AuthError("Current password is incorrect.")
    problems = validate_password_policy(new)
    if problems:
        raise AuthError("Password needs " + ", ".join(problems) + ".")
    user.password_hash = hash_password(new)
    user.must_change_password = False
    audit(session, user.username, "user.password_change", "user", user.id)


def set_role(session: Session, user_id: int, role: str, actor: str) -> None:
    if role not in ROLE_PERMISSIONS:
        raise AuthError(f"Unknown role {role!r}")
    user = session.get(User, user_id)
    if user is None:
        raise AuthError("User not found")
    if user.role == Role.ADMIN.value and role != Role.ADMIN.value:
        admins = session.scalar(select(func.count()).select_from(User).where(
            User.role == Role.ADMIN.value, User.is_active.is_(True)))
        if admins <= 1:
            raise AuthError("Cannot demote the last active admin.")
    user.role = role
    audit(session, actor, "user.role_change", "user", user_id, {"role": role})


def set_active(session: Session, user_id: int, active: bool, actor: str) -> None:
    user = session.get(User, user_id)
    if user is None:
        raise AuthError("User not found")
    if not active and user.role == Role.ADMIN.value:
        admins = session.scalar(select(func.count()).select_from(User).where(
            User.role == Role.ADMIN.value, User.is_active.is_(True)))
        if admins <= 1:
            raise AuthError("Cannot deactivate the last active admin.")
    user.is_active = active
    audit(session, actor, "user.activate" if active else "user.deactivate", "user", user_id)


def reset_password(session: Session, user_id: int, new_password: str, actor: str) -> None:
    user = session.get(User, user_id)
    if user is None:
        raise AuthError("User not found")
    problems = validate_password_policy(new_password)
    if problems:
        raise AuthError("Password needs " + ", ".join(problems) + ".")
    user.password_hash = hash_password(new_password)
    user.must_change_password = True
    user.failed_attempts = 0
    user.locked_until = None
    audit(session, actor, "user.password_reset", "user", user_id)


def ensure_bootstrap_admin(session: Session) -> str | None:
    """Create the first admin from ADMIN_USERNAME / ADMIN_PASSWORD env vars if no admin exists."""
    s = get_settings()
    exists = session.scalar(select(func.count()).select_from(User).where(User.role == Role.ADMIN.value))
    if exists:
        return None
    if not (s.admin_username and s.admin_password):
        return "No admin user exists. Set ADMIN_USERNAME and ADMIN_PASSWORD and restart."
    create_user(session, s.admin_username, s.admin_password.get_secret_value(), Role.ADMIN.value,
                email=s.admin_email, full_name="Administrator", actor="system",
                must_change_password=s.app_env == "production")
    return None
