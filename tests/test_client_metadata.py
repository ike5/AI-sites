import io
import json
import socket
import ssl

import pytest
from mcp.shared.auth import OAuthClientInformationFull

from aisites import SiteError, client_metadata
from aisites.client_metadata import ClientMetadataFetcher, is_metadata_url

URL = "https://assistant.example/oauth/client.json"


def doc(**changes):
    d = {"client_id": URL, "client_name": "Helper", "redirect_uris": ["https://assistant.example/callback"]}
    d.update(changes)
    return {k: v for k, v in d.items() if v is not None}


def ok(body, status=200, ctype="application/json; charset=utf-8"):
    raw = body if isinstance(body, bytes) else json.dumps(body).encode()
    return status, {"content-type": ctype}, raw


class Rig:
    """Injected fetch, resolve and clock, with a record of what was asked."""

    def __init__(self, response=None, addresses=("93.184.216.34",)):
        self.response = response or ok(doc())
        self.addresses = addresses if isinstance(addresses, Exception) else list(addresses)
        self.fetches, self.lookups, self.time = [], [], 1000.0

    def fetch(self, url, address, timeout, max_bytes):
        self.fetches.append((url, address, timeout, max_bytes))
        if isinstance(self.response, Exception):
            raise self.response
        return self.response

    def resolve(self, hostname):
        self.lookups.append(hostname)
        if isinstance(self.addresses, Exception):
            raise self.addresses
        return self.addresses

    def now(self):
        return self.time

    def fetcher(self, **kw):
        return ClientMetadataFetcher(fetch=self.fetch, resolve=self.resolve, now=self.now, **kw)


def code(rig, client_id=URL):
    with pytest.raises(SiteError) as e:
        rig.fetcher().get(client_id)
    return e.value.code


def test_valid_document_is_normalised_and_constructs_client_information():
    rig = Rig(ok(doc(grant_types=["authorization_code", "refresh_token"], client_uri="https://assistant.example/",
                     logo_uri="http://assistant.example/logo.png", software_id="abc", extra="dropped",
                     redirect_uris=["https://assistant.example/cb", "http://127.0.0.1:8123/cb",
                                    "http://[::1]:9/cb", "http://localhost/cb"])))
    result = rig.fetcher().get(URL)
    assert result == {"client_id": URL, "client_name": "Helper", "token_endpoint_auth_method": "none",
                      "redirect_uris": ["https://assistant.example/cb", "http://127.0.0.1:8123/cb",
                                        "http://[::1]:9/cb", "http://localhost/cb"],
                      "grant_types": ["authorization_code", "refresh_token"], "response_types": ["code"],
                      "client_uri": "https://assistant.example/", "software_id": "abc"}
    info = OAuthClientInformationFull(**result)
    assert info.client_id == URL and info.client_secret is None and len(info.redirect_uris) == 4


def test_minimal_document_gets_defaults():
    result = Rig(ok({"client_id": URL, "redirect_uris": ["https://a.example/cb"]})).fetcher().get(URL)
    assert result["grant_types"] == ["authorization_code"] and result["response_types"] == ["code"]
    assert "client_name" not in result
    OAuthClientInformationFull(**result)


@pytest.mark.parametrize("client_id,expected", [
    (URL, True),
    ("https://assistant.example/a?x=1", True),
    ("https://assistant.example:443/a", True),
    ("abc123", False),
    ("", False),
    ("http://assistant.example/client.json", False),
    ("https://assistant.example", False),
    ("https://assistant.example/", False),
    ("https://assistant.example/a#frag", False),
    ("https://user:pw@assistant.example/a", False),
    ("https://user@assistant.example/a", False),
    ("https://assistant.example/a/../b", False),
    ("https://assistant.example/./b", False),
    ("https://assistant.example/a/%2e%2e/b", False),
    ("https://93.184.216.34/a", False),
    ("https://[::1]/a", False),
    ("https://localhost/a", False),
    ("https://app.localhost/a", False),
    ("https://db.internal/a", False),
    ("https://printer.local/a", False),
    ("https://router.home.arpa/a", False),
    ("https://assistant.example:8443/a", False),
    ("https://assistant.example/a b", False),
    ("https://assistant.example\\@evil.example/a", False),
    (None, False),
])
def test_is_metadata_url(client_id, expected):
    assert is_metadata_url(client_id) is expected


def test_address_problems_are_errors_and_never_fetched():
    for bad in ["http://assistant.example/a", "https://localhost/a", "https://x.example/a#f", "https://10.0.0.1/a"]:
        rig = Rig()
        with pytest.raises(SiteError):
            rig.fetcher().get(bad)
        assert rig.fetches == [] and rig.lookups == []


