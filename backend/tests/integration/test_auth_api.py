from __future__ import annotations

from fastapi.testclient import TestClient


def test_health_is_public(client: TestClient):
    response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["checks"]["database"]["status"] == "ok"
    assert set(body["checks"]) == {"database", "imap", "smtp", "llm"}


def test_login_success(client: TestClient, seeded):
    response = client.post(
        "/api/v1/auth/login",
        json={"email": "HR@ACME-HR.COM", "password": "hr-password-123"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["token_type"] == "bearer"
    assert body["user"]["role"] == "hr"
    assert body["expires_in"] > 0


def test_login_is_case_insensitive_on_email(client: TestClient, seeded):
    response = client.post(
        "/api/v1/auth/login",
        json={"email": "HR@ACME-HR.COM", "password": "hr-password-123"},
    )
    assert response.status_code == 200


def test_login_wrong_password(client: TestClient, seeded):
    response = client.post(
        "/api/v1/auth/login",
        json={"email": "HR@ACME-HR.COM", "password": "nope"},
    )
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"


def test_login_unknown_user(client: TestClient, seeded):
    response = client.post(
        "/api/v1/auth/login",
        json={"email": "ghost@acme-hr.com", "password": "whatever"},
    )
    assert response.status_code == 401


def test_login_validation_error_envelope(client: TestClient, seeded):
    response = client.post("/api/v1/auth/login", json={"email": "not-an-email"})
    assert response.status_code == 422
    body = response.json()
    assert body["error"]["code"] == "validation_error"
    assert isinstance(body["error"]["details"], list)


def test_me_requires_token(client: TestClient):
    response = client.get("/api/v1/auth/me")
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"


def test_me_with_token(client: TestClient, auth_headers):
    response = client.get("/api/v1/auth/me", headers=auth_headers)
    assert response.status_code == 200
    assert response.json()["email"] == "hr@acme-hr.com"


def test_me_rejects_garbage_token(client: TestClient):
    response = client.get(
        "/api/v1/auth/me", headers={"Authorization": "Bearer not-a-real-token"}
    )
    assert response.status_code == 401


def test_request_id_is_echoed(client: TestClient):
    response = client.get("/health", headers={"X-Request-ID": "abc-123"})
    assert response.headers["X-Request-ID"] == "abc-123"


def test_openapi_documents_auth(client: TestClient):
    spec = client.get("/openapi.json").json()
    assert "/api/v1/auth/login" in spec["paths"]
    assert "/health" in spec["paths"]
