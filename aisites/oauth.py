"""The sign-in flow for assistants: an assistant sends its person to this host, they sign in and approve,
and the assistant receives a token for one site.

This is the standard flow the Model Context Protocol specifies (OAuth 2.1 with PKCE, discovery documents
and resource indicators). The protocol itself is the MCP library's (`mcp.server.auth`); this module is the
part a host must supply: where things are stored, and what an approval means here.

An approval is for one site and gives the assistant the same power over it as that site's owner key,
until the person disconnects it. One approved assistant on one site is a "connection".

Expiry is decided here, by the host's clock. The library also compares against real time, so every
`expires_at` handed to it is given as "this many seconds from now".
"""

from __future__ import annotations

import hashlib
import json
import secrets
import time
from typing import Any, Optional

import anyio
from mcp.server.auth.middleware.client_auth import AuthenticationError, ClientAuthenticator
from mcp.server.auth.provider import (AccessToken, AuthorizationCode, AuthorizationParams, AuthorizeError,
                                      RefreshToken, RegistrationError, TokenError, construct_redirect_uri)
from mcp.server.auth.routes import build_metadata, cors_middleware, create_auth_routes, validate_issuer_url
from mcp.server.auth.settings import ClientRegistrationOptions, RevocationOptions
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from pydantic import AnyHttpUrl
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from . import client_metadata, timeview
from .errors import SiteError, not_found
from .ops import Principal

SCOPE = "site"
REQUEST_MINUTES = 10
CODE_MINUTES = 2
ACCESS_MINUTES = 60
REFRESH_DAYS = 30
REGISTRATIONS_PER_HOUR = 300   # across the whole host
REGISTRATIONS_PER_ADDRESS_PER_HOUR = 20
UNUSED_CLIENT_DAYS = 7


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


