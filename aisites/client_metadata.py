"""Recognising an assistant by the address it publishes about itself (Client ID Metadata Documents).

In the MCP sign-in flow an assistant may use an https address as its `client_id`; the document at that
address names it and lists where the person may be sent back to. The address comes from a stranger, so
fetching it is a request-forgery risk. Everything here is built around that: the address must be an
ordinary public https one, every IP its name resolves to must be public, the connection is then made
to one of those checked IPs (so the name cannot change its answer between check and fetch), redirects
are refused, and the size and time of the answer are capped. The document is validated field by field
and only known, cleaned fields are passed on.

Network access sits behind two seams, `fetch` and `resolve`, so tests never touch the network.
"""

import http.client
import ipaddress
import json
import socket
import ssl
import threading
import time
from collections import OrderedDict
from copy import deepcopy
from typing import Any, Callable, Optional
from urllib.parse import unquote, urlsplit

from .errors import SiteError

FAILURE_SECONDS = 60
MAX_CACHE_ENTRIES = 500
MAX_REDIRECT_URIS = 10
MAX_URL_LENGTH = 2048
MAX_NAME_LENGTH = 80
MAX_TEXT_LENGTH = 200
GRANTS = ("authorization_code", "refresh_token")
LOOPBACK_HOSTS = ("localhost", "127.0.0.1", "::1")
BLOCKED_SUFFIXES = (".localhost", ".local", ".internal", ".home.arpa")
URL_FIELDS = ("client_uri", "logo_uri", "tos_uri", "policy_uri")
TEXT_FIELDS = ("software_id", "software_version")
USER_AGENT = "AI-sites-client-metadata/1"

ADVICE = "Publish the document at a public https address and use that address as your client_id."


def _bad(code: str, message: str, hint: str = ADVICE, status: int = 400) -> SiteError:
    return SiteError(code, message, hint, status)


# ---------------------------------------------------------------- the address

def _check_address(client_id: Any) -> Any:
    """Rules about the address itself; returns the parsed address or raises."""
    if not isinstance(client_id, str) or not client_id.isascii() or not client_id.isprintable() \
            or " " in client_id or "\\" in client_id or len(client_id) > MAX_URL_LENGTH:
        raise _bad("bad_client_id_url", "The client_id is not a plain https address.")
    try:
        parts = urlsplit(client_id)
        port = parts.port
        host = (parts.hostname or "").lower().rstrip(".")
    except ValueError:
        raise _bad("bad_client_id_url", "The client_id is not a valid address.")
    if parts.scheme != "https":
        raise _bad("bad_client_id_url", "The client_id address must use https.")
    if not host:
        raise _bad("bad_client_id_url", "The client_id address has no host name.")
    if parts.path in ("", "/"):
        raise _bad("bad_client_id_url", "The client_id address needs a path, such as /oauth/client.json.")
    if parts.fragment or "#" in client_id:
        raise _bad("bad_client_id_url", "The client_id address must not contain a # fragment.")
    if parts.username is not None or parts.password is not None or "@" in parts.netloc:
        raise _bad("bad_client_id_url", "The client_id address must not contain a user name or password.")
    if any(seg in (".", "..") for seg in unquote(parts.path).replace("\\", "/").split("/")):
        raise _bad("bad_client_id_url", "The client_id address must not contain . or .. path segments.")
    if port not in (None, 443):
        raise _bad("bad_client_id_url", "The client_id address must use the standard https port.")
    try:
        ipaddress.ip_address(host)
        raise _bad("blocked_client_host", "The client_id address must use a host name, not an IP address.")
    except ValueError:
        pass
    if host == "localhost" or host.endswith(BLOCKED_SUFFIXES):
        raise _bad("blocked_client_host", "The client_id address must use a public host name.")
    return parts


def is_metadata_url(client_id: str) -> bool:
    """True if this client_id is an address to fetch, as opposed to an id the host issued itself."""
    try:
        _check_address(client_id)
        return True
    except SiteError:
        return False


def _is_public(ip: Any) -> bool:
    """Public unicast only; looks through IPv6 forms that carry an IPv4 address."""
    if ip.version == 6:
        embedded = [ip.ipv4_mapped, ip.sixtofour]
        if ip.teredo:
            embedded += list(ip.teredo)
        if ip in ipaddress.ip_network("64:ff9b::/96"):  # NAT64 well-known prefix
            embedded.append(ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF))
        if any(e is not None and not _is_public(e) for e in embedded):
            return False
    return not (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_reserved
                or ip.is_unspecified or not ip.is_global)


# ---------------------------------------------------------------- the network seams

def default_resolve(hostname: str) -> list:
    """All addresses the name resolves to (IPv4 first)."""
    infos = socket.getaddrinfo(hostname, 443, type=socket.SOCK_STREAM)
    found = []
    for info in infos:
        address = info[4][0].split("%")[0]
        if address not in found:
            found.append(address)
    return sorted(found, key=lambda a: ":" in a)


