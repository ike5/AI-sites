"""The command-line door. Every operation in `ops.OPS` is a subcommand with the same arguments.

    aisites site create ./mysite --name "Corner Bikes" --timezone America/Los_Angeles
    aisites define products --kind Product -s ./mysite
    aisites add products sku=BIKE-1 name="Road bike" price=499.00 stock=3
    aisites change products BIKE-1 price=549.00
    aisites schedule products BIKE-1 price=449.00 --on 2026-12-15
    aisites serve ./mysite

Field values are `name=value`; use `name:=<json>` for lists, objects, numbers that must stay numbers, or
null. The site folder comes from -s/--site, then $AISITES_SITE, then the current folder.
"""

import argparse
import json
import os
import sys
from importlib import resources
from typing import Any, Callable, Optional

from . import ops
from .errors import SiteError
from .site import Site

# Each operation says how it reads on the command line (`Op.positional`, `Op.pairs`, `Op.flags`), so a
# new operation needs no change here. This table is a snapshot kept for callers that want an overview.
POSITIONAL = {name: list(op.positional) for name, op in ops.OPS.items()}


def _pairs(tokens: list) -> dict:
    out: dict = {}
    for token in tokens:
        if ":=" in token and (("=" not in token) or token.index(":=") < token.index("=")):
            name, raw = token.split(":=", 1)
            try:
                out[name] = json.loads(raw)
            except ValueError:
                raise SiteError("invalid", f"'{raw}' after {name}:= is not valid JSON.")
        elif "=" in token:
            name, value = token.split("=", 1)
            out[name] = value
        else:
            raise SiteError("invalid", f"'{token}' is not a name=value pair.",
                            "Write field=value, or field:=<json> for lists, objects and null.")
    return out


def _grants(items: list) -> list:
    """Each --grant is one JSON grant object, or a JSON array of them."""
    out: list = []
    for item in items:
        try:
            value = json.loads(item)
        except ValueError:
            raise SiteError("invalid", f"'{item}' after --grant is not valid JSON.",
                            '--grant \'{"target": "games", "ops": ["change"], "fields": ["price"]}\'')
        out += value if isinstance(value, list) else [value]
    return out


def _op_parser(op: ops.Op) -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog=f"aisites {op.name}", description=op.summary)
    p.add_argument("-s", "--site", help="site folder (default: $AISITES_SITE or the current folder)")
    positional = list(op.positional)
    for name in positional:
        if name.endswith("?"):
            p.add_argument(name[:-1], nargs="?")
        else:
            p.add_argument(name)
    taken = {n.rstrip("?") for n in positional}
    if op.pairs:
        p.add_argument("pairs", nargs="*", metavar="name=value")
        p.add_argument("--json", help="the name=value pairs as one JSON object instead")
        if op.pairs != "*":
            taken.add(op.pairs)
    for name, spec in op.params.items():
        if name in taken or op.pairs == "*":
            continue
        flag = op.flags.get(name, "--" + name)
        kind = spec.get("type")
        desc = spec.get("description", "")
        if kind == "boolean":
            p.add_argument(flag, dest=name, action="store_true", default=None, help=desc)
        elif kind == "integer":
            p.add_argument(flag, dest=name, type=int, help=desc)
        elif kind == "array":
            p.add_argument(flag, dest=name, action="append", help=desc + " (repeatable)")
        elif kind == "object":
            p.add_argument(flag, dest=name, action="append", metavar="name=value", help=desc + " (repeatable)")
        else:
            p.add_argument(flag, dest=name, help=desc)
    return p


def _op_args(op: ops.Op, argv: list) -> tuple:
    ns = vars(_op_parser(op).parse_args(argv))
    site_dir = ns.pop("site", None)
    pairs = ns.pop("pairs", None) or []
    raw_json = ns.pop("json", None)
    args: dict = {}
    for name, value in ns.items():
        if value is None:
            continue
        kind = op.params.get(name, {}).get("type")
        if kind == "array" and name == "grants":
            value = _grants(value)
        elif kind == "array":
            value = [part for item in value for part in (item.split(",") if name in ("fields", "remove")
                                                         and op.name != "define" else [item])]
        elif kind == "object":
            value = _pairs(value)
        elif kind == "integer" and not isinstance(value, int):
            try:  # a positional argument arrives as text
                value = int(value)
            except ValueError:
                raise SiteError("invalid", f"`{name}` must be a whole number.")
        args[name] = value
    if op.pairs:
        try:
            given = json.loads(raw_json) if raw_json else _pairs(pairs)
        except ValueError:
            raise SiteError("invalid", "--json is not valid JSON.")
        if op.pairs == "*":
            args.update(given)
        elif given or op.pairs in op.required:
            args[op.pairs] = given
    return site_dir, args


def _site_dir(given: Optional[str]) -> str:
    return given or os.environ.get("AISITES_SITE") or "."


def _emit(obj: Any, out: Any) -> None:
    print(json.dumps(obj, indent=2, ensure_ascii=False), file=out)


def guide_text() -> str:
    return resources.files("aisites").joinpath("authoring.md").read_text(encoding="utf-8")


