"""The hosted service: sign up by email, create a site from a template, connect your own AI.

This sits beside the engine, not inside it. Its state is one small database in the sites folder
(`platform.db`); every site keeps its own database as before. Nothing here is needed to self-host a site.

Sign-in is a one-time code sent by email, so there are no passwords, and signing up, signing in and
recovering access are the same flow. An email address is used once, to send the code, and is then kept
only as a keyed fingerprint.

A person's AI can start registration for them (`start_registration`): the person confirms with the
emailed code on a page that shows a pairing phrase, and the AI then collects an account key. The phrase
is what stops someone else's assistant registering your address and you confirming it by reflex.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import shutil
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Optional

from . import mail, templates, timeview
from .errors import SiteError, not_found
from .site import Site, VERSION
from .store import Store

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS accounts (
    id TEXT PRIMARY KEY, email_hash TEXT NOT NULL UNIQUE, created TEXT NOT NULL,
    sites_limit INTEGER, suspended INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS codes (
    email_hash TEXT PRIMARY KEY, code_hash TEXT NOT NULL, expires TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0, ticket TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS requests (who TEXT NOT NULL, at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS requests_by_who ON requests (who, at);
CREATE TABLE IF NOT EXISTS sessions (
    id_hash TEXT PRIMARY KEY, account TEXT NOT NULL, csrf TEXT NOT NULL, created TEXT NOT NULL,
    expires TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS tickets (
    id TEXT PRIMARY KEY, claim_hash TEXT NOT NULL, email_hash TEXT NOT NULL, phrase TEXT NOT NULL,
    created TEXT NOT NULL, expires TEXT NOT NULL, confirmed TEXT, account TEXT, collected TEXT);
CREATE TABLE IF NOT EXISTS account_keys (
    id TEXT PRIMARY KEY, hash TEXT NOT NULL UNIQUE, account TEXT NOT NULL, label TEXT NOT NULL DEFAULT '',
    created TEXT NOT NULL, revoked TEXT);
CREATE TABLE IF NOT EXISTS sites (
    name TEXT PRIMARY KEY, account TEXT NOT NULL, template TEXT NOT NULL, created TEXT NOT NULL,
    suspended INTEGER NOT NULL DEFAULT 0);
"""

SITE_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{2,62}$")
RESERVED = {"account", "accounts", "register", "sites", "templates", "s", "static", "admin", "api", "www",
            "mcp", "llms", "well-known", "host", "platform", "help", "support", "login", "signup"}
EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
WORDS = ("amber", "birch", "cedar", "delta", "ember", "fjord", "grove", "harbor", "indigo", "juniper", "kelp",
         "lagoon", "maple", "nectar", "olive", "pebble", "quartz", "river", "sable", "tundra", "umber",
         "violet", "willow", "yarrow", "zephyr", "basalt", "clover", "dune", "fern", "glacier", "heron", "iris")

CODE_MINUTES = 10
CODE_ATTEMPTS = 5
TICKET_MINUTES = 15
SESSION_DAYS = 30
CODES_PER_EMAIL_PER_HOUR = 5
CODES_PER_ADDRESS_PER_HOUR = 20
DEFAULTS = {"name": "AI-sites host", "public_url": "", "sites_per_account": 3, "mail": {}}


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