class Connections:
    """Storage and decisions for the flow, on the platform's database. All methods are synchronous."""

    def __init__(self, platform: Any, fetcher: Any = None):
        self.platform = platform
        self.store = platform.store
        self.fetcher = fetcher or client_metadata.ClientMetadataFetcher()

    # ------------------------------------------------------------ availability

    @property
    def issuer(self) -> str:
        return self.platform.config["public_url"]

    def available(self) -> bool:
        """The standard requires HTTPS for the issuer, or localhost for trying it out."""
        try:
            url = AnyHttpUrl(self.issuer)
            validate_issuer_url(url)
        except Exception:
            return False
        # The discovery documents sit at fixed places under the address, so it must be one with no path.
        return (url.path or "/") == "/" and not url.query

    def _seconds_left(self, expires_z: str) -> float:
        return (timeview.from_z(expires_z) - self.platform.now()).total_seconds()

    # ------------------------------------------------------------ assistants (clients)

    def client(self, client_id: str) -> Optional[OAuthClientInformationFull]:
        row = self.store.one("SELECT info FROM oauth_clients WHERE client_id=?", (client_id,))
        if row:
            return OAuthClientInformationFull(**json.loads(row["info"]))
        if self.platform.config["client_metadata_urls"] and client_metadata.is_metadata_url(client_id):
            try:
                return OAuthClientInformationFull(**{**self.fetcher.get(client_id), "scope": SCOPE})
            except SiteError:
                return None  # the library answers "invalid client"; the reason is not the caller's to learn
        return None

    def may_register(self, address: str) -> bool:
        """Count one registration attempt from this network address; False once it has made too many."""
        who, since = self.platform._mac("register", address or "unknown"), self.platform._later(hours=-1)
        with self.store.tx():
            self.store.x("DELETE FROM requests WHERE at < ?", (since,))
            used = self.store.one("SELECT COUNT(*) AS n FROM requests WHERE who=? AND at >= ?", (who, since))["n"]
            if used >= REGISTRATIONS_PER_ADDRESS_PER_HOUR:
                return False
            self.store.x("INSERT INTO requests (who, at) VALUES (?,?)", (who, self.platform.now_z()))
            return True

    def register(self, info: OAuthClientInformationFull) -> None:
        now = self.platform.now_z()
        with self.store.tx():
            self.store.x("DELETE FROM oauth_clients WHERE used IS NULL AND created < ?",
                         (self.platform._later(days=-UNUSED_CLIENT_DAYS),))
            recent = self.store.one("SELECT COUNT(*) AS n FROM oauth_clients WHERE created >= ?",
                                    (self.platform._later(hours=-1),))["n"]
            if recent >= REGISTRATIONS_PER_HOUR:
                raise RegistrationError(error="invalid_client_metadata",
                                        error_description="Too many assistants registered recently; try later.")
            self.store.x("INSERT INTO oauth_clients (client_id, info, created) VALUES (?,?,?)",
                         (info.client_id, info.model_dump_json(exclude_none=True), now))

    @staticmethod
    def _name_of(client: OAuthClientInformationFull) -> str:
        name = " ".join(str(client.client_name or "").split())[:80]
        return name or "An assistant"

    # ------------------------------------------------------------ which site a request is for

    def site_of(self, resource: Optional[str]) -> Optional[str]:
        """The site whose MCP door this address is, or None. Only this host's own sites qualify."""
        if not resource:
            return None
        base = self.issuer + "/s/"
        target = str(resource).rstrip("/")
        if not target.startswith(base) or not target.endswith("/mcp"):
            return None
        name = target[len(base):-len("/mcp")]
        return name if "/" not in name and self.store.one("SELECT 1 FROM sites WHERE name=?", (name,)) else None

    def resource_of(self, site: str) -> str:
        return f"{self.issuer}/s/{site}/mcp"

    # ------------------------------------------------------------ approval requests

    def start(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        site = self.site_of(params.resource)
        if params.resource and site is None:
            raise AuthorizeError(error="invalid_request",
                                 error_description="The resource is not the MCP address of a site on this host.")
        request_id = "req_" + secrets.token_urlsafe(18)
        self._sweep()
        self.store.x("DELETE FROM oauth_requests WHERE expires <= ?", (self.platform.now_z(),))
        self.store.x("INSERT INTO oauth_requests (id, client_id, client_name, params, site, expires) "
                     "VALUES (?,?,?,?,?,?)",
                     (request_id, client.client_id, self._name_of(client), params.model_dump_json(), site,
                      self.platform._later(minutes=REQUEST_MINUTES)))
        return self.platform.link("/account/authorize?request=" + request_id)

    def pending(self, request_id: Any) -> dict:
        row = self.store.one("SELECT * FROM oauth_requests WHERE id=? AND expires > ?",
                             (str(request_id or ""), self.platform.now_z()))
        if row is None:
            raise SiteError("no_request", "This approval request has expired or does not exist.",
                            "Go back to your assistant and connect again.", status=404)
        params = json.loads(row["params"])
        return {"id": row["id"], "client_id": row["client_id"], "client_name": row["client_name"],
                "site": row["site"], "params": params, "returns_to": str(params["redirect_uri"])}

    def _back(self, request: dict, **answer: Optional[str]) -> str:
        return construct_redirect_uri(request["returns_to"], state=request["params"].get("state"),
                                      iss=self.issuer, **answer)

    def deny(self, request_id: Any) -> str:
        request = self.pending(request_id)
        self.store.x("DELETE FROM oauth_requests WHERE id=?", (request["id"],))
        return self._back(request, error="access_denied", error_description="The person did not approve.")

    def approve(self, request_id: Any, account: str, site: Optional[str] = None) -> str:
        """Record the person's approval and return where to send them, carrying a single-use code."""
        request = self.pending(request_id)
        site = request["site"] or site
        owned = self.store.one("SELECT suspended FROM sites WHERE name=? AND account=?", (str(site or ""), account))
        if owned is None:
            raise SiteError("not_your_site", "That site is not one of yours, so it cannot be connected.",
                            "Choose one of your own sites, or deny the request.", status=403)
        if owned["suspended"]:
            raise SiteError("suspended", "That site is suspended and cannot be connected.", status=403)
        code = secrets.token_urlsafe(32)
        params = {**request["params"], "resource": self.resource_of(site),
                  "scopes": request["params"].get("scopes") or [SCOPE]}
        with self.store.tx():
            done = self.store.x("DELETE FROM oauth_requests WHERE id=?", (request["id"],))
            if done.rowcount != 1:
                raise SiteError("no_request", "This approval request was already answered.", status=409)
            self.store.x("DELETE FROM oauth_codes WHERE expires <= ?", (self.platform.now_z(),))
            self.store.x("INSERT INTO oauth_codes (code_hash, client_id, client_name, account, site, params, expires) "
                         "VALUES (?,?,?,?,?,?,?)",
                         (_sha(code), request["client_id"], request["client_name"], account, site,
                          json.dumps(params), self.platform._later(minutes=CODE_MINUTES)))
            self.store.x("UPDATE oauth_clients SET used=? WHERE client_id=?",
                         (self.platform.now_z(), request["client_id"]))
        return self._back(request, code=code)

    # ------------------------------------------------------------ codes and tokens

    def code(self, client_id: str, code: str) -> Optional[AuthorizationCode]:
        row = self.store.one("SELECT * FROM oauth_codes WHERE code_hash=? AND client_id=? AND expires > ?",
                             (_sha(code), client_id, self.platform.now_z()))
        if row is None:
            return None
        p = json.loads(row["params"])
        return AuthorizationCode(
            code=code, scopes=p["scopes"], expires_at=time.time() + self._seconds_left(row["expires"]),
            client_id=client_id, code_challenge=p["code_challenge"], redirect_uri=p["redirect_uri"],
            redirect_uri_provided_explicitly=p["redirect_uri_provided_explicitly"], resource=p["resource"],
            subject=row["account"])

    def _issue(self, connection: str, scopes: list, resource: str, renewable: bool = True) -> OAuthToken:
        access = "ais_at_" + secrets.token_urlsafe(32)
        refresh = "ais_rt_" + secrets.token_urlsafe(32) if renewable else None
        for token, kind, expires in ((access, "access", self.platform._later(minutes=ACCESS_MINUTES)),
                                     (refresh, "refresh", self.platform._later(days=REFRESH_DAYS))):
            if token:
                self.store.x("INSERT INTO oauth_tokens (token_hash, kind, connection, scopes, resource, expires) "
                             "VALUES (?,?,?,?,?,?)",
                             (_sha(token), kind, connection, " ".join(scopes), resource, expires))
        return OAuthToken(access_token=access, expires_in=ACCESS_MINUTES * 60, scope=" ".join(scopes),
                          refresh_token=refresh)

    def exchange_code(self, client_id: str, code: AuthorizationCode, renewable: bool = True) -> OAuthToken:
        """Turn a code into tokens, once. `renewable` is whether this assistant asked to be able to renew."""
        with self.store.tx():
            row = self.store.one("SELECT * FROM oauth_codes WHERE code_hash=? AND client_id=?",
                                 (_sha(code.code), client_id))
            done = self.store.x("DELETE FROM oauth_codes WHERE code_hash=?", (_sha(code.code),))
            if row is None or done.rowcount != 1 or row["expires"] <= self.platform.now_z():
                raise TokenError(error="invalid_grant", error_description="The code was already used or has expired.")
            connection = "con_" + secrets.token_hex(8)
            self.store.x("INSERT INTO oauth_connections (id, account, site, client_id, client_name, created) "
                         "VALUES (?,?,?,?,?,?)", (connection, row["account"], row["site"], client_id,
                                                  row["client_name"], self.platform.now_z()))
            return self._issue(connection, code.scopes, self.resource_of(row["site"]), renewable)

    def _live(self, token: str, kind: str):
        return self.store.one(
            "SELECT t.*, c.client_id, c.account, c.site FROM oauth_tokens t JOIN oauth_connections c "
            "ON c.id = t.connection WHERE t.token_hash=? AND t.kind=? AND t.expires > ? AND c.revoked IS NULL",
            (_sha(token), kind, self.platform.now_z()))

    def refresh_token(self, client_id: str, token: str) -> Optional[RefreshToken]:
        row = self._live(token, "refresh")
        if row is None or row["client_id"] != client_id:
            return None
        return RefreshToken(token=token, client_id=client_id, scopes=row["scopes"].split(),
                            expires_at=int(time.time() + self._seconds_left(row["expires"])),
                            resource=row["resource"], subject=row["account"])

    def exchange_refresh(self, client_id: str, token: RefreshToken, scopes: list) -> OAuthToken:
        with self.store.tx():
            row = self._live(token.token, "refresh")
            if row is None or row["client_id"] != client_id:
                raise TokenError(error="invalid_grant", error_description="The refresh token is no longer valid.")
            # Replaced on every use, so a copied refresh token is of no use once the real one is used.
            self.store.x("DELETE FROM oauth_tokens WHERE connection=?", (row["connection"],))
            granted = row["scopes"].split()
            return self._issue(row["connection"], [s for s in scopes if s in granted] or granted, row["resource"])

    def access_token(self, token: str) -> Optional[AccessToken]:
        row = self._live(token, "access")
        if row is None:
            return None
        return AccessToken(token=token, client_id=row["client_id"], scopes=row["scopes"].split(),
                           expires_at=int(time.time() + self._seconds_left(row["expires"])),
                           resource=row["resource"], subject=row["account"])

    def revoke(self, token: str, client_id: Optional[str] = None) -> None:
        """End the connection a token belongs to. An assistant can end only its own."""
        row = self.store.one("SELECT t.connection, c.client_id FROM oauth_tokens t JOIN oauth_connections c "
                             "ON c.id = t.connection WHERE t.token_hash=?", (_sha(token),))
        if row and client_id in (None, row["client_id"]):
            self._end(row["connection"])

    def _sweep(self) -> None:
        """Drop tokens that have expired, and close connections left with none (not renewed in time)."""
        now = self.platform.now_z()
        with self.store.tx():
            self.store.x("DELETE FROM oauth_tokens WHERE expires <= ?", (now,))
            self.store.x("UPDATE oauth_connections SET revoked=? WHERE revoked IS NULL AND id NOT IN "
                         "(SELECT connection FROM oauth_tokens)", (now,))

    def _end(self, connection: str) -> None:
        with self.store.tx():
            self.store.x("UPDATE oauth_connections SET revoked=? WHERE id=? AND revoked IS NULL",
                         (self.platform.now_z(), connection))
            self.store.x("DELETE FROM oauth_tokens WHERE connection=?", (connection,))

    # ------------------------------------------------------------ what a site and an account see

    def principal_for(self, token: Optional[str], site: str) -> Optional[Principal]:
        """Who this token is on this site: its owner, acting through a named connection, or nobody."""
        if not token or not token.startswith("ais_at_"):
            return None
        row = self._live(token, "access")
        if row is None or row["site"] != site:
            return None
        owner = self.store.one("SELECT account, suspended FROM sites WHERE name=?", (site,))
        if owner is None or owner["account"] != row["account"] or owner["suspended"]:
            return None
        recently = self.platform._later(minutes=-1)
        self.store.x("UPDATE oauth_connections SET last_used=? WHERE id=? AND (last_used IS NULL OR last_used < ?)",
                     (self.platform.now_z(), row["connection"], recently))
        return Principal("owner", f"assistant:{row['connection']}")

    def connections(self, account: str) -> list:
        self._sweep()
        return [{"id": r["id"], "assistant": r["client_name"], "site": r["site"], "connected": r["created"],
                 "last_used": r["last_used"]}
                for r in self.store.q("SELECT * FROM oauth_connections WHERE account=? AND revoked IS NULL "
                                      "ORDER BY created, id", (account,))]

    def disconnect(self, account: str, connection: Any) -> None:
        row = self.store.one("SELECT id FROM oauth_connections WHERE id=? AND account=? AND revoked IS NULL",
                             (str(connection or ""), account))
        if row is None:
            raise not_found(f"Connection '{connection}'", "The account page lists your connected assistants.")
        self._end(row["id"])

    def resource_metadata(self, site: str) -> dict:
        return {"resource": self.resource_of(site), "authorization_servers": [self.issuer],
                "scopes_supported": [SCOPE], "bearer_methods_supported": ["header"],
                "resource_name": f"{site} on {self.platform.config['name']}"}

    def resource_metadata_url(self, site: str) -> str:
        return f"{self.issuer}/.well-known/oauth-protected-resource/s/{site}/mcp"


class HostAuthProvider:
    """The MCP library's provider interface, answered from `Connections`. Database work runs in a thread."""

    def __init__(self, connections: Connections):
        self.c = connections

    async def get_client(self, client_id: str) -> Optional[OAuthClientInformationFull]:
        return await anyio.to_thread.run_sync(self.c.client, client_id)

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        await anyio.to_thread.run_sync(self.c.register, client_info)

    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        return await anyio.to_thread.run_sync(self.c.start, client, params)

    async def load_authorization_code(self, client: OAuthClientInformationFull, authorization_code: str):
        return await anyio.to_thread.run_sync(self.c.code, client.client_id, authorization_code)

    async def exchange_authorization_code(self, client: OAuthClientInformationFull, authorization_code: Any):
        return await anyio.to_thread.run_sync(self.c.exchange_code, client.client_id, authorization_code,
                                              "refresh_token" in (client.grant_types or []))

    async def load_refresh_token(self, client: OAuthClientInformationFull, refresh_token: str):
        return await anyio.to_thread.run_sync(self.c.refresh_token, client.client_id, refresh_token)

    async def exchange_refresh_token(self, client: OAuthClientInformationFull, refresh_token: Any, scopes: list):
        return await anyio.to_thread.run_sync(self.c.exchange_refresh, client.client_id, refresh_token, scopes)

    async def load_access_token(self, token: str):
        return await anyio.to_thread.run_sync(self.c.access_token, token)

    async def revoke_token(self, token: Any) -> None:
        await anyio.to_thread.run_sync(self.c.revoke, token.token, token.client_id)

    async def exchange_identity_assertion(self, client: Any, params: Any):
        raise TokenError(error="unsupported_grant_type", error_description="Not supported by this host.")


class _LimitedRegistration:
    """The library's registration endpoint, behind a limit per network address."""

    def __init__(self, inner: Any, connections: Connections):
        self.inner, self.c = inner, connections

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] == "http" and scope["method"] == "POST":
            address = scope["client"][0] if scope.get("client") else "unknown"
            if not await anyio.to_thread.run_sync(self.c.may_register, address):
                refusal = JSONResponse(
                    {"error": "invalid_client_metadata",
                     "error_description": "Too many registrations from this network address; try again in an hour."},
                    status_code=429, headers={"Retry-After": "3600", "Access-Control-Allow-Origin": "*"})
                return await refusal(scope, receive, send)
        await self.inner(scope, receive, send)


