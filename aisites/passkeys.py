"""Passkeys for the hosted service: sign in without an emailed code, and confirm what hands out control.

Email stays a way to sign in, so a passkey would protect nothing if a mailbox alone could still give
control away. Once an account has a passkey, everything that hands out control needs it: approving an
assistant, issuing an account key or a new owner key, confirming an assistant's sign-up, and adding or
removing a passkey. Signing in by emailed code still lets the person look at their account.

A lost device is handled with a wait: signed in by email, the person asks for their passkeys to be
removed, which happens a day later, and using any passkey in the meantime cancels it.

Checking what a device signs is done by the `webauthn` library (an optional extra). This module is the
part a host supplies: where things are stored and what a confirmation is for.
"""

from __future__ import annotations

import base64
import json
import secrets
from typing import Any, Optional
from urllib.parse import urlsplit

from .errors import SiteError, not_found

CHALLENGE_MINUTES = 5
PROOF_SECONDS = 120
RESET_HOURS = 24
MAX_PER_ACCOUNT = 10
CHALLENGES_PER_ADDRESS_PER_HOUR = 60


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def library() -> Any:
    try:
        import webauthn
        return webauthn
    except ImportError:
        return None


class Passkeys:
    def __init__(self, platform: Any):
        self.platform = platform
        self.store = platform.store

    # ------------------------------------------------------------ whether they can be used here

    def unavailable(self) -> Optional[str]:
        """Why passkeys are off on this host, or None if they are on."""
        if library() is None:
            return "The passkey library is not installed on this host (pip install 'aisites[passkeys]')."
        if not self.platform.connections.available():
            return "Passkeys need this host's public address to be https with no path (or localhost)."
        return None

    @property
    def origin(self) -> str:
        return self.platform.config["public_url"]

    @property
    def rp_id(self) -> str:
        return urlsplit(self.origin).hostname or ""

    def _need(self) -> Any:
        why = self.unavailable()
        if why:
            raise SiteError("no_passkeys", "Passkeys are not available on this host.", why, status=404)
        return library()

    # ------------------------------------------------------------ what an account has

    def _settle(self, account: str) -> None:
        """Carry out a removal the person asked for once its wait is over."""
        due = self.store.one("SELECT due FROM passkey_resets WHERE account=?", (account,))
        if due and due["due"] <= self.platform.now_z():
            with self.store.tx():
                self.store.x("DELETE FROM passkeys WHERE account=?", (account,))
                self.store.x("DELETE FROM passkey_resets WHERE account=?", (account,))

    def has(self, account: str) -> bool:
        self._settle(account)
        return self.store.one("SELECT 1 FROM passkeys WHERE account=? LIMIT 1", (account,)) is not None

    def list(self, account: str) -> list:
        self._settle(account)
        return [{"id": r["id"], "name": r["name"], "created": r["created"], "last_used": r["last_used"]}
                for r in self.store.q("SELECT * FROM passkeys WHERE account=? ORDER BY created, id", (account,))]

    def pending_reset(self, account: str) -> Optional[str]:
        self._settle(account)
        row = self.store.one("SELECT due FROM passkey_resets WHERE account=?", (account,))
        return row["due"] if row else None

    # ------------------------------------------------------------ challenges

    def _challenge(self, purpose: str, account: Optional[str], action: str, options: Any, address: str = "") -> dict:
        lib = self._need()
        now = self.platform.now_z()
        with self.store.tx():
            self.store.x("DELETE FROM passkey_challenges WHERE expires <= ?", (now,))
            if address:
                who, since = self.platform._mac("passkey", address), self.platform._later(hours=-1)
                self.store.x("DELETE FROM requests WHERE at < ?", (since,))
                used = self.store.one("SELECT COUNT(*) AS n FROM requests WHERE who=? AND at >= ?", (who, since))["n"]
                if used >= CHALLENGES_PER_ADDRESS_PER_HOUR:
                    err = SiteError("too_many", "Too many passkey attempts from this network address.",
                                    "Try again in an hour.", status=429)
                    err.retry_after = 3600
                    raise err
                self.store.x("INSERT INTO requests (who, at) VALUES (?,?)", (who, now))
            challenge_id = "pkc_" + secrets.token_urlsafe(18)
            self.store.x("INSERT INTO passkey_challenges (id, challenge, purpose, account, action, expires) "
                         "VALUES (?,?,?,?,?,?)", (challenge_id, options.challenge, purpose, account, action,
                                                  self.platform._later(minutes=CHALLENGE_MINUTES)))
        return {"challenge_id": challenge_id, "options": json.loads(lib.options_to_json(options))}

    def _take(self, challenge_id: Any, purpose: str) -> Any:
        """A challenge is good for one answer, whether or not that answer turns out to be right."""
        with self.store.tx():
            row = self.store.one("SELECT * FROM passkey_challenges WHERE id=? AND purpose=? AND expires > ?",
                                 (str(challenge_id or ""), purpose, self.platform.now_z()))
            self.store.x("DELETE FROM passkey_challenges WHERE id=?", (str(challenge_id or ""),))
        if row is None:
            raise SiteError("bad_passkey", "That passkey request has expired or was already used.",
                            "Start again from the page.", status=400)
        return row

    @staticmethod
    def _credential(given: Any) -> dict:
        try:
            credential = json.loads(given) if isinstance(given, str) else given
        except ValueError:
            credential = None
        if not isinstance(credential, dict) or not isinstance(credential.get("id"), str):
            raise SiteError("bad_passkey", "That is not a passkey answer.", "Start again from the page.", status=400)
        return credential

    def _descriptors(self, account: str) -> list:
        from webauthn.helpers import base64url_to_bytes
        from webauthn.helpers.structs import PublicKeyCredentialDescriptor
        return [PublicKeyCredentialDescriptor(id=base64url_to_bytes(p["id"])) for p in self.list(account)]

    # ------------------------------------------------------------ adding and removing

    def start_adding(self, account: str, name: Any) -> dict:
        lib = self._need()
        from webauthn.helpers.structs import (AuthenticatorSelectionCriteria, ResidentKeyRequirement,
                                              UserVerificationRequirement)
        if len(self.list(account)) >= MAX_PER_ACCOUNT:
            raise SiteError("limit_reached", f"An account can have {MAX_PER_ACCOUNT} passkeys.",
                            "Remove one you no longer use.", status=409)
        shown = " ".join(str(name or "").split())[:60] or "My account"
        options = lib.generate_registration_options(
            rp_id=self.rp_id, rp_name=self.platform.config["name"], user_id=account.encode(),
            user_name=shown, user_display_name=shown, exclude_credentials=self._descriptors(account),
            authenticator_selection=AuthenticatorSelectionCriteria(
                resident_key=ResidentKeyRequirement.REQUIRED, user_verification=UserVerificationRequirement.REQUIRED))
        return self._challenge("add", account, shown, options)

    def finish_adding(self, account: str, challenge_id: Any, credential: Any) -> dict:
        lib = self._need()
        row = self._take(challenge_id, "add")
        if row["account"] != account:
            raise SiteError("bad_passkey", "That passkey request belongs to another sign-in.", status=400)
        try:
            checked = lib.verify_registration_response(
                credential=self._credential(credential), expected_challenge=bytes(row["challenge"]),
                expected_rp_id=self.rp_id, expected_origin=self.origin, require_user_verification=True)
        except SiteError:
            raise
        except Exception:
            raise SiteError("bad_passkey", "That passkey could not be checked.", "Try adding it again.", status=400)
        passkey_id = _b64(checked.credential_id)
        if self.store.one("SELECT 1 FROM passkeys WHERE id=?", (passkey_id,)):
            raise SiteError("bad_passkey", "That passkey is already registered.", status=409)
        self.store.x("INSERT INTO passkeys (id, account, public_key, sign_count, name, created) VALUES (?,?,?,?,?,?)",
                     (passkey_id, account, checked.credential_public_key, checked.sign_count, row["action"],
                      self.platform.now_z()))
        return {"id": passkey_id, "name": row["action"]}

    def remove(self, account: str, passkey_id: Any) -> None:
        if self.store.x("DELETE FROM passkeys WHERE id=? AND account=?", (str(passkey_id or ""), account)).rowcount != 1:
            raise not_found(f"Passkey '{passkey_id}'", "The account page lists your passkeys.")

    # ------------------------------------------------------------ using one

    def start_using(self, purpose: str, account: Optional[str] = None, action: str = "", address: str = "") -> dict:
        """A challenge for signing in (no account yet), or for confirming one action on an account."""
        lib = self._need()
        from webauthn.helpers.structs import UserVerificationRequirement
        options = lib.generate_authentication_options(
            rp_id=self.rp_id, user_verification=UserVerificationRequirement.REQUIRED,
            allow_credentials=self._descriptors(account) if account else None)
        return self._challenge(purpose, account, action, options, address)

    def _verify(self, row: Any, credential: Any) -> str:
        """Check a device's answer to a challenge. Returns the account the passkey belongs to."""
        lib = self._need()
        credential = self._credential(credential)
        known = self.store.one("SELECT * FROM passkeys WHERE id=?", (credential["id"],))
        refused = SiteError("bad_passkey", "That passkey was not accepted.", "Try again, or sign in by email.",
                            status=403)
        if known is None:
            raise refused
        try:
            checked = lib.verify_authentication_response(
                credential=credential, expected_challenge=bytes(row["challenge"]), expected_rp_id=self.rp_id,
                expected_origin=self.origin, credential_public_key=bytes(known["public_key"]),
                credential_current_sign_count=known["sign_count"], require_user_verification=True)
        except Exception:
            raise refused
        self._settle(known["account"])
        if self.store.one("SELECT 1 FROM passkeys WHERE id=?", (known["id"],)) is None:
            raise refused
        with self.store.tx():
            self.store.x("UPDATE passkeys SET sign_count=?, last_used=? WHERE id=?",
                         (checked.new_sign_count, self.platform.now_z(), known["id"]))
            # The device turned up after all, so a removal that was waiting is called off.
            self.store.x("DELETE FROM passkey_resets WHERE account=?", (known["account"],))
        return known["account"]

    def sign_in(self, challenge_id: Any, credential: Any) -> str:
        return self._verify(self._take(challenge_id, "sign_in"), credential)

    def confirm(self, session: dict, challenge_id: Any, credential: Any) -> str:
        """Record that this session just presented its passkey for one named action."""
        row = self._take(challenge_id, "confirm")
        if row["account"] != session["account"] or self._verify(row, credential) != session["account"]:
            raise SiteError("bad_passkey", "That passkey does not belong to this account.", status=403)
        with self.store.tx():
            self.store.x("DELETE FROM passkey_proofs WHERE expires <= ?", (self.platform.now_z(),))
            self.store.x("INSERT OR REPLACE INTO passkey_proofs (session, action, expires) VALUES (?,?,?)",
                         (session["id"], row["action"], self.platform._later(seconds=PROOF_SECONDS)))
        return row["action"]

    def require(self, session: dict, action: str) -> None:
        """Let an action that hands out control go ahead only if the passkey was just presented for it."""
        if not self.has(session["account"]):
            return
        used = self.store.x("DELETE FROM passkey_proofs WHERE session=? AND action=? AND expires > ?",
                            (session["id"], action, self.platform.now_z()))
        if used.rowcount != 1:
            raise SiteError("needs_passkey", "This needs your passkey.",
                            "Use the button on the page; it asks for your passkey and then carries on. If you "
                            "have lost the device, the account page can remove your passkeys after a wait.",
                            status=403)

    def confirm_for(self, account: str, challenge_id: Any, credential: Any, action: str) -> None:
        """The same check where there is no session: a page reached from an email."""
        row = self._take(challenge_id, "confirm")
        if row["action"] != action or self._verify(row, credential) != account:
            raise SiteError("bad_passkey", "That passkey does not belong to this account.", status=403)

    # ------------------------------------------------------------ a lost device

    def ask_removal(self, account: str, email: Any) -> str:
        """Start the wait after which every passkey on the account is removed. A notice goes out at once."""
        owner = self.store.one("SELECT email_hash FROM accounts WHERE id=?", (account,))
        if owner is None or self.platform.fingerprint(email) != owner["email_hash"]:
            raise SiteError("wrong_email", "That is not this account's email address.",
                            "Enter the address you sign in with, so the notice reaches you.", status=400)
        if not self.has(account):
            raise SiteError("no_passkeys", "This account has no passkey to remove.", status=409)
        due = self.platform._later(hours=RESET_HOURS)
        self.store.x("INSERT OR REPLACE INTO passkey_resets (account, due) VALUES (?,?)", (account, due))
        host = self.platform.config["name"]
        self.platform.mailer.send(
            str(email).strip(), f"Your passkeys on {host} will be removed",
            f"Someone signed in to your account on {host} by email asked for its passkeys to be removed.\n\n"
            f"They will be removed at {due}. Until then nothing has changed.\n\n"
            "If this was not you, sign in and cancel it on the account page, or simply use one of your passkeys: "
            f"that cancels it. {self.platform.link('/account')}\n")
        return due

    def cancel_removal(self, account: str) -> None:
        self.store.x("DELETE FROM passkey_resets WHERE account=?", (account,))
