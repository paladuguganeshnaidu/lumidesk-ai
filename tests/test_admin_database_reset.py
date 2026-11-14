"""Integration checks for the irreversible admin database reset.

Runs only against a temporary SQLite database; never touches live data.
"""
import base64
import json
import os

import pytest
from fastapi.testclient import TestClient
from itsdangerous import TimestampSigner
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from app import db as database
from app.main import app


@pytest.fixture()
def reset_env(tmp_path, monkeypatch):
    engine = create_engine(
        "sqlite:///" + str(tmp_path / "reset-integration.db"),
        connect_args={"check_same_thread": False},
    )
    database.Base.metadata.create_all(bind=engine)
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    with factory() as session:
        epoch = database.current_session_epoch(session)
        session.add_all([
            database.User(id=1, google_sub="admin-google", email="admin@example.com",
                          name="Administrator", role="admin"),
            database.User(id=2, google_sub="student-google", email="student@example.com",
                          name="Student", role="student"),
        ])
        session.commit()
        session.add_all([
            database.UsageEvent(user_id=2, kind="tutor", detail="test"),
            database.ReportRecord(user_id=2, title="Test report", pages=2),
            database.PaymentOrder(
                user_id=2, stripe_session_id="cs_test_123",
                price_id="price_test", mode="payment", status="paid",
                currency="usd", amount_minor=100,
            ),
            database.StripeEvent(id="evt_test_123", kind="checkout.session.completed"),
        ])
        session.commit()
    monkeypatch.setattr(database, "SessionLocal", factory)
    monkeypatch.setenv("ADMIN_EMAILS", "admin@example.com")

    def get_test_db():
        with factory() as session:
            yield session
    app.dependency_overrides[database.get_db] = get_test_db
    try:
        with TestClient(app) as client:
            # Starlette's signed session cookie; no OAuth/network calls required.
            def login(user_id, email, role):
                session_data = {
                    "user": {"id": user_id, "email": email, "name": role,
                             "role": role, "active": True},
                    "session_epoch": epoch,
                    "csrf": "test-csrf",
                }
                encoded = base64.b64encode(json.dumps(session_data).encode("utf-8"))
                value = TimestampSigner(os.environ.get("SECRET_KEY", "dev-only-change-this-secret-before-deploy")).sign(encoded).decode("utf-8")
                client.cookies.set("lumidesk_session", value)
                return value
            yield client, factory, epoch, login
    finally:
        app.dependency_overrides.clear()
        engine.dispose()


def test_database_reset_requires_live_admin_role_csrf_and_phrase(reset_env):
    client, factory, epoch, login = reset_env
    endpoint = "/api/admin/database/reset"
    valid = {"confirmation": "DELETE ALL LUMIDESK DATA", "acknowledge_billing": True}
    assert client.post(endpoint, json=valid).status_code == 401

    login(2, "student@example.com", "student")
    assert client.post(endpoint, json=valid, headers={"X-CSRF-Token": "test-csrf"}).status_code == 403

    login(1, "admin@example.com", "admin")
    assert client.post(endpoint, json=valid).status_code == 403
    assert client.post(endpoint, json={"confirmation": "DELETE ALL LUMIDESK DATA", "acknowledge_billing": False},
                       headers={"X-CSRF-Token": "test-csrf"}).status_code == 400
    assert client.post(endpoint, json={"confirmation": "DELETE ALL", "acknowledge_billing": True},
                       headers={"X-CSRF-Token": "test-csrf"}).status_code == 400
    with factory() as db:
        assert db.scalar(select(func.count(database.User.id))) == 2


def test_full_reset_clears_all_records_and_revokes_old_cookies(reset_env):
    client, factory, epoch, login = reset_env
    old_cookie = login(1, "admin@example.com", "admin")
    response = client.post("/api/admin/database/reset",
                           json={"confirmation": "DELETE ALL LUMIDESK DATA", "acknowledge_billing": True},
                           headers={"X-CSRF-Token": "test-csrf"})
    assert response.status_code == 200, response.text
    assert response.json()["ok"] is True
    with factory() as db:
        for model in (database.User, database.ReportRecord, database.UsageEvent,
                      database.PaymentOrder, database.StripeEvent):
            assert db.scalar(select(func.count()).select_from(model)) == 0
        assert database.current_session_epoch(db) != epoch
        # An old signed cookie must stay invalid even if a new account reuses ID 1.
        db.add(database.User(id=1, google_sub="new-admin", email="admin@example.com",
                             name="New admin", role="admin"))
        db.commit()
    client.cookies.set("lumidesk_session", old_cookie)
    assert client.get("/api/admin/overview").status_code == 401
