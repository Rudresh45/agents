from __future__ import annotations

from fastapi.testclient import TestClient

INTERNAL_TOKEN = "test-internal-service-token"


def test_internal_ping_requires_service_token(client: TestClient) -> None:
    response = client.get("/internal/ping")
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"


def test_internal_ping_rejects_user_jwt(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    response = client.get("/internal/ping", headers=auth_headers)
    assert response.status_code == 401


def test_internal_ping_rejects_wrong_service_token(client: TestClient) -> None:
    response = client.get(
        "/internal/ping",
        headers={"Authorization": "Bearer not-the-service-token"},
    )
    assert response.status_code == 401


def test_internal_ping_accepts_service_token(client: TestClient) -> None:
    response = client.get(
        "/internal/ping",
        headers={"Authorization": f"Bearer {INTERNAL_TOKEN}"},
    )
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "service": "backend"}


def test_internal_routes_are_not_in_public_openapi(client: TestClient) -> None:
    spec = client.get("/openapi.json").json()
    assert "/internal/ping" not in spec["paths"]
    assert client.get("/api/v1/internal/ping").status_code == 404
