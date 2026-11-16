"""A passkey device in software, for tests: it makes keys and signs what a browser and authenticator would."""

import base64
import hashlib
import json
import secrets

import cbor2
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec


def b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


class Device:
    """Holds passkeys for one origin. `verified=False` makes a device that did not check who is holding it."""

    def __init__(self, origin: str, verified: bool = True):
        self.origin, self.verified = origin, verified
        self.rp_id = origin.split("://")[1].split(":")[0].split("/")[0]
        self.keys: dict = {}     # credential id -> (private key, user handle, sign count)

    def _client_data(self, kind: str, challenge: str, origin: str = None) -> bytes:
        return json.dumps({"type": kind, "challenge": challenge, "origin": origin or self.origin,
                           "crossOrigin": False}).encode()

    def _auth_data(self, count: int, attested: bytes = b"") -> bytes:
        flags = 0x01 | (0x04 if self.verified else 0) | (0x40 if attested else 0)
        return hashlib.sha256(self.rp_id.encode()).digest() + bytes([flags]) + count.to_bytes(4, "big") + attested

    def create(self, options: dict) -> dict:
        key = ec.generate_private_key(ec.SECP256R1())
        numbers = key.public_key().public_numbers()
        cose = cbor2.dumps({1: 2, 3: -7, -1: 1, -2: numbers.x.to_bytes(32, "big"), -3: numbers.y.to_bytes(32, "big")})
        cred_id = secrets.token_bytes(16)
        self.keys[b64(cred_id)] = [key, unb64(options["user"]["id"]), 0]
        attested = bytes(16) + len(cred_id).to_bytes(2, "big") + cred_id + cose
        attestation = cbor2.dumps({"fmt": "none", "attStmt": {}, "authData": self._auth_data(0, attested)})
        return {"id": b64(cred_id), "rawId": b64(cred_id), "type": "public-key", "response": {
            "clientDataJSON": b64(self._client_data("webauthn.create", options["challenge"])),
            "attestationObject": b64(attestation)}}

    def get(self, options: dict, origin: str = None, credential: str = None) -> dict:
        allowed = [c["id"] for c in options.get("allowCredentials") or []]
        cred_id = credential or next((c for c in self.keys if not allowed or c in allowed), None)
        assert cred_id is not None, "this device holds no passkey the site would accept"
        key, user, count = self.keys[cred_id]
        self.keys[cred_id][2] = count = count + 1
        client = self._client_data("webauthn.get", options["challenge"], origin)
        auth = self._auth_data(count)
        signature = key.sign(auth + hashlib.sha256(client).digest(), ec.ECDSA(hashes.SHA256()))
        return {"id": cred_id, "rawId": cred_id, "type": "public-key", "response": {
            "clientDataJSON": b64(client), "authenticatorData": b64(auth), "signature": b64(signature),
            "userHandle": b64(user)}}
