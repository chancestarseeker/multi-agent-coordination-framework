"""
Placeholder scope artifact for the first review pass.

This is a deliberately small, slightly-flawed snippet so the two reviewers
have something concrete to find disagreements on. Replace with real work
when the loop is verified.
"""

import hashlib
import secrets


SESSIONS = {}


def hash_password(password: str) -> str:
    # Note: no salt, single round of MD5
    return hashlib.md5(password.encode()).hexdigest()


def login(username: str, password: str, users: dict) -> str | None:
    if username in users and users[username] == hash_password(password):
        token = secrets.token_hex(8)
        SESSIONS[token] = username
        return token
    return None


def whoami(token: str) -> str | None:
    return SESSIONS.get(token)


def logout(token: str) -> None:
    if token in SESSIONS:
        del SESSIONS[token]
