"""The assistant's side of visitor identity: make a key, ask to be a contact, and get a token to act with.

    from aisites.identity_client import Contact, new_key
    key = new_key()                                   # keep this; it is the person's identity on one site
    me = Contact("https://shop.example", key)
    asked = me.ask("Ana", purpose="Order question")   # {"contact": "ct_...", "status": "pending"}
    token = me.token(asked["contact"])["token"]       # once approved; send as Authorization: Bearer <token>

The same from a shell: `aisites identity new`, `aisites identity ask`, `aisites identity token`.

Use a different key for every site. A site only ever sees the public half.
"""

from __future__ import annotations

import base64
import json
import secrets
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from . import canon
from .errors import SiteError
from .identity import PERMISSION_TYPE, RECOVERY_TYPE, REQUEST_TYPE


def new_key() -> str:
    """A new private key, as base64. Whoever holds it is the contact."""
    raw = Ed25519PrivateKey.generate().private_bytes(
        serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption())
    return base64.b64encode(raw).decode()


def _load(private_key: str) -> Ed25519PrivateKey:
    return Ed25519PrivateKey.from_private_bytes(base64.b64decode(private_key))


def public_of(private_key: str) -> str:
    raw = _load(private_key).public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return base64.b64encode(raw).decode()


def sign(body: dict, private_key: str) -> str:
    """Base64 signature over the object's JSON with keys sorted and no spaces."""
    return base64.b64encode(_load(private_key).sign(canon.canonical(body))).decode()


def _z(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def contact_request(site: str, private_key: str, name: str, purpose: Optional[str] = None,
                    invitation: Optional[str] = None, email: Optional[str] = None,
                    now: Optional[datetime] = None) -> dict:
    """The signed form that asks a site to know this key. `site` is the site's own public key."""
    body = {"type": REQUEST_TYPE, "site": site, "public_key": public_of(private_key), "name": name,
            "time": _z(now or datetime.now(timezone.utc))}
    body.update({k: v for k, v in (("purpose", purpose), ("invitation", invitation), ("email", email)) if v})
    return {"request": body, "signature": sign(body, private_key)}


def permission(site: str, private_key: str, contact: str, may: Optional[list] = None, hours: float = 1,
               now: Optional[datetime] = None) -> dict:
    """A signed statement that lets its holder act as this contact, optionally with fewer permissions."""
    body: dict = {"type": PERMISSION_TYPE, "site": site, "contact": contact,
                  "expires": _z((now or datetime.now(timezone.utc)) + timedelta(hours=hours)),
                  "nonce": secrets.token_urlsafe(18)}
    if may is not None:
        body["may"] = may
    return {"statement": body, "signature": sign(body, private_key)}


def new_key_request(site: str, new_private_key: str, contact: str, email: str,
                    now: Optional[datetime] = None) -> dict:
    """Ask to replace a lost key with a new one, confirmed by a code sent to the contact's address."""
    body = {"type": RECOVERY_TYPE, "site": site, "contact": contact, "public_key": public_of(new_private_key),
            "email": email, "time": _z(now or datetime.now(timezone.utc))}
    return {"request": body, "signature": sign(body, new_private_key)}


def _http(method: str, url: str, body: Optional[dict] = None) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(url, data=data, method=method, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as e:
        try:
            err = json.loads(e.read())["error"]
        except (ValueError, KeyError):
            raise SiteError("http_error", f"The site answered {e.code}.", status=e.code)
        raise SiteError(err.get("code", "http_error"), err.get("message", ""), err.get("hint", ""), status=e.code)


class Contact:
    """One person's identity on one site. `send(method, url, body)` can be replaced, for example in tests."""

    def __init__(self, site_url: str, private_key: str, send: Optional[Callable] = None,
                 now: Optional[Callable] = None):
        self.url, self.key = site_url.rstrip("/"), private_key
        self.send, self.now = send or _http, now or (lambda: datetime.now(timezone.utc))
        self._site: Optional[str] = None

    def info(self) -> dict:
        return self.send("GET", self.url + "/identity")

    @property
    def site(self) -> str:
        if self._site is None:
            self._site = self.info()["site"]
        return self._site

    def ask(self, name: str, purpose: Optional[str] = None, invitation: Optional[str] = None,
            email: Optional[str] = None) -> dict:
        return self.send("POST", self.url + "/identity/contacts",
                         contact_request(self.site, self.key, name, purpose, invitation, email, self.now()))

    def status(self, contact: str) -> dict:
        return self.send("GET", f"{self.url}/identity/contacts/{contact}")

    def token(self, contact: str, may: Optional[list] = None, hours: float = 1) -> dict:
        return self.send("POST", self.url + "/identity/token",
                         permission(self.site, self.key, contact, may, hours, self.now()))

    def replace_key(self, contact: str, email: str) -> dict:
        """Run with the NEW key. Finish with `confirm_key` once the code arrives by email."""
        return self.send("POST", self.url + "/identity/recover",
                         new_key_request(self.site, self.key, contact, email, self.now()))

    def confirm_key(self, contact: str, code: Any) -> dict:
        return self.send("POST", self.url + "/identity/recover/confirm", {"contact": contact, "code": str(code)})