def routes(connections: Connections) -> list:
    """The standard endpoints, from the library, with the discovery document stating what this host supports."""
    issuer = AnyHttpUrl(connections.issuer)
    registration = ClientRegistrationOptions(enabled=True, valid_scopes=[SCOPE], default_scopes=[SCOPE])
    revocation = RevocationOptions(enabled=True)
    made = create_auth_routes(HostAuthProvider(connections), issuer, client_registration_options=registration,
                              revocation_options=revocation)
    metadata = build_metadata(issuer, None, registration, revocation).model_dump(mode="json", exclude_none=True)
    # Exactly the address the site's own document and the `iss` answer give: clients compare them as text.
    metadata["issuer"] = connections.issuer
    metadata["token_endpoint_auth_methods_supported"] = ["none", "client_secret_post", "client_secret_basic"]
    metadata["revocation_endpoint_auth_methods_supported"] = metadata["token_endpoint_auth_methods_supported"]
    metadata["authorization_response_iss_parameter_supported"] = True
    if connections.platform.config["client_metadata_urls"]:
        metadata["client_id_metadata_document_supported"] = True

    async def discovery(request: Any) -> JSONResponse:
        return JSONResponse(metadata, headers={"Cache-Control": "public, max-age=3600",
                                               "Access-Control-Allow-Origin": "*"})

    path = "/.well-known/oauth-authorization-server"
    out = [Route(path, discovery, methods=["GET"])]
    for route in made:
        if route.path == path:
            continue
        if route.path == "/register":
            route = Route(route.path, _LimitedRegistration(route.app, connections), methods=["POST", "OPTIONS"])
        if route.path == "/revoke":
            route = Route(route.path, cors_middleware(_revocation(connections), ["POST", "OPTIONS"]),
                          methods=["POST", "OPTIONS"])
        out.append(route)
    return out


def _revocation(connections: Connections) -> Any:
    """The revocation endpoint. The library's own insists on a client secret, which an assistant that
    registered without one (as most do) cannot send; this one accepts both kinds."""
    authenticator = ClientAuthenticator(HostAuthProvider(connections))

    async def revoke(request: Request) -> Response:
        try:
            client = await authenticator.authenticate_request(request)
        except AuthenticationError as e:
            return JSONResponse({"error": "unauthorized_client", "error_description": e.message}, status_code=401)
        token = (await request.form()).get("token")
        if not isinstance(token, str) or not token:
            return JSONResponse({"error": "invalid_request", "error_description": "token is required"},
                                status_code=400)
        await anyio.to_thread.run_sync(connections.revoke, token, client.client_id)
        # The same answer whether or not the token existed, as the standard asks.
        return Response(status_code=200, headers={"Cache-Control": "no-store", "Pragma": "no-cache"})

    return revoke
