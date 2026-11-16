"""Measure what it costs an assistant to get an answer: requests, bytes, and rough tokens.

    python tools/measure.py            # a table
    python tools/measure.py --json     # the same numbers as JSON

Three example sites are built in a temporary folder with a fixed clock, and the same questions are put to
each in four ways:

  before   the path an assistant had to take before questions existed: the old first response, then the
           raw collections, working the answer out itself
  level 1  an assistant that can only follow links it has been shown
  level 2  an assistant that can fill in an address
  level 3  an assistant that combines: asks by kind without reading the list, with lean output

"Before" uses the old first response's size as recorded at commit 09130a1 (it no longer exists) plus the
follow-up reads, which are unchanged. Tokens are estimated as bytes / 4.

This counts requests and bytes. It does not show whether a model answers correctly.
"""

import json
import pathlib
import sys
import tempfile
from datetime import datetime, timezone

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402

from aisites import ops  # noqa: E402
from aisites.api import create_app  # noqa: E402
from aisites.mcp_door import OTHER, tools_for  # noqa: E402
from examples import game_catalogue, pharmacy, school  # noqa: E402

BASE = "https://site.example"
OLD_FIRST_RESPONSE = {"school": 2368, "pharmacy": 2028, "games": 1541}   # bytes, at commit 09130a1
OLD_TOOL_LIST = 16127


def clock():
    return datetime(2026, 11, 16, 10, 0, tzinfo=timezone.utc)   # a Monday; 11:00 in Madrid


class Visitor:
    def __init__(self, site):
        self.http = TestClient(create_app(site), base_url=BASE)
        self.steps = []

    def get(self, path):
        r = self.http.get(path)
        assert r.status_code == 200, (path, r.status_code, r.text[:200])
        self.steps.append((path.replace(BASE, ""), len(r.content)))
        return r.json()

    def cost(self, first=0):
        size = first + sum(n for _, n in self.steps)
        return {"requests": len(self.steps) + (1 if first else 0), "bytes": size, "tokens": round(size / 4),
                "steps": ([("(old first response)", first)] if first else []) + self.steps}


def school_open_tomorrow(site):
    out = {}
    v = Visitor(site); v.get("/hours?date=tomorrow"); out["before"] = v.cost(OLD_FIRST_RESPONSE["school"])
    v = Visitor(site); front = v.get("/")
    v.get(next(q for q in front["ask"] if q.get("kind") == "Hours")["try"][0]); out["level 1"] = v.cost()
    v = Visitor(site); front = v.get("/")
    v.get(next(q for q in front["ask"] if q.get("kind") == "Hours")["url"] + "?date=tomorrow"); out["level 2"] = v.cost()
    v = Visitor(site); v.get("/ask/Hours?date=tomorrow&links=0"); out["level 3"] = v.cost()
    return out


def pharmacy_ibuprofen(site):
    out = {}
    v = Visitor(site)
    v.get("/collections/products"); v.get("/collections/stock?product=ibuprofen-200&stock.gt=0")
    v.get("/collections/places/norte"); v.get("/hours/norte")
    out["before"] = v.cost(OLD_FIRST_RESPONSE["pharmacy"])
    v = Visitor(site); front = v.get("/")
    options = v.get(next(q for q in front["ask"] if q.get("kind") == "Offer")["url"])
    v.get(next(o for o in options["options"] if "Ibuprofeno" in o["label"])["url"]); out["level 1"] = v.cost()
    v = Visitor(site); front = v.get("/")
    v.get(next(q for q in front["ask"] if q.get("kind") == "Offer")["url"] + "?q=ibuprofen&stock.gt=0")
    out["level 2"] = v.cost()
    v = Visitor(site); v.get("/ask/Offer?q=ibuprofen&stock.gt=0&links=0"); out["level 3"] = v.cost()
    return out


def game_price_in_mexico(site):
    out = {}
    v = Visitor(site); v.get("/collections/offers?product=GAME-1&region=MX")
    out["before"] = v.cost(OLD_FIRST_RESPONSE["games"])
    v = Visitor(site); front = v.get("/")
    options = v.get(next(q for q in front["ask"] if q.get("kind") == "Offer")["url"])
    v.get(options["options"][0]["url"]); out["level 1"] = v.cost()
    v = Visitor(site); front = v.get("/")
    v.get(next(q for q in front["ask"] if q.get("kind") == "Offer")["url"] + "?product=GAME-1&region=MX")
    out["level 2"] = v.cost()
    v = Visitor(site); v.get("/ask/Offer?product=GAME-1&region=MX&format=table&links=0"); out["level 3"] = v.cost()
    return out


