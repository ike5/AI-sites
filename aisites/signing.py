"""Ed25519 signatures over canonical JSON, used to sign the history head.

A valid signature proves only that whoever holds the site key published this body. It says nothing about
whether the times inside it are true; that needs an independent witness (see `anchors`).
"""

import base64
import binascii
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from . import canon
from .errors import SiteError

ALGORITHM = "ed25519"


def load_key(directory: Any) -> Ed25519PrivateKey:
    """The site's private key from `<site dir>/site.key` (base64 of the raw 32 bytes)."""
    path = Path(directory) / "site.key"
    try:
        return Ed25519PrivateKey.from_private_bytes(base64.b64decode(path.read_text().strip(), validate=True))
    except (OSError, ValueError, binascii.Error):
        raise SiteError("no_signing_key", f"The site's signing key in '{path}' is missing or unreadable.",
                        "Restore site.key from a backup; a new key would break continuity with the history.",
                        status=500)


def public_key(key: Ed25519PrivateKey) -> str:
    raw = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return base64.b64encode(raw).decode()


def sign(body: dict, key: Ed25519PrivateKey) -> str:
    """Base64 signature over the canonical form of `body`, so any language can reproduce the bytes."""
    return base64.b64encode(key.sign(canon.canonical(body))).decode()


def verify_signature(body: Any, signature: Any, public_key: Any) -> bool:
    """True only for a valid signature. Malformed input of any kind is simply False."""
    try:
        pub = Ed25519PublicKey.from_public_bytes(base64.b64decode(public_key, validate=True))
        pub.verify(base64.b64decode(signature, validate=True), canon.canonical(body))
        return True
    except (InvalidSignature, ValueError, TypeError, binascii.Error):
        return False