def _connect(address: str, port: int, hostname: str, timeout: float) -> Any:
    """TCP to the checked address; TLS verified against the original host name."""
    sock = socket.create_connection((address, port), timeout)
    try:
        return ssl.create_default_context().wrap_socket(sock, server_hostname=hostname)
    except BaseException:
        sock.close()
        raise


class _PinnedConnection(http.client.HTTPSConnection):
    """Sends the right Host header for `host` but connects to a fixed, already-checked address."""

    def __init__(self, host: str, address: str, timeout: float):
        super().__init__(host, 443, timeout=timeout)
        self._address = address

    def connect(self) -> None:
        self.sock = _connect(self._address, self.port, self.host, self.timeout)


def default_fetch(url: str, address: str, timeout: float, max_bytes: int) -> tuple:
    """GET `url` over a connection pinned to `address`. Redirects are returned, never followed."""
    parts = urlsplit(url)
    target = (parts.path or "/") + (("?" + parts.query) if parts.query else "")
    deadline = time.monotonic() + timeout
    conn = _PinnedConnection(parts.hostname, address, timeout)
    try:
        conn.request("GET", target, headers={"Accept": "application/json", "User-Agent": USER_AGENT,
                                             "Connection": "close"})
        resp = conn.getresponse()
        headers = {k.lower(): v for k, v in resp.getheaders()}
        if 300 <= resp.status < 400:
            return resp.status, headers, b""
        chunks, size = [], 0
        while size <= max_bytes:  # one byte past the limit is enough to know it is too big
            if time.monotonic() > deadline:
                raise TimeoutError("response too slow")
            chunk = resp.read(min(8192, max_bytes + 1 - size))
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
        return resp.status, headers, b"".join(chunks)
    finally:
        conn.close()


# ---------------------------------------------------------------- the document

def _clean_text(value: Any, limit: int) -> Optional[str]:
    if not isinstance(value, str):
        return None
    text = "".join(c for c in value if c.isprintable() or c in " ").strip()
    return text[:limit].strip() or None


def _redirect_uri_ok(uri: Any) -> bool:
    if not isinstance(uri, str) or not uri.isascii() or not uri.isprintable() or " " in uri \
            or len(uri) > MAX_URL_LENGTH or "#" in uri:
        return False
    try:
        parts = urlsplit(uri)
        host = parts.hostname
        parts.port
    except ValueError:
        return False
    if not host or parts.username is not None or parts.password is not None:
        return False
    return parts.scheme == "https" or (parts.scheme == "http" and host in LOOPBACK_HOSTS)


def _https_url(value: Any) -> Optional[str]:
    if not isinstance(value, str) or len(value) > MAX_URL_LENGTH or not value.isascii() \
            or not value.isprintable() or " " in value:
        return None
    try:
        parts = urlsplit(value)
        parts.port
    except ValueError:
        return None
    return value if parts.scheme == "https" and parts.hostname else None


def _list_of_strings(value: Any) -> Optional[list]:
    return value if isinstance(value, list) and all(isinstance(v, str) for v in value) else None


def validate_document(client_id: str, body: bytes) -> dict:
    """Check a fetched document and return the cleaned record, or raise SiteError."""
    try:
        doc = json.loads(body.decode("utf-8"))
    except (ValueError, RecursionError):
        raise _bad("client_metadata_invalid_json", "The client metadata document is not valid JSON.")
    if not isinstance(doc, dict):
        raise _bad("client_metadata_not_object", "The client metadata document must be a JSON object.")
    if doc.get("client_id") != client_id:
        raise _bad("client_metadata_client_id_mismatch",
                   "The document's client_id must be exactly the address it was fetched from.",
                   "Set client_id in the document to the same https address you use as your client_id.")
    uris = doc.get("redirect_uris")
    if not isinstance(uris, list) or not uris:
        raise _bad("client_metadata_redirect_uris", "The document needs a non-empty redirect_uris list.",
                   "List every address the person may be returned to, as https addresses.")
    if len(uris) > MAX_REDIRECT_URIS:
        raise _bad("client_metadata_redirect_uris",
                   f"The document lists more than {MAX_REDIRECT_URIS} redirect_uris.",
                   f"List at most {MAX_REDIRECT_URIS}.")
    if not all(_redirect_uri_ok(u) for u in uris):
        raise _bad("client_metadata_redirect_uris",
                   "Each redirect_uris entry must be an https address without a # fragment "
                   "(http is allowed only for localhost, 127.0.0.1 or [::1]).")
    if "client_secret" in doc or "client_secret_expires_at" in doc:
        raise _bad("client_metadata_secret", "The document must not contain a client_secret.",
                   "An assistant identified by an address has no secret; remove client_secret fields.")
    if doc.get("token_endpoint_auth_method", "none") != "none":
        raise _bad("client_metadata_auth_method", 'token_endpoint_auth_method must be "none" or absent.',
                   "An assistant identified by an address has no secret, so it cannot authenticate with one.")
    grants = ["authorization_code"] if "grant_types" not in doc else _list_of_strings(doc["grant_types"])
    if not grants or any(g not in GRANTS for g in grants):
        raise _bad("client_metadata_grant_types",
                   "grant_types must be a non-empty list drawn from authorization_code and refresh_token.")
    if doc.get("response_types", ["code"]) != ["code"]:
        raise _bad("client_metadata_response_types", 'response_types must be exactly ["code"].')

    result: dict = {"client_id": client_id, "redirect_uris": list(dict.fromkeys(uris)),
                    "token_endpoint_auth_method": "none", "grant_types": list(dict.fromkeys(grants)),
                    "response_types": ["code"]}
    name = _clean_text(doc.get("client_name"), MAX_NAME_LENGTH)
    if name:
        result["client_name"] = name
    for field in URL_FIELDS:
        if _https_url(doc.get(field)):
            result[field] = doc[field]
    for field in TEXT_FIELDS:
        text = _clean_text(doc.get(field), MAX_TEXT_LENGTH)
        if text:
            result[field] = text
    return result