class Platform:
    def __init__(self, sites_dir: Any, clock: Optional[Callable[[], datetime]] = None, mailer: Any = None):
        self.dir = Path(sites_dir)
        if not self.enabled(self.dir):
            raise SiteError("no_platform", f"'{self.dir}' is not set up as a host.",
                            "Run: aisites host init <sites folder>", status=404)
        self.store = Store(str(self.dir / "platform.db"), SCHEMA)
        self.clock = clock or (lambda: datetime.now(timeview.UTC))
        self._secret = (self.dir / "platform.key").read_bytes()
        self._mailer = mailer

    # ------------------------------------------------------------ set-up

    @staticmethod
    def enabled(sites_dir: Any) -> bool:
        return (Path(sites_dir) / "platform.db").exists()

    @classmethod
    def init(cls, sites_dir: Any, clock: Optional[Callable[[], datetime]] = None, mailer: Any = None,
             **config: Any) -> "Platform":
        """Turn a sites folder into a host. Safe to run again to change settings."""
        d = Path(sites_dir)
        d.mkdir(parents=True, exist_ok=True)
        key = d / "platform.key"
        if not key.exists():
            key.write_bytes(secrets.token_bytes(32))
            os.chmod(key, 0o600)
        Store(str(d / "platform.db"), SCHEMA).close()
        platform = cls(d, clock, mailer)
        platform.configure(**config)
        return platform

    @property
    def config(self) -> dict:
        path = self.dir / "platform.json"
        stored = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        return {**DEFAULTS, **stored}

    def configure(self, **changes: Any) -> dict:
        unknown = sorted(set(changes) - set(DEFAULTS))
        if unknown:
            raise SiteError("invalid", f"Unknown host setting: {', '.join(unknown)}.",
                            "Settings: " + ", ".join(DEFAULTS) + ".")
        config = {**self.config, **{k: v for k, v in changes.items() if v is not None}}
        if not isinstance(config["sites_per_account"], int) or config["sites_per_account"] < 1:
            raise SiteError("invalid", "sites_per_account must be a whole number of at least 1.")
        config["public_url"] = str(config["public_url"]).rstrip("/")
        (self.dir / "platform.json").write_text(json.dumps(config, indent=2), encoding="utf-8")
        return config

    @property
    def mailer(self) -> Any:
        return self._mailer or mail.from_config(self.config["mail"], self.dir)

    def now(self) -> datetime:
        return self.clock().astimezone(timeview.UTC).replace(microsecond=0)

    def now_z(self) -> str:
        return timeview.to_z(self.now())

    def _later(self, **delta: Any) -> str:
        return timeview.to_z(self.now() + timedelta(**delta))

    def _mac(self, *parts: str) -> str:
        return hmac.new(self._secret, "|".join(parts).encode(), hashlib.sha256).hexdigest()

    def link(self, path: str) -> str:
        return self.config["public_url"] + path

    # ------------------------------------------------------------ email and codes

    def fingerprint(self, email: Any) -> str:
        """The only form in which an address is kept. Keyed, so it cannot be reversed by guessing."""
        address = str(email or "").strip().lower()
        if len(address) > 254 or not EMAIL.match(address):
            raise SiteError("bad_email", "That does not look like an email address.",
                            "Give the address the sign-in code should be sent to.")
        return self._mac("email", address)

    def _throttle(self, who: str, limit: int, what: str) -> None:
        since = self._later(hours=-1)
        self.store.x("DELETE FROM requests WHERE at < ?", (since,))
        used = self.store.one("SELECT COUNT(*) AS n FROM requests WHERE who=? AND at >= ?", (who, since))["n"]
        if used >= limit:
            err = SiteError("too_many_codes", f"Too many sign-in codes were requested for this {what}.",
                            "Use the last code sent, or try again in an hour.", status=429)
            err.retry_after = 3600
            raise err

    def send_code(self, email: str, address: str = "", ticket: Optional[dict] = None) -> None:
        """Email a one-time code. The answer is the same whether or not an account exists."""
        email_hash = self.fingerprint(email)
        address_key = self._mac("address", address or "unknown")
        code = f"{secrets.randbelow(1_000_000):06d}"
        with self.store.tx():
            self._throttle(email_hash, CODES_PER_EMAIL_PER_HOUR, "address")
            self._throttle(address_key, CODES_PER_ADDRESS_PER_HOUR, "network")
            now = self.now_z()
            self.store.x("INSERT INTO requests (who, at) VALUES (?,?), (?,?)", (email_hash, now, address_key, now))
            self.store.x(
                "INSERT INTO codes (email_hash, code_hash, expires, attempts, ticket) VALUES (?,?,?,0,?) "
                "ON CONFLICT(email_hash) DO UPDATE SET code_hash=excluded.code_hash, expires=excluded.expires, "
                "attempts=0, ticket=excluded.ticket",
                (email_hash, self._mac("code", email_hash, code), self._later(minutes=CODE_MINUTES),
                 ticket["id"] if ticket else ""))
        host = self.config["name"]
        if ticket:
            text = (f"An AI assistant asked to create and manage sites for this email address on {host}.\n\n"
                    f"Pairing phrase: {ticket['phrase']}\n"
                    "Your assistant should have shown you the same phrase.\n\n"
                    f"To allow it, open {self.link('/account/confirm?ticket=' + ticket['id'])}\n"
                    f"and enter this code: {code}\n\n"
                    f"The code is valid for {CODE_MINUTES} minutes. If the phrase does not match, or you did not "
                    "ask for this, do nothing: without the code the assistant gets no access.\n")
            subject = f"Confirm your assistant on {host}"
        else:
            text = (f"Your sign-in code for {host} is: {code}\n\n"
                    f"It is valid for {CODE_MINUTES} minutes. If you did not ask for it, ignore this message.\n")
            subject = f"Your sign-in code for {host}"
        self.mailer.send(str(email).strip(), subject, text)

    def _use_code(self, email_hash: str, code: Any, ticket: str = "") -> str:
        """Check a code and return the account it signs in, creating the account on first use."""
        bad = SiteError("bad_code", "That code is not valid.",
                        "Codes last ten minutes and work once. Ask for a new one.", status=401)
        with self.store.tx():
            row = self.store.one("SELECT * FROM codes WHERE email_hash=?", (email_hash,))
            if row is None or row["expires"] <= self.now_z() or row["ticket"] != ticket:
                raise bad
            if row["attempts"] >= CODE_ATTEMPTS:
                self.store.x("DELETE FROM codes WHERE email_hash=?", (email_hash,))
        if row["attempts"] >= CODE_ATTEMPTS:
            raise bad
        given = self._mac("code", email_hash, re.sub(r"\s", "", str(code or "")))
        if not hmac.compare_digest(given, row["code_hash"]):
            # Counted in its own transaction, so the count survives the refusal.
            self.store.x("UPDATE codes SET attempts = attempts + 1 WHERE email_hash=?", (email_hash,))
            raise bad
        with self.store.tx():
            self.store.x("DELETE FROM codes WHERE email_hash=?", (email_hash,))
            account = self.store.one("SELECT id, suspended FROM accounts WHERE email_hash=?", (email_hash,))
            if account is None:
                account_id = "acct_" + secrets.token_hex(8)
                self.store.x("INSERT INTO accounts (id, email_hash, created) VALUES (?,?,?)",
                             (account_id, email_hash, self.now_z()))
                return account_id
            if account["suspended"]:
                raise SiteError("suspended", "This account has been suspended by the host.",
                                "Contact the host's operator.", status=403)
            return account["id"]

    # ------------------------------------------------------------ browser sessions

    def sign_in(self, email: str, code: Any) -> dict:
        account = self._use_code(self.fingerprint(email), code)
        token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(16)
        self.store.x("INSERT INTO sessions (id_hash, account, csrf, created, expires) VALUES (?,?,?,?,?)",
                     (_sha(token), account, csrf, self.now_z(), self._later(days=SESSION_DAYS)))
        self.store.x("DELETE FROM sessions WHERE expires <= ?", (self.now_z(),))
        return {"token": token, "csrf": csrf, "account": account}

    def session(self, token: Optional[str]) -> Optional[dict]:
        if not token:
            return None
        row = self.store.one("SELECT account, csrf FROM sessions WHERE id_hash=? AND expires > ?",
                             (_sha(token), self.now_z()))
        return {"account": row["account"], "csrf": row["csrf"]} if row else None

    def sign_out(self, token: Optional[str]) -> None:
        if token:
            self.store.x("DELETE FROM sessions WHERE id_hash=?", (_sha(token),))

    # ------------------------------------------------------------ registration started by an AI

    def start_registration(self, email: str, address: str = "") -> dict:
        ticket = {"id": "reg_" + secrets.token_hex(10),
                  "phrase": "-".join(secrets.choice(WORDS) for _ in range(2)) + f"-{secrets.randbelow(90) + 10}"}
        claim = secrets.token_urlsafe(24)
        email_hash = self.fingerprint(email)
        expires = self._later(minutes=TICKET_MINUTES)
        self.store.x("INSERT INTO tickets (id, claim_hash, email_hash, phrase, created, expires) VALUES (?,?,?,?,?,?)",
                     (ticket["id"], _sha(claim), email_hash, ticket["phrase"], self.now_z(), expires))
        self.store.x("DELETE FROM tickets WHERE expires <= ?", (self._later(days=-1),))
        try:
            self.send_code(email, address, ticket)
        except SiteError:
            self.store.x("DELETE FROM tickets WHERE id=?", (ticket["id"],))
            raise
        return {
            "ticket": ticket["id"], "claim": claim, "pairing_phrase": ticket["phrase"], "expires": expires,
            "confirm_at": self.link("/account/confirm?ticket=" + ticket["id"]),
            "collect_at": self.link("/register/" + ticket["id"]),
            "tell_the_person": f"I've asked {self.config['name']} to email you a code. Open the confirmation "
                               f"page, check it shows the phrase \"{ticket['phrase']}\", and enter the code.",
            "next": "After they confirm, GET collect_at with the header Authorization: Bearer <claim> to "
                    "receive an account key. Until then it answers with status pending.",
        }

    def _ticket(self, ticket_id: Any):
        row = self.store.one("SELECT * FROM tickets WHERE id=?", (str(ticket_id or ""),))
        if row is None or row["expires"] <= self.now_z():
            raise SiteError("no_ticket", "That registration has expired or does not exist.",
                            "Start again with POST /register.", status=404)
        return row

    def ticket_phrase(self, ticket_id: Any) -> str:
        row = self._ticket(ticket_id)
        if row["confirmed"]:
            raise SiteError("already_confirmed", "This registration was already confirmed.",
                            "Return to your assistant.", status=409)
        return row["phrase"]

    def confirm_registration(self, ticket_id: Any, code: Any) -> None:
        row = self._ticket(ticket_id)
        if row["confirmed"]:
            raise SiteError("already_confirmed", "This registration was already confirmed.",
                            "Return to your assistant.", status=409)
        account = self._use_code(row["email_hash"], code, row["id"])
        self.store.x("UPDATE tickets SET confirmed=?, account=? WHERE id=?", (self.now_z(), account, row["id"]))

    def collect(self, ticket_id: Any, claim: Optional[str]) -> dict:
        """Hand the account key to the AI that started the registration, once, after confirmation."""
        row = self._ticket(ticket_id)
        if not claim or not hmac.compare_digest(_sha(claim), row["claim_hash"]):
            raise SiteError("bad_claim", "That is not the claim secret this registration was started with.",
                            "Send it as: Authorization: Bearer <claim>", status=401)
        if not row["confirmed"]:
            return {"status": "pending", "pairing_phrase": row["phrase"], "expires": row["expires"]}
        with self.store.tx():
            done = self.store.x("UPDATE tickets SET collected=? WHERE id=? AND collected IS NULL",
                                (self.now_z(), row["id"]))
            if done.rowcount != 1:
                raise SiteError("already_collected", "This registration's account key was already collected.",
                                "Use that key, or issue another on the account page.", status=409)
            key = self.issue_account_key(row["account"], "Issued to the assistant that registered")
        return {"status": "confirmed", "account_key": key["secret"], "key_id": key["id"],
                "note": "Keep this key safe; it cannot be shown again. Send it as Authorization: Bearer <key>.",
                "next": {"templates": self.link("/templates"), "create_a_site": "POST " + self.link("/sites")}}

    # ------------------------------------------------------------ account keys

    def issue_account_key(self, account: str, label: str = "") -> dict:
        secret, key_id = "ais_acct_" + secrets.token_urlsafe(24), "ak_" + secrets.token_hex(6)
        self.store.x("INSERT INTO account_keys (id, hash, account, label, created) VALUES (?,?,?,?,?)",
                     (key_id, _sha(secret), account, str(label or "")[:80], self.now_z()))
        return {"id": key_id, "secret": secret, "label": str(label or "")[:80]}

    def account_keys(self, account: str) -> list:
        return [{"id": r["id"], "label": r["label"], "created": r["created"], "revoked": r["revoked"]}
                for r in self.store.q("SELECT * FROM account_keys WHERE account=? ORDER BY created, id", (account,))]

    def revoke_account_key(self, account: str, key_id: str) -> None:
        done = self.store.x("UPDATE account_keys SET revoked=? WHERE id=? AND account=? AND revoked IS NULL",
                            (self.now_z(), str(key_id), account))
        if done.rowcount != 1:
            raise not_found(f"Key '{key_id}'", "The account page lists your keys.")

    def account_for_key(self, token: Optional[str]) -> Optional[str]:
        if not token:
            return None
        row = self.store.one(
            "SELECT k.account FROM account_keys k JOIN accounts a ON a.id = k.account "
            "WHERE k.hash=? AND k.revoked IS NULL AND a.suspended = 0", (_sha(token),))
        return row["account"] if row else None

    # ------------------------------------------------------------ sites

    def site_urls(self, name: str) -> dict:
        base = self.link(f"/s/{name}")
        return {"site": base + "/", "manifest": base + "/.well-known/aisite.json", "mcp": base + "/mcp/",
                "admin": base + "/admin/ops"}

    def sites(self, account: str) -> list:
        return [{"name": r["name"], "template": r["template"], "created": r["created"],
                 "suspended": bool(r["suspended"]), **self.site_urls(r["name"])}
                for r in self.store.q("SELECT * FROM sites WHERE account=? ORDER BY created, name", (account,))]

    def create_site(self, account: str, name: Any, template: str = "blank", title: str = "",
                    timezone: str = "UTC", language: str = "en", currency: str = "",
                    description: str = "") -> dict:
        """Create a site for an account from a template. The owner key is returned once."""
        name = str(name or "").strip().lower()
        if not SITE_NAME.match(name) or name in RESERVED:
            raise SiteError("bad_name", f"'{name}' cannot be used as a site name.",
                            "Use 3 to 63 lowercase letters, digits, '-' or '_', starting with a letter or digit.")
        if template not in templates.TEMPLATES:
            raise SiteError("bad_template", f"'{template}' is not a template.",
                            "Templates: " + ", ".join(templates.TEMPLATES) + ". GET /templates describes them.")
        timeview.zone(timezone)
        folder = self.dir / name
        with self.store.tx():
            row = self.store.one("SELECT sites_limit FROM accounts WHERE id=?", (account,))
            limit = row["sites_limit"] or self.config["sites_per_account"]
            owned = self.store.one("SELECT COUNT(*) AS n FROM sites WHERE account=?", (account,))["n"]
            if owned >= limit:
                raise SiteError("site_limit", f"This account already has {owned} sites; the limit is {limit}.",
                                "Ask the host's operator to raise it.", status=409)
            if folder.exists() or self.store.one("SELECT 1 FROM sites WHERE name=?", (name,)):
                raise SiteError("name_taken", f"The name '{name}' is already in use.", "Choose another name.",
                                status=409)
            site, owner_key = Site.create(folder, name=str(title or name), description=str(description or ""),
                                          timezone=timezone, language=language, currency=currency, clock=self.clock)
            try:
                applied = templates.apply(site, template)
            except Exception:
                site.close()
                shutil.rmtree(folder, ignore_errors=True)  # a half-built site must not keep the name
                raise
            site.close()
            self.store.x("INSERT INTO sites (name, account, template, created) VALUES (?,?,?,?)",
                         (name, account, template, self.now_z()))
        return {
            "name": name, "template": template, "structures": applied.get("structures", []),
            **self.site_urls(name), "owner_key": owner_key,
            "note": "Keep the owner key safe; it cannot be shown again. If it is lost, sign in to the account "
                    "page, or call POST /sites/<name>/key with an account key, to issue a new one.",
            "connect_your_ai": [
                "MCP: add the `mcp` address as a connector and send the owner key as the header "
                "Authorization: Bearer <owner key>. Call the `describe` tool first.",
                "Web API: GET the `admin` address lists every operation; POST /admin/{target}/{op} with the "
                "same header.",
                "Authoring guide: run `aisites guide`, or read aisites/authoring.md.",
            ],
        }

    def _owned(self, account: str, name: Any):
        row = self.store.one("SELECT * FROM sites WHERE name=? AND account=?", (str(name or ""), account))
        if row is None:  # someone else's site reads exactly like one that does not exist
            raise not_found(f"Site '{name}'", "GET /sites lists your sites.")
        return row

    def new_owner_key(self, account: str, name: Any) -> dict:
        """Issue a new owner key for one of the account's sites. The old key stops working."""
        row = self._owned(account, name)
        site = Site(self.dir / row["name"], self.clock)
        try:
            key = site.new_owner_key()
        finally:
            site.close()
        return {"name": row["name"], "owner_key": key, **self.site_urls(row["name"]),
                "note": "The previous owner key no longer works. Keys you issued for roles are unaffected."}

    # ------------------------------------------------------------ operator

    def suspend(self, name: str, on: bool = True) -> dict:
        done = self.store.x("UPDATE sites SET suspended=? WHERE name=?", (int(on), name))
        if done.rowcount != 1:
            raise not_found(f"Site '{name}'", "Only sites created through sign-up can be suspended here.")
        return {"name": name, "suspended": bool(on)}

    def is_suspended(self, name: str) -> bool:
        row = self.store.one("SELECT suspended FROM sites WHERE name=?", (name,))
        return bool(row and row["suspended"])

    def accounts(self) -> list:
        """For support: accounts by fingerprint with their sites. Addresses are not known to the host."""
        return [{"account": a["id"], "email_fingerprint": a["email_hash"][:16], "created": a["created"],
                 "suspended": bool(a["suspended"]),
                 "sites": [s["name"] for s in self.store.q("SELECT name FROM sites WHERE account=? ORDER BY name",
                                                           (a["id"],))]}
                for a in self.store.q("SELECT * FROM accounts ORDER BY created, id")]

    # ------------------------------------------------------------ what this host is

    def describe(self) -> dict:
        return {
            "aisites_host": VERSION, "name": self.config["name"],
            "what": "A host for AI-sites: sites that AI agents read and act on. Anyone can register by "
                    "email, create a site from a template, and build it with their own AI.",
            "register": {
                "by_a_person": self.link("/account"),
                "by_an_ai": [
                    "POST " + self.link("/register") + " with {\"email\": \"<the person's address>\"}.",
                    "Tell the person the pairing_phrase and the confirm_at address from the answer. They "
                    "receive a code by email and enter it there.",
                    "GET the collect_at address with Authorization: Bearer <claim> until it returns an "
                    "account_key.",
                    "POST " + self.link("/sites") + " with {\"name\": ..., \"template\": ...} and the "
                    "account key to create a site. The answer holds its owner key and addresses.",
                ],
            },
            "templates": self.link("/templates"), "sites": self.link("/sites"),
            "sites_live_at": self.link("/s/<name>/"), "sites_per_account": self.config["sites_per_account"],
            "sign_in": "A one-time code by email. No passwords.",
        }

    def close(self) -> None:
        self.store.close()
