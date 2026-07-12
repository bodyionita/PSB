"""Pure auth primitives: Argon2id hashing and session-token hashing."""

from __future__ import annotations

from app.security import (
    generate_session_token,
    hash_password,
    hash_session_token,
    verify_password,
)


def test_password_roundtrip():
    h = hash_password("correct horse battery staple")
    assert verify_password(h, "correct horse battery staple") is True
    assert verify_password(h, "wrong") is False


def test_hash_is_salted_and_argon2id():
    h1 = hash_password("same")
    h2 = hash_password("same")
    assert h1 != h2  # random salt
    assert h1.startswith("$argon2id$")


def test_verify_handles_empty_or_malformed_hash():
    assert verify_password("", "anything") is False
    assert verify_password("not-a-real-hash", "anything") is False


def test_session_token_hash_is_deterministic_and_secret_dependent():
    token = generate_session_token()
    assert hash_session_token(token, "s1") == hash_session_token(token, "s1")
    assert hash_session_token(token, "s1") != hash_session_token(token, "s2")


def test_generated_tokens_are_unique():
    assert generate_session_token() != generate_session_token()