# ---------------------------------------------------------------- the fetcher

class ClientMetadataFetcher:
    """Fetches, checks and caches client metadata documents.

    fetch(url, address, timeout, max_bytes) -> (status, headers, body)   `address` is a checked IP;
        connect to it, not to the name. Headers are a dict with lower-case names. Raise OSError if the
        address cannot be reached.
    resolve(hostname) -> list of IP address strings
    now() -> seconds, for the cache
    """

    def __init__(self, fetch: Optional[Callable] = None, resolve: Optional[Callable] = None,
                 now: Optional[Callable] = None, cache_seconds: float = 86400, max_bytes: int = 65536,
                 timeout: float = 5.0):
        self.fetch = fetch or default_fetch
        self.resolve = resolve or default_resolve
        self.now = now or time.time
        self.cache_seconds, self.max_bytes, self.timeout = cache_seconds, max_bytes, timeout
        self._cache: "OrderedDict[str, tuple]" = OrderedDict()
        self._lock = threading.Lock()

    def get(self, client_id: str) -> dict:
        """The validated document as a dict ready for OAuthClientInformationFull(**result). Raises SiteError."""
        parts = _check_address(client_id)  # cheap and local, so never cached
        with self._lock:
            hit = self._cache.get(client_id)
            if hit and hit[0] > self.now():
                if isinstance(hit[1], SiteError):
                    raise SiteError(hit[1].code, hit[1].message, hit[1].hint, hit[1].status)
                return deepcopy(hit[1])
        try:
            result = self._load(client_id, (parts.hostname or "").lower().rstrip("."))
        except SiteError as e:
            self._remember(client_id, e, FAILURE_SECONDS)
            raise
        self._remember(client_id, result, self.cache_seconds)
        return deepcopy(result)

    def _remember(self, client_id: str, value: Any, seconds: float) -> None:
        with self._lock:
            self._cache.pop(client_id, None)
            self._cache[client_id] = (self.now() + seconds, value)
            while len(self._cache) > MAX_CACHE_ENTRIES:
                self._cache.popitem(last=False)

    def _public_addresses(self, host: str) -> list:
        try:
            found = list(self.resolve(host))
            ips = [ipaddress.ip_address(str(a).split("%")[0]) for a in found]
        except (OSError, ValueError):
            ips = []
        if not ips:
            raise _bad("client_host_unresolvable", "The host name in the client_id does not resolve.",
                       "Check that its DNS records exist and are public.")
        if not all(_is_public(ip) for ip in ips):
            raise _bad("client_host_not_public",
                       "The host name in the client_id resolves to a non-public address.",
                       "Host the document on a public server; private, loopback and link-local "
                       "addresses are refused.")
        return [str(ip) for ip in ips]

    def _load(self, client_id: str, host: str) -> dict:
        addresses = self._public_addresses(host)
        response = None
        for address in addresses:
            try:
                response = self.fetch(client_id, address, self.timeout, self.max_bytes)
                break
            except (OSError, http.client.HTTPException):  # includes timeouts and TLS failures
                continue
        if response is None:
            raise _bad("client_metadata_unreachable",
                       "The client metadata document could not be fetched (connection, TLS or timeout).",
                       "Check the server is up, its certificate matches the host name, and it answers "
                       "within a few seconds.", status=502)
        status, headers, body = response
        if 300 <= status < 400:
            raise _bad("client_metadata_redirect", "The client_id address redirects; redirects are not followed.",
                       "Serve the document directly at the client_id address.")
        if status != 200:
            raise _bad("client_metadata_status", f"The client_id address answered with status {status}, not 200.")
        ctype = str(headers.get("content-type", "")).split(";")[0].strip().lower()
        if ctype != "application/json":
            raise _bad("client_metadata_content_type", "The document must be served as application/json.")
        if len(body) > self.max_bytes:
            raise _bad("client_metadata_too_large",
                       f"The document is larger than {self.max_bytes} bytes.", "Keep it small.")
        return validate_document(client_id, body)
