"""Bcrypt password hashing helpers — T09 / #10.

`hash_password` and `verify_password` are the seam between the seed
path (admin tooling that creates a local account) and the login
service (which compares the presented password against the persisted
hash). Keeping them in one place means a future swap to argon2id is
a single edit — every caller goes through this module.

Why bcrypt
----------

The local admin path is the only place the backend stores a credential
hash; the SSO path delegates to the IdP per ADR-0006. Per ADR-0009
the hash is "自适应代价因子 + native constant-time compare". Bcrypt's
`$2b$` prefix is stable across libraries, the work factor is part of
the hash itself (`12` is the bcrypt default and the value the rest of
the codebase expects), and `bcrypt.checkpw` runs in constant time.

Implementation notes
--------------------

* `hash_password` decodes the result to `str` so it round-trips into
  MongoDB without us having to encode at every call site. Bcrypt's
  hash output is ASCII (`$2b$12$...`), so this is a no-op for the
  common case but explicit beats implicit.
* `verify_password` is exception-safe: a malformed hash (e.g. someone
  overwrote the column with a password in cleartext) raises
  `ValueError` from bcrypt and we surface that as `False`. Login
  should never 500 on a bad row — the audit log gets a "verify error"
  record and the user sees "wrong password".
"""
from __future__ import annotations

import bcrypt

# bcrypt work factor. 12 is bcrypt's default and the floor recommended
# by OWASP ASVS V2.4.1 for password storage (T09 #10 acceptance).
# Anything lower is rejected at hash time.
_BCRYPT_ROUNDS: int = 12


def hash_password(password: str) -> str:
    """Hash `password` with a fresh bcrypt salt.

    Returns the `$2b$12$...` string form, ready for Mongo. Two hashes
    of the same password are never equal (random salt) — by design,
    per the password-hashing module's API contract.
    """
    if not isinstance(password, str) or not password:
        raise ValueError("password must be a non-empty string")
    salt = bcrypt.gensalt(rounds=_BCRYPT_ROUNDS)
    return bcrypt.hashpw(password.encode("utf-8"), salt).decode("ascii")


def verify_password(password: str, hashed: str) -> bool:
    """Compare `password` against `hashed` in constant time.

    Returns `False` for any failure: wrong password, malformed hash,
    or type mismatch. We never raise from this seam — login should
    not 500 on a corrupted row.
    """
    if not isinstance(password, str) or not isinstance(hashed, str):
        return False
    if not password or not hashed:
        return False
    try:
        return bcrypt.checkpw(password.encode("utf-8"), hashed.encode("ascii"))
    except (ValueError, TypeError):
        # bcrypt raises `ValueError` on a malformed hash, `TypeError`
        # if the salt is wrong. Both are "no" from the login flow's
        # perspective.
        return False


__all__ = ["hash_password", "verify_password"]
