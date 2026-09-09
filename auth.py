"""
auth.py — Password hashing and authentication.

Uses stdlib hashlib.pbkdf2_hmac + secrets (no bcrypt/passlib dependency).
Salted and slow-hashed, meeting SR-01 for a local prototype.
Never logs or stores plaintext passwords (SR-10).
"""

from __future__ import annotations

import hashlib
import logging
import secrets
import sqlite3
from typing import Optional

import db as _db

logger = logging.getLogger(__name__)

_HASH_ALGO = "sha256"
_ITERATIONS = 260_000   # NIST SP 800-132 recommended minimum for SHA-256 PBKDF2
_SALT_BYTES = 32
_SEP = "$"


def hash_password(plaintext: str) -> str:
    """Return a storable string: algo$iterations$salt_hex$hash_hex."""
    salt = secrets.token_bytes(_SALT_BYTES)
    dk = hashlib.pbkdf2_hmac(_HASH_ALGO, plaintext.encode(), salt, _ITERATIONS)
    return _SEP.join([_HASH_ALGO, str(_ITERATIONS), salt.hex(), dk.hex()])


def verify_password(plaintext: str, stored_hash: str) -> bool:
    """Constant-time verify. Returns False on any malformed hash."""
    try:
        algo, iters_s, salt_hex, hash_hex = stored_hash.split(_SEP)
        iters = int(iters_s)
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(hash_hex)
        candidate = hashlib.pbkdf2_hmac(algo, plaintext.encode(), salt, iters)
        return secrets.compare_digest(candidate, expected)
    except Exception:
        return False


def login(
    conn: sqlite3.Connection,
    email: str,
    password: str,
) -> Optional[sqlite3.Row]:
    """
    Verify credentials.
    Returns the users row on success, None on any failure.
    Does NOT reveal whether the email or password was wrong (FR-01, §12.2).
    """
    user = _db.get_user_by_email(conn, email)
    if user is None:
        # Still do a dummy verify to avoid timing-based user enumeration
        verify_password(password, hash_password("dummy"))
        logger.warning("Login failed (unknown email): %s", email)
        return None

    if not verify_password(password, user["password_hash"]):
        logger.warning("Login failed (bad password): %s", email)
        return None

    logger.info("Login successful: email=%s role=%s", email, user["role"])
    return user
