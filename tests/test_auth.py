import pytest

from erpdm.services import auth


def test_password_policy_rejects_weak():
    assert auth.validate_password_policy("short")
    assert not auth.validate_password_policy("Str0ngEnough!")


def test_create_and_authenticate(session):
    auth.create_user(session, "alice", "Str0ngEnough!", "modeller")
    u = auth.authenticate(session, "Alice", "Str0ngEnough!")
    assert u.username == "alice" and u.role == "modeller"
    assert u.can("generate") and not u.can("manage_metadata")


def test_bad_password_and_lockout(session):
    from erpdm.config import get_settings
    auth.create_user(session, "bob", "Str0ngEnough!", "viewer")
    for _ in range(get_settings().max_failed_logins):
        with pytest.raises(auth.AuthError):
            auth.authenticate(session, "bob", "wrong-password")
    with pytest.raises(auth.AuthError, match="locked"):
        auth.authenticate(session, "bob", "Str0ngEnough!")


def test_duplicate_and_invalid_usernames(session):
    auth.create_user(session, "carol", "Str0ngEnough!", "viewer")
    with pytest.raises(auth.AuthError):
        auth.create_user(session, "carol", "Str0ngEnough!", "viewer")
    with pytest.raises(auth.AuthError):
        auth.create_user(session, "x y", "Str0ngEnough!", "viewer")


def test_cannot_demote_last_admin(session):
    from sqlalchemy import select
    from erpdm.db.models import User
    for u in session.scalars(select(User).where(User.role == "admin")):
        u.role = "viewer"
    admin = auth.create_user(session, "root1", "Str0ngEnough!", "admin")
    with pytest.raises(auth.AuthError):
        auth.set_role(session, admin.id, "viewer", "test")
    with pytest.raises(auth.AuthError):
        auth.set_active(session, admin.id, False, "test")


def test_viewer_permissions():
    u = auth.AuthenticatedUser(1, "v", "viewer", None)
    assert u.can("view") and u.can("use_assistant")
    assert not u.can("generate") and not u.can("approve_mapping")
