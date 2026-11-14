"""Canonical JSON, hashing and commitments used by the history log.

Canonical form: UTF-8 JSON with keys sorted, no insignificant whitespace, non-ASCII kept as is.
Money should be stored as decimal strings, never floats, so the form is portable across languages.
"""

import hashlib
import json
import secrets
from typing import Any

GENESIS_PREV = "sha256:" + "0" * 64

# The keys of a log entry that are covered by its hash, in addition to `prev`.
BODY_KEYS = ("seq", "time", "op", "path", "commit", "detail", "prev")


def canonical(obj: Any) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def sha256(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def new_salt() -> str:
    return secrets.token_hex(16)


def commit(salt: str, field: str, value: Any) -> str:
    """A salted commitment to one field value. The salt stops guessable values being brute-forced."""
    return sha256(canonical([salt, field, value]))


def entry_hash(body: dict) -> str:
    return sha256(canonical({k: body[k] for k in BODY_KEYS}))
