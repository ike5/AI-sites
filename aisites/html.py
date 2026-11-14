"""A plain human-readable rendering of any payload, generated from the same data agents read."""

from html import escape
from typing import Any

STYLE = """
:root { color-scheme: light dark; --fg: #1a1a1a; --bg: #fdfdfc; --muted: #666; --line: #ddd; --link: #0b57d0; }
@media (prefers-color-scheme: dark) { :root { --fg: #e8e8e6; --bg: #161616; --muted: #9a9a9a; --line: #333; --link: #8ab4f8; } }
body { font: 15px/1.5 system-ui, sans-serif; color: var(--fg); background: var(--bg); margin: 0; padding: 24px 16px; }
main { max-width: 960px; margin: 0 auto; }
h1 { font-size: 1.4rem; margin: 0 0 4px; }
p.note { color: var(--muted); margin: 0 0 20px; }
a { color: var(--link); }
dl { display: grid; grid-template-columns: max-content 1fr; gap: 4px 16px; margin: 0; }
dt { color: var(--muted); }
dd { margin: 0; min-width: 0; overflow-wrap: anywhere; }
.scroll { overflow-x: auto; }
table { border-collapse: collapse; width: 100%; overflow-wrap: normal; }
th, td { text-align: left; padding: 6px 10px; border-bottom: 1px solid var(--line); vertical-align: top; }
th { color: var(--muted); font-weight: 600; white-space: nowrap; }
ul { margin: 0; padding-left: 20px; }
"""


def _scalar(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, bool):
        return "yes" if v else "no"
    if isinstance(v, str) and v.startswith("/") and " " not in v:
        return f'<a href="{escape(v, quote=True)}">{escape(v)}</a>'
    return escape(str(v))


def _flatten(row: dict) -> dict:
    """Records keep their fields under `data`; lift them so a list of records makes one table."""
    if isinstance(row.get("data"), dict):
        return {**row["data"], **{k: v for k, v in row.items() if k not in ("data", "key")}}
    return row


def _value(v: Any) -> str:
    if isinstance(v, dict):
        return "<dl>" + "".join(
            f"<dt>{escape(str(k))}</dt><dd>"
            + ("<ul>" + "".join(_node(n) for n in x) + "</ul>" if k == "nodes" and isinstance(x, list) else _value(x))
            + "</dd>" for k, x in v.items()) + "</dl>"
    if isinstance(v, list):
        if not v:
            return ""
        if all(isinstance(x, dict) for x in v):
            if any("children" in x for x in v):
                return "<ul>" + "".join(_node(x) for x in v) + "</ul>"
            rows = [_flatten(x) for x in v]
            cols: list = []
            for r in rows:
                cols += [c for c in r if c not in cols]
            head = "".join(f"<th>{escape(str(c))}</th>" for c in cols)
            body = "".join("<tr>" + "".join(f"<td>{_value(r.get(c))}</td>" for c in cols) + "</tr>" for r in rows)
            return f'<div class="scroll"><table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>'
        if all(not isinstance(x, (dict, list)) for x in v):
            return ", ".join(_scalar(x) for x in v)
        return "<ul>" + "".join(f"<li>{_value(x)}</li>" for x in v) + "</ul>"
    return _scalar(v)


def _node(n: dict) -> str:
    extra = {k: v for k, v in n.items() if k not in ("id", "label", "children")}
    line = f"{escape(str(n.get('label', n.get('id', ''))))} <small>({escape(str(n.get('id', '')))})</small>"
    if extra:
        line += " " + escape(", ".join(f"{k}: {v}" for k, v in extra.items()))
    kids = n.get("children") or []
    return f"<li>{line}{'<ul>' + ''.join(_node(k) for k in kids) + '</ul>' if kids else ''}</li>"


def render(payload: Any, title: str, prefix: str = "") -> str:
    home = f'{prefix}/' if prefix else "/"
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
        f"<title>{escape(title)}</title>"
        f"<link rel='alternate' type='application/json' href='?format=json'>"
        f"<style>{STYLE}</style></head><body><main>"
        f"<h1>{escape(title)}</h1>"
        f"<p class='note'>Human view of data published for AI agents. "
        f"<a href='{escape(home, quote=True)}'>Site overview</a> · <a href='?format=json'>JSON</a></p>"
        f"{_value(payload)}</main></body></html>"
    )
