"""Check a site's public history from the outside.

What a passing run means, and what it does not:
- The chain is internally consistent: every entry links to the one before, every hash and every served
  value matches its commitment.
- The head is signed by the key named in the genesis entry. That proves the holder of the site key
  published this history; it does not prove the dates in it are true, because that key can sign a
  rewritten history with old dates just as easily.
- With a pin saved from an earlier run, the history has not been rewritten or truncated since that run.
  Without one, a rewrite is invisible. Independent timestamping is left to a later anchor.

`fetch(path, params) -> dict` returns parsed JSON for a site-relative path, so the same checks run
against a served site (`url_fetch`), a site folder (`local_fetch`) or a test client.
"""

import json
import urllib.parse
import urllib.request
from typing import Any, Callable, Optional

from . import canon
from .history import verify_entries
from .signing import ALGORITHM, verify_signature

PAGE = 1000
Fetch = Callable[[str, dict], dict]


def verify_site(fetch: Fetch, pin: Optional[dict] = None) -> dict:
    problems: list = []
    report: dict = {"ok": False, "problems": problems, "entries": 0, "head": None, "first_published": None,
                    "sealed": 0, "redacted": 0, "pin": None}

    # The head is read first: entries appended while we page are then simply beyond it.
    try:
        head = fetch("/history/head", {})
        checkpoint = head["checkpoint"]
        signature = head["signature"]
        head_seq, head_hash, head_key = checkpoint["seq"], checkpoint["hash"], checkpoint["key"]
    except Exception as e:
        problems.append(f"could not read a signed head from /history/head: {e}")
        return report
    report["head"], report["first_published"] = checkpoint, head.get("first_published")
    if head.get("algorithm") != ALGORITHM:
        problems.append(f"head uses algorithm {head.get('algorithm')!r}; only {ALGORITHM} is understood")
    elif not verify_signature(checkpoint, signature, head_key):
        problems.append("head signature is not valid for the key it names")

    pin_seq = pin_hash = None
    if pin is not None:
        if isinstance(pin, dict) and isinstance(pin.get("seq"), int) and isinstance(pin.get("hash"), str):
            pin_seq, pin_hash = pin["seq"], pin["hash"]
        else:
            problems.append('pin is not of the form {"seq": <integer>, "hash": "<hash>"}')

    prev, start_seq, after = canon.GENESIS_PREV, 1, 0
    genesis = None
    seen_head = seen_pin = False
    last_seq = 0
    while True:
        try:
            page = fetch("/history", {"after": after, "limit": PAGE})
            entries = page["entries"]
        except Exception as e:
            problems.append(f"could not read /history after entry {after}: {e}")
            return report
        problems += verify_entries(entries, prev, start_seq)
        for e in entries:
            seq = e.get("seq")
            if seq == 1:
                genesis = e
            if seq == head_seq:
                seen_head = True
                if e.get("hash") != head_hash:
                    problems.append(f"entry {seq} does not have the hash the signed head names")
            if pin_seq is not None and seq == pin_seq:
                seen_pin = True
                if e.get("hash") != pin_hash:
                    problems.append(f"history was rewritten since the pin: entry {seq} has a different hash")
            if e.get("op") == "sealed" and "revealed" not in e:
                report["sealed"] += 1
            report["redacted"] += len(e.get("redacted") or {})
        report["entries"] += len(entries)
        if entries:
            prev, start_seq = entries[-1].get("hash"), entries[-1].get("seq", start_seq) + 1
            last_seq = entries[-1].get("seq", last_seq)
        nxt = page.get("next_after")
        if nxt is None or not entries:
            break
        if not isinstance(nxt, int) or nxt <= after:
            problems.append("history paging does not move forward")
            break
        after = nxt

    if genesis is None:
        problems.append("history has no genesis entry")
    else:
        if (genesis.get("detail") or {}).get("public_key") != head_key:
            problems.append("head is signed with a different key from the one in the genesis entry")
        if head.get("first_published") not in (None, genesis.get("time")):
            problems.append("first_published does not match the genesis entry")
    if not seen_head:
        problems.append(f"the signed head names entry {head_seq}, but history ends at entry {last_seq}")
    if pin_seq is not None and not seen_pin:
        problems.append(f"history was truncated: the pin names entry {pin_seq}, but history ends at "
                        f"entry {last_seq}")

    report["ok"] = not problems
    report["pin"] = {"seq": head_seq, "hash": head_hash}
    return report


def url_fetch(base: str, timeout: float = 30) -> Fetch:
    """Fetch over HTTP(S) with the standard library. `base` is the site's root, e.g. https://x.org/s/shop."""
    root = base.rstrip("/")

    def fetch(path: str, params: dict) -> dict:
        url = root + path + ("?" + urllib.parse.urlencode(params) if params else "")
        req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "aisites-verify"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    return fetch


def local_fetch(site: Any) -> Fetch:
    """Answer the two routes the verifier reads straight from a site folder, as the web door would."""
    def fetch(path: str, params: dict) -> dict:
        if path == "/history/head":
            return json.loads(json.dumps(site.head_view()))
        if path == "/history":
            after, limit = int(params.get("after", 0)), int(params.get("limit", 100))
            entries = site.history.entries(after, limit + 1)
            out: dict = {"entries": entries[:limit]}
            if len(entries) > limit:
                out["next_after"] = entries[limit - 1]["seq"]
            return json.loads(json.dumps(out))
        raise ValueError(f"the local verifier does not serve {path}")
    return fetch