def main(argv: Optional[list] = None, clock: Optional[Callable] = None, out: Any = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    out = out or sys.stdout
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(__doc__, file=out)
        print("Operations: " + ", ".join(ops.OPS), file=out)
        print("Other commands: site create, site key, serve, mcp, ops, guide, verify, anchors", file=out)
        return 0
    command, rest = argv[0], argv[1:]
    try:
        if command in ops.OPS:
            site_dir, args = _op_args(ops.OPS[command], rest)
            site = Site(_site_dir(site_dir), clock)
            try:
                _emit(ops.run(site, ops.LOCAL, command, args), out)
            finally:
                site.close()
            return 0
        if command == "site":
            return _site_command(rest, clock, out)
        if command == "serve":
            return _serve(rest)
        if command == "mcp":
            import anyio
            from .mcp_door import run_stdio
            p = argparse.ArgumentParser(prog="aisites mcp", description="Serve the MCP door over stdio.")
            p.add_argument("site", nargs="?")
            anyio.run(run_stdio, Site(_site_dir(p.parse_args(rest).site), clock))
            return 0
        if command == "ops":
            _emit(ops.describe_ops(), out)
            return 0
        if command == "guide":
            print(guide_text(), file=out)
            return 0
        if command == "verify":
            return _verify(rest, clock, out)
        if command == "anchors":
            return _anchors(rest, clock, out)
        raise SiteError("unknown_command", f"'{command}' is not a command.",
                        "Run `aisites help` for the list.")
    except SiteError as e:
        _emit(e.as_dict(), sys.stderr)
        return 1


def _site_command(argv: list, clock: Optional[Callable], out: Any) -> int:
    p = argparse.ArgumentParser(prog="aisites site")
    sub = p.add_subparsers(dest="action", required=True)
    c = sub.add_parser("create", help="create a new site folder and print its owner key once")
    c.add_argument("directory")
    c.add_argument("--name", required=True)
    c.add_argument("--description", default="")
    c.add_argument("--timezone", default="UTC", help="region name, e.g. America/Los_Angeles")
    c.add_argument("--language", default="en")
    c.add_argument("--currency", default="")
    k = sub.add_parser("key", help="issue a new owner key; the old one stops working")
    k.add_argument("directory", nargs="?")
    ns = p.parse_args(argv)
    if ns.action == "create":
        site, owner_key = Site.create(ns.directory, ns.name, ns.description, ns.timezone, ns.language,
                                      ns.currency, clock)
        _emit({"created": str(site.dir), "site": site.config, "owner_key": owner_key,
               "note": "Keep the owner key safe; it is not stored and cannot be shown again."}, out)
        site.close()
    else:
        site = Site(_site_dir(ns.directory), clock)
        _emit({"owner_key": site.new_owner_key(),
               "note": "The previous owner key no longer works."}, out)
        site.close()
    return 0


# ------------------------------------------------------------ verification and anchors (Milestone 2)

def _verify(argv: list, clock: Optional[Callable], out: Any) -> int:
    from .verify import local_fetch, url_fetch, verify_site

    p = argparse.ArgumentParser(prog="aisites verify",
                                description="Check a site's history chain and signed head. Exits 1 on any problem.")
    p.add_argument("target", help="the site's URL, or a site folder")
    p.add_argument("--pin", help="JSON file holding the head from an earlier run; updated when the check passes")
    ns = p.parse_args(argv)
    pin = None
    if ns.pin and os.path.exists(ns.pin):
        try:
            with open(ns.pin, encoding="utf-8") as f:
                pin = json.load(f)
        except ValueError:
            raise SiteError("invalid", f"The pin file '{ns.pin}' is not valid JSON.",
                            "Delete it to start a new pin from this run.")
    if ns.target.startswith(("http://", "https://")):
        report = verify_site(url_fetch(ns.target), pin)
    else:
        site = Site(ns.target, clock)
        try:
            report = verify_site(local_fetch(site), pin)
        finally:
            site.close()
    _emit(report, out)
    if report["ok"] and ns.pin:
        with open(ns.pin, "w", encoding="utf-8") as f:
            json.dump(report["pin"], f)
    return 0 if report["ok"] else 1


def _anchors(argv: list, clock: Optional[Callable], out: Any) -> int:
    p = argparse.ArgumentParser(prog="aisites anchors",
                                description="Show or set where `checkpoint` sends signed heads.")
    p.add_argument("-s", "--site", help="site folder (default: $AISITES_SITE or the current folder)")
    p.add_argument("--add-file", metavar="PATH", action="append",
                   help="append checkpoints to this file, ideally outside the site's control (repeatable)")
    p.add_argument("--clear", action="store_true", help="remove all stored anchors first")
    ns = p.parse_args(argv)
    site = Site(_site_dir(ns.site), clock)
    try:
        items = [] if ns.clear else site.anchor_config()
        items += [{"type": "file", "path": os.path.abspath(path)} for path in ns.add_file or []]
        if ns.clear or ns.add_file:
            site.set_anchor_config(items)
        _emit({"anchors": site.anchor_config()}, out)
    finally:
        site.close()
    return 0


def _serve(argv: list) -> int:
    import uvicorn

    p = argparse.ArgumentParser(prog="aisites serve", description="Serve one site, or a folder of sites.")
    p.add_argument("site", nargs="?", help="site folder")
    p.add_argument("--sites", help="folder containing several site folders, served at /s/<name>/")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8000)
    ns = p.parse_args(argv)
    if ns.sites:
        from .host import Host
        app: Any = Host(ns.sites)
    else:
        from .api import create_app
        app = create_app(Site(_site_dir(ns.site)))
    uvicorn.run(app, host=ns.host, port=ns.port, log_level="info")
    return 0


if __name__ == "__main__":
    sys.exit(main())