@pytest.mark.parametrize("address", ["127.0.0.1", "10.0.0.5", "192.168.1.1", "172.16.0.1", "169.254.169.254",
                                     "100.64.1.1", "0.0.0.0", "224.0.0.1", "240.0.0.1", "::1", "::", "fc00::1",
                                     "fe80::1", "ff02::1", "::ffff:10.0.0.1", "::ffff:127.0.0.1",
                                     "64:ff9b::a00:1", "2002:a00:1::", "2001:db8::1"])
def test_non_public_resolution_is_refused_without_fetching(address):
    rig = Rig(addresses=[address])
    assert code(rig) == "client_host_not_public"
    assert rig.fetches == []


def test_mixed_public_and_private_is_refused():
    rig = Rig(addresses=["93.184.216.34", "10.0.0.5"])
    assert code(rig) == "client_host_not_public"
    assert rig.fetches == []


@pytest.mark.parametrize("failure", [[], OSError("no such host"), socket.gaierror(-2)])
def test_resolution_failure_is_refused(failure):
    rig = Rig(addresses=failure)
    assert code(rig) == "client_host_unresolvable"
    assert rig.fetches == []


@pytest.mark.parametrize("address", ["93.184.216.34", "2606:2800:220:1:248:1893:25c8:1946"])
def test_public_addresses_are_accepted_and_passed_to_fetch(address):
    rig = Rig(addresses=[address])
    rig.fetcher(timeout=3, max_bytes=999).get(URL)
    assert rig.fetches == [(URL, address, 3, 999)]
    assert rig.lookups == ["assistant.example"]


def test_next_address_is_tried_when_one_is_unreachable():
    rig = Rig(addresses=["93.184.216.34", "2606:2800:220:1::1"])
    calls = []

    def fetch(url, address, timeout, max_bytes):
        calls.append(address)
        if len(calls) == 1:
            raise ConnectionRefusedError()
        return ok(doc())
    ClientMetadataFetcher(fetch=fetch, resolve=rig.resolve, now=rig.now).get(URL)
    assert calls == ["93.184.216.34", "2606:2800:220:1::1"]


def test_unreachable_is_a_502_without_internal_detail():
    rig = Rig(TimeoutError("10.0.0.1 secret"))
    with pytest.raises(SiteError) as e:
        rig.fetcher().get(URL)
    assert e.value.code == "client_metadata_unreachable" and e.value.status == 502
    assert "10.0.0.1" not in e.value.message + e.value.hint


@pytest.mark.parametrize("response,expected", [
    (ok(b"", 301), "client_metadata_redirect"),
    (ok(b"", 302), "client_metadata_redirect"),
    (ok(doc(), 404), "client_metadata_status"),
    (ok(doc(), 500), "client_metadata_status"),
    (ok(doc(), ctype="text/html"), "client_metadata_content_type"),
    ((200, {}, json.dumps(doc()).encode()), "client_metadata_content_type"),
    (ok(b"{not json"), "client_metadata_invalid_json"),
    (ok(b"\xff\xfe"), "client_metadata_invalid_json"),
    (ok([doc()]), "client_metadata_not_object"),
    (ok(doc(client_id="https://assistant.example/other")), "client_metadata_client_id_mismatch"),
    (ok(doc(client_id=None)), "client_metadata_client_id_mismatch"),
    (ok(doc(redirect_uris=None)), "client_metadata_redirect_uris"),
    (ok(doc(redirect_uris=[])), "client_metadata_redirect_uris"),
    (ok(doc(redirect_uris="https://a.example/cb")), "client_metadata_redirect_uris"),
    (ok(doc(redirect_uris=[f"https://a.example/{i}" for i in range(11)])), "client_metadata_redirect_uris"),
    (ok(doc(redirect_uris=["http://a.example/cb"])), "client_metadata_redirect_uris"),
    (ok(doc(redirect_uris=["https://a.example/cb#x"])), "client_metadata_redirect_uris"),
    (ok(doc(redirect_uris=["/relative"])), "client_metadata_redirect_uris"),
    (ok(doc(redirect_uris=["myapp://cb"])), "client_metadata_redirect_uris"),
    (ok(doc(redirect_uris=[5])), "client_metadata_redirect_uris"),
    (ok(doc(client_secret="s3cret")), "client_metadata_secret"),
    (ok(doc(client_secret_expires_at=0)), "client_metadata_secret"),
    (ok(doc(token_endpoint_auth_method="client_secret_basic")), "client_metadata_auth_method"),
    (ok(doc(grant_types=["client_credentials"])), "client_metadata_grant_types"),
    (ok(doc(grant_types=["authorization_code", "implicit"])), "client_metadata_grant_types"),
    (ok(doc(grant_types="authorization_code")), "client_metadata_grant_types"),
    (ok(doc(response_types=["code", "token"])), "client_metadata_response_types"),
    (ok(doc(response_types=["token"])), "client_metadata_response_types"),
])
def test_bad_responses_are_refused(response, expected):
    assert code(Rig(response)) == expected