def measure():
    tmp = pathlib.Path(tempfile.mkdtemp())
    sites = {name: module.build(tmp / name, clock)[0]
             for name, module in (("school", school), ("pharmacy", pharmacy), ("games", game_catalogue))}
    results = {
        "questions": {
            "school: is it open tomorrow?": school_open_tomorrow(sites["school"]),
            "pharmacy: where is ibuprofen in stock, and is that branch open?": pharmacy_ibuprofen(sites["pharmacy"]),
            "games: what does GAME-1 cost in Mexico?": game_price_in_mexico(sites["games"]),
        },
        "first response": {name: {"before": OLD_FIRST_RESPONSE[name],
                                  "now": len(TestClient(create_app(site), base_url=BASE).get("/").content)}
                           for name, site in sites.items()},
    }
    # Asking again when nothing has changed.
    v = Visitor(sites["pharmacy"])
    tag = v.get("/ask/Offer?q=ibuprofen&stock.gt=0&links=0")["tag"]
    full = v.steps[-1][1]
    v.get(f"/ask/Offer?q=ibuprofen&stock.gt=0&links=0&unless={tag}")
    results["asking again, nothing changed"] = {"full answer": full, "with unless=<tag>": v.steps[-1][1]}
    # What the owner's or a staff member's AI must load before working through MCP.
    games = sites["games"]
    games.set_role("pricing", [{"target": "games", "ops": ["get", "list", "change", "schedule"], "fields": ["price"]}])
    key = games.resolve(games.issue_key("pricing")["secret"])

    def tools(site, principal):
        chosen = [{"name": o.name, "description": o.summary, "inputSchema": o.schema()}
                  for o in tools_for(site, principal)]
        if principal.kind != "role" and len(chosen) < len(ops.OPS):
            chosen.append({"name": OTHER.name, "description": OTHER.description, "inputSchema": OTHER.input_schema})
        return {"tools": len(chosen), "bytes": len(json.dumps(chosen))}

    role = tools(games, key)   # measured before the role exists for the owner's own count below
    results["mcp tool list"] = {
        "before (everyone, every operation)": {"tools": 34, "bytes": OLD_TOOL_LIST},
        "every operation now": {"tools": len(ops.OPS), "bytes": len(json.dumps([
            {"name": o.name, "description": o.summary, "inputSchema": o.schema()} for o in ops.OPS.values()]))},
        "owner of the pharmacy site (hours, no bookings, forms, trees or roles)": tools(sites["pharmacy"], ops.LOCAL),
        "owner of the game catalogue (a tree and a role)": tools(games, ops.LOCAL),
        "a pricing role": role}
    results["describe the whole site (pharmacy)"] = {
        "full": len(json.dumps(sites["pharmacy"].describe(full=True))),
        "short (the default now)": len(json.dumps(sites["pharmacy"].describe()))}
    for site in sites.values():
        site.close()
    return results


def report(results):
    lines = ["| Question | Path | Requests | Bytes | Tokens (about) |", "|---|---|---|---|---|"]
    for question, paths in results["questions"].items():
        for path, cost in paths.items():
            lines.append(f"| {question} | {path} | {cost['requests']} | {cost['bytes']:,} | {cost['tokens']:,} |")
    lines += ["", "| First response | Before | Now |", "|---|---|---|"]
    lines += [f"| {name} | {v['before']:,} bytes | {v['now']:,} bytes |" for name, v in results["first response"].items()]
    again = results["asking again, nothing changed"]
    lines += ["", f"Asking again when nothing changed: {again['full answer']:,} bytes in full, "
                  f"{again['with unless=<tag>']:,} bytes with unless=<tag>.", ""]
    lines += ["| MCP tool list | Tools | Bytes | Tokens (about) |", "|---|---|---|---|"]
    lines += [f"| {who} | {v['tools']} | {v['bytes']:,} | {round(v['bytes'] / 4):,} |"
              for who, v in results["mcp tool list"].items()]
    described = results["describe the whole site (pharmacy)"]
    lines += ["", f"Describing the whole site through MCP: {described['full']:,} bytes in full, "
                  f"{described['short (the default now)']:,} bytes in the short form that is now the default."]
    return "\n".join(lines)


if __name__ == "__main__":
    measured = measure()
    print(json.dumps(measured, indent=1) if "--json" in sys.argv else report(measured))
