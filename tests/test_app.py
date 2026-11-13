import os
os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("DATABASE_URL", "sqlite:////tmp/lumidesk-pytest.db")
os.environ.setdefault("SECRET_KEY", "test-secret-key-that-is-not-used-in-production")

from fastapi.testclient import TestClient
from app.main import app


def test_health_and_pages():
    with TestClient(app) as client:
        assert client.get("/health").status_code == 200
        assert "LumiDesk AI" in client.get("/").text
        assert "Visual learning assistant" in client.get("/app").text
        assert "Admin console" in client.get("/admin").text


def test_google_login_requires_configuration():
    with TestClient(app) as client:
        response = client.get("/auth/google")
        assert response.status_code == 503


def test_private_api_rejects_anonymous_user():
    with TestClient(app) as client:
        csrf = client.get("/api/me").json()["csrf"]
        response = client.post("/api/tutor", json={"message": "Explain neural networks"}, headers={"X-CSRF-Token": csrf})
        assert response.status_code == 401