def test_ten_redirect_uris_and_none_auth_method_are_fine():
    uris = [f"https://a.example/{i}" for i in range(10)]
    result = Rig(ok(doc(redirect_uris=uris, token_endpoint_auth_method="none"))).fetcher().get(URL)
    assert result["redirect_uris"] == uris


def test_body_over_limit_is_refused_and_exact_limit_is_not():
    body = json.dumps(doc()).encode()
    rig = Rig(ok(body))
    with pytest.raises(SiteError) as e:
        rig.fetcher(max_bytes=len(body) - 1).get(URL)
    assert e.value.code == "client_metadata_too_large"
    assert Rig(ok(body)).fetcher(max_bytes=len(body)).get(URL)["client_id"] == URL


def test_errors_never_echo_response_content():
    with pytest.raises(SiteError) as e:
        Rig(ok(b"SECRET-INTERNAL-PAGE 10.1.2.3")).fetcher().get(URL)
    assert "SECRET" not in e.value.message + e.value.hint and "10.1.2.3" not in e.value.message


def test_name_is_cleaned_capped_and_unknown_fields_dropped():
    messy = "  He\x00lp\x1b[31mer‮\n\t" + "x" * 200
    result = Rig(ok(doc(client_name=messy, scope="admin", jwks_uri="https://a.example/k", contacts=["a@b.c"],
                        tos_uri="http://a.example/tos", policy_uri="https://a.example/p",
                        logo_uri=5, software_version="v" * 1000, application_type="web"))).fetcher().get(URL)
    assert result["client_name"].startswith("Help") and len(result["client_name"]) <= 80
    assert not any(ord(c) < 32 or ord(c) == 0x202E for c in result["client_name"])
    assert set(result) == {"client_id", "redirect_uris", "token_endpoint_auth_method", "grant_types",
                           "response_types", "client_name", "policy_uri", "software_version"}
    assert len(result["software_version"]) == 200
    OAuthClientInformationFull(**result)


def test_blank_or_non_string_name_is_omitted():
    assert "client_name" not in Rig(ok(doc(client_name="  \x00 "))).fetcher().get(URL)
    assert "client_name" not in Rig(ok(doc(client_name=["x"]))).fetcher().get(URL)


def test_success_is_cached_then_refetched_after_expiry():
    rig = Rig()
    f = rig.fetcher(cache_seconds=100)
    first = f.get(URL)
    first["client_name"] = "mutated"
    rig.time += 99
    assert f.get(URL)["client_name"] == "Helper"
    assert len(rig.fetches) == 1 and len(rig.lookups) == 1
    rig.time += 2
    f.get(URL)
    assert len(rig.fetches) == 2


def test_failure_is_cached_for_sixty_seconds_then_retried():
    rig = Rig(ok(doc(), 404))
    f = rig.fetcher()
    for _ in range(3):
        with pytest.raises(SiteError) as e:
            f.get(URL)
        assert e.value.code == "client_metadata_status"
    assert len(rig.fetches) == 1
    rig.time += 59
    with pytest.raises(SiteError):
        f.get(URL)
    assert len(rig.fetches) == 1
    rig.time += 2
    rig.response = ok(doc())
    assert f.get(URL)["client_id"] == URL
    assert len(rig.fetches) == 2


def test_resolution_failures_are_cached_too():
    rig = Rig(addresses=[])
    f = rig.fetcher()
    for _ in range(2):
        with pytest.raises(SiteError):
            f.get(URL)
    assert len(rig.lookups) == 1


def test_cache_is_bounded_dropping_the_oldest():
    rig = Rig()

    def fetch(url, address, timeout, max_bytes):
        rig.fetches.append(url)
        return ok(doc(client_id=url))
    f = ClientMetadataFetcher(fetch=fetch, resolve=rig.resolve, now=rig.now)
    urls = [f"https://assistant.example/c/{i}" for i in range(client_metadata.MAX_CACHE_ENTRIES + 5)]
    for u in urls:
        f.get(u)
    assert len(f._cache) == client_metadata.MAX_CACHE_ENTRIES
    count = len(rig.fetches)
    f.get(urls[-1])
    assert len(rig.fetches) == count
    f.get(urls[0])
    assert len(rig.fetches) == count + 1


# ---- the default fetch path, with the socket stood in for

class FakeSock:
    def __init__(self, reply: bytes):
        self.reply, self.sent, self.closed = reply, b"", False

    def sendall(self, data):
        self.sent += data

    def makefile(self, *a, **k):
        return io.BytesIO(self.reply)

    def settimeout(self, t):
        pass

    def close(self):
        self.closed = True


