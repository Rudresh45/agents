from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import jwt
import pytest
from app.core.config import get_settings
from app.core.exceptions import AuthError
from app.core.security import (
    create_access_token,
    decode_token,
    hash_password,
    verify_internal_token,
    verify_password,
)


def test_password_hash_roundtrip():
    stored = hash_password("correct horse battery staple")
    assert stored.startswith("scrypt$")
    assert "correct horse battery staple" not in stored
    assert verify_password("correct horse battery staple", stored)


def test_password_hash_is_salted():
    first = hash_password("same-password")
    second = hash_password("same-password")
    assert first != second
    assert verify_password("same-password", first)
    assert verify_password("same-password", second)


@pytest.mark.parametrize("wrong", ["", "wrong", "same-password ", "SAME-PASSWORD"])
def test_password_verify_rejects_wrong(wrong: str):
    stored = hash_password("same-password")
    assert not verify_password(wrong, stored)


@pytest.mark.parametrize("garbage", ["", "not-a-hash", "scrypt$1$2", "argon2$x$y$z$q$r"])
def test_password_verify_rejects_malformed(garbage: str):
    assert not verify_password("anything", garbage)


def test_jwt_roundtrip():
    token, expires_in = create_access_token(subject="user-1", role="hr")
    payload = decode_token(token)
    assert payload["sub"] == "user-1"
    assert payload["role"] == "hr"
    assert expires_in == get_settings().jwt_expire_minutes * 60


def test_jwt_rejects_tampering():
    token, _ = create_access_token(subject="user-1", role="hr")
    head, body, signature = token.split(".")
    tampered = f"{head}.{body}.{'a' * len(signature)}"
    with pytest.raises(AuthError):
        decode_token(tampered)


def test_jwt_rejects_expired():
    settings = get_settings()
    now = datetime.now(UTC)
    payload = {
        "sub": "user-1",
        "role": "hr",
        "iat": int((now - timedelta(hours=2)).timestamp()),
        "exp": int((now - timedelta(hours=1)).timestamp()),
    }
    token = jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)
    with pytest.raises(AuthError):
        decode_token(token)


def test_jwt_rejects_missing_claims():
    settings = get_settings()
    token = jwt.encode({"sub": "user-1"}, settings.jwt_secret, algorithm=settings.jwt_algorithm)
    with pytest.raises(AuthError):
        decode_token(token)


def test_internal_token_roundtrip():
    assert verify_internal_token("test-internal-service-token")
    assert not verify_internal_token("wrong")
    assert not verify_internal_token("")


def test_internal_token_fails_closed_when_unconfigured(monkeypatch):
    monkeypatch.setattr(
        "app.core.security.get_settings",
        lambda: SimpleNamespace(internal_service_token=""),
    )
    assert not verify_internal_token("anything")