def http_reply(status="200 OK", body=b"{}", extra=""):
    return (f"HTTP/1.1 {status}\r\nContent-Type: application/json\r\n{extra}Connection: close\r\n\r\n"
            .encode() + body)


@pytest.fixture
def wire(monkeypatch):
    state = {"connects": [], "sock": None}

    def connect(address, port, hostname, timeout):
        state["connects"].append((address, port, hostname, timeout))
        return state["sock"]
    monkeypatch.setattr(client_metadata, "_connect", connect)
    return state


def test_default_fetch_connects_to_checked_address_with_right_host_header(wire):
    wire["sock"] = FakeSock(http_reply(body=b'{"a":1}'))
    status, headers, body = client_metadata.default_fetch("https://assistant.example/a/b.json?x=1",
                                                          "93.184.216.34", 4.0, 100)
    assert (status, body) == (200, b'{"a":1}') and headers["content-type"] == "application/json"
    assert wire["connects"] == [("93.184.216.34", 443, "assistant.example", 4.0)]
    sent = wire["sock"].sent.decode()
    assert sent.startswith("GET /a/b.json?x=1 HTTP/1.1\r\n")
    assert "Host: assistant.example\r\n" in sent and "Accept: application/json\r\n" in sent
    assert "User-Agent: AI-sites" in sent


@pytest.mark.parametrize("status", ["301 Moved Permanently", "302 Found", "307 Temporary Redirect"])
def test_default_fetch_does_not_follow_redirects(wire, status):
    wire["sock"] = FakeSock(http_reply(status, b"", "Location: https://10.0.0.1/x\r\n"))
    got, _, body = client_metadata.default_fetch(URL, "93.184.216.34", 4.0, 100)
    assert got == int(status[:3]) and body == b""
    assert len(wire["connects"]) == 1  # no second request, so no second resolution or connection


def test_default_fetch_stops_reading_just_past_the_limit(wire):
    wire["sock"] = FakeSock(http_reply(body=b"x" * 5000))
    _, _, body = client_metadata.default_fetch(URL, "93.184.216.34", 4.0, 100)
    assert len(body) == 101


def test_default_fetch_end_to_end_through_get_rejects_oversize(wire, monkeypatch):
    wire["sock"] = FakeSock(http_reply(body=b" " * 5000))
    f = ClientMetadataFetcher(resolve=lambda h: ["93.184.216.34"], max_bytes=100)
    with pytest.raises(SiteError) as e:
        f.get(URL)
    assert e.value.code == "client_metadata_too_large"


def test_default_fetch_enforces_overall_deadline(wire, monkeypatch):
    wire["sock"] = FakeSock(http_reply(body=b"x" * 5000))
    clock = iter([0.0, 0.0, 99.0, 99.0, 99.0])
    monkeypatch.setattr(client_metadata.time, "monotonic", lambda: next(clock))
    with pytest.raises(TimeoutError):
        client_metadata.default_fetch(URL, "93.184.216.34", 1.0, 10000)


def test_connect_pins_address_and_verifies_certificate_against_hostname(monkeypatch):
    log = []
    raw = FakeSock(b"")

    class Ctx:
        def wrap_socket(self, sock, server_hostname=None):
            log.append(("wrap", sock is raw, server_hostname))
            return "tls"
    monkeypatch.setattr(socket, "create_connection", lambda addr, timeout: log.append(("tcp", addr, timeout)) or raw)
    monkeypatch.setattr(ssl, "create_default_context", lambda: Ctx())
    assert client_metadata._connect("2606:2800::1", 443, "assistant.example", 3.0) == "tls"
    assert log == [("tcp", ("2606:2800::1", 443), 3.0), ("wrap", True, "assistant.example")]


def test_connect_closes_socket_if_tls_fails(monkeypatch):
    raw = FakeSock(b"")

    class Ctx:
        def wrap_socket(self, sock, server_hostname=None):
            raise ssl.SSLCertVerificationError("mismatch")
    monkeypatch.setattr(socket, "create_connection", lambda addr, timeout: raw)
    monkeypatch.setattr(ssl, "create_default_context", lambda: Ctx())
    with pytest.raises(ssl.SSLError):
        client_metadata._connect("93.184.216.34", 443, "assistant.example", 3.0)
    assert raw.closed


def test_default_resolve_lists_each_address_once_ipv4_first(monkeypatch):
    infos = [(10, 1, 6, "", ("2606:2800::1", 443, 0, 0)), (2, 1, 6, "", ("93.184.216.34", 443)),
             (2, 1, 6, "", ("93.184.216.34", 443)), (10, 1, 6, "", ("fe80::1%eth0", 443, 0, 2))]
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: infos)
    assert client_metadata.default_resolve("x.example") == ["93.184.216.34", "2606:2800::1", "fe80::1"]
