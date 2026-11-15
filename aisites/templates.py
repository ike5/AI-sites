"""Site templates: ready-made starting points for a new site.

A template is data: a name, a title, a description and an ordered list of steps. A step is
`[operation name, arguments]` with plain JSON values, and `apply` runs each one through the same
operation table a site owner uses (`ops.run` as LOCAL), so a template can do nothing the owner could
not do by hand. Templates never issue keys, change site settings or run maintenance operations.

Placeholders. Sample content must not go stale, so a template holds no calendar dates. Inside any
string in a step's arguments, `{date}` is replaced by today's date and `{date+N}` by the date N days
ahead (N a whole number), both as YYYY-MM-DD in the site's time zone, e.g. `"{date+1}T19:00"`.
Any other `{...}` is an error, so a typo is never left in a site.
"""

from __future__ import annotations

import re
from datetime import timedelta
from typing import Any

from . import ops
from .errors import SiteError

_PLACEHOLDER = re.compile(r"\{([^{}]*)\}")
_DATE = re.compile(r"date(?:\+(\d+))?")


def _resolve(value: Any, today) -> Any:
    """Copy of a step's arguments with every placeholder replaced."""
    if isinstance(value, str):
        def sub(m: re.Match) -> str:
            d = _DATE.fullmatch(m.group(1))
            if not d:
                raise SiteError("bad_placeholder", f"Unknown placeholder {m.group(0)} in a template.",
                                "Use {date} for today or {date+N} for N days ahead.")
            return (today + timedelta(days=int(d.group(1) or 0))).isoformat()
        return _PLACEHOLDER.sub(sub, value)
    if isinstance(value, list):
        return [_resolve(v, today) for v in value]
    if isinstance(value, dict):
        return {k: _resolve(v, today) for k, v in value.items()}
    return value


def _step(op: str, **args: Any) -> list:
    return [op, args]


_WEEKDAYS = ("mon", "tue", "wed", "thu", "fri")


def _sittings() -> list:
    """A few tables for tonight and tomorrow: two sittings, two table sizes."""
    steps = []
    for day in ("{date}", "{date+1}"):
        for at in ("18:00", "19:30"):
            for seats, count in ((2, 4), (4, 2)):
                steps.append(_step("add", target="tables", data={
                    "id": f"{day}-{at.replace(':', '')}-{seats}", "name": f"Table for {seats}, {at}",
                    "start": f"{day}T{at}", "place": "main", "seats": seats,
                    "capacity": count, "remaining": count}))
    return steps


TEMPLATES: dict = {
    "blank": {
        "name": "blank", "title": "Blank site",
        "description": "An empty site. You or your AI add whatever it needs.",
        "good_for": "anything not covered by the other templates",
        "steps": [],
    },
    "school": {
        "name": "school", "title": "School or office hours",
        "description": "Publishes opening hours, closures and notices, so a visitor's AI can answer "
                       "\"is it open tomorrow?\". Comes with a sample school day and one sample closure.",
        "good_for": "schools, offices, clinics",
        "steps": [
            _step("define", target="places", kind="Place"),
            _step("define", target="hours", kind="Hours", refs={"place": "places"}),
            _step("define", target="closures", kind="Closure", refs={"hours": "hours"}),
            _step("define", target="notices", kind="Notice"),
            _step("define", target="pages", kind="Page"),
            _step("add", target="places", data={"id": "main", "name": "Main site (sample)",
                                                "address": "1 Sample Street"}),
            _step("add", target="hours", data={"id": "main", "name": "Opening hours (sample)", "place": "main",
                                               "weekly": {d: [["08:00", "15:00"]] for d in _WEEKDAYS}}),
            _step("add", target="closures", data={"id": "sample-closure", "hours": "main",
                                                  "start_date": "{date+3}",
                                                  "reason": "Sample closure: staff training day"}),
            _step("add", target="notices", data={"id": "welcome", "title": "Welcome (sample notice)",
                                                 "text": "Replace this with a real announcement.",
                                                 "valid_until": "{date+14}T00:00"}),
            _step("add", target="pages", data={"slug": "about", "title": "About (sample page)",
                                               "body": "Replace this with a few lines about who you are."}),
        ],
    },
    "notice_board": {
        "name": "notice_board", "title": "Notice board",
        "description": "Says where you are and what is on today, with notices that expire by themselves, "
                       "plus a short menu or price list.",
        "good_for": "food carts, market stalls, pop-ups",
        "steps": [
            _step("define", target="places", kind="Place"),
            _step("define", target="notices", kind="Notice", refs={"place": "places"}),
            _step("define", target="menu", kind="Product"),
            _step("add", target="places", data={"id": "spot", "name": "Today's spot (sample)",
                                                "address": "Corner of Sample Street"}),
            _step("add", target="menu", data={"sku": "item-1", "name": "Sample item", "price": "5.00"}),
            _step("add", target="menu", data={"sku": "item-2", "name": "Another sample item", "price": "7.50"}),
            _step("add", target="notices", data={"id": "location", "title": "We are at today's spot (sample)",
                                                 "place": "spot", "text": "Replace with where and when.",
                                                 "valid_until": "{date+1}T23:00"}),
            _step("add", target="notices", data={"id": "special", "title": "Sample special offer",
                                                 "text": "Replace with this week's offer.",
                                                 "valid_until": "{date+3}T23:00"}),
        ],
    },
    "shop": {
        "name": "shop", "title": "Shop catalogue",
        "description": "A product catalogue sorted into categories, with prices and stock levels an AI can "
                       "check before recommending anything.",
        "good_for": "small shops, makers, publishers",
        "steps": [
            _step("define", target="categories", type="tree"),
            _step("define", target="products", kind="Product", refs={"category": "categories"}),
            _step("add", target="categories", data={"id": "featured", "label": "Featured (sample)"}),
            _step("add", target="categories", data={"id": "everyday", "label": "Everyday (sample)"}),
            _step("add", target="categories", data={"id": "gifts", "label": "Gifts (sample)"},
                  parent="everyday"),
            _step("add", target="products", data={"sku": "SAMPLE-1", "name": "Sample product one",
                                                  "price": "19.00", "stock": 25, "category": "featured"}),
            _step("add", target="products", data={"sku": "SAMPLE-2", "name": "Sample product two",
                                                  "price": "9.50", "stock": 100, "category": "everyday"}),
            _step("add", target="products", data={"sku": "SAMPLE-3", "name": "Sample product three",
                                                  "price": "34.00", "stock": 8, "category": "gifts"}),
        ],
    },
    "restaurant": {
        "name": "restaurant", "title": "Restaurant with bookable tables",
        "description": "Lets any visiting AI reserve and book a table, with opening hours alongside. "
                       "Comes with sample sittings for today and tomorrow.",
        "good_for": "restaurants, cafes, anything with a limited number of seats",
        "steps": [
            _step("define", target="places", kind="Place"),
            _step("define", target="tables", kind="Slot", refs={"place": "places"},
                  description="Tables by sitting. `remaining` is how many tables of that size are free.",
                  fields=["seats:integer"],
                  settings={"booking": {"field": "remaining", "open": True, "hold_minutes": 15,
                                        "max_active_per_caller": 2,
                                        "details": ["name!:string", "party_size!:integer"]}}),
            _step("define", target="hours", kind="Hours", refs={"place": "places"}),
            _step("add", target="places", data={"id": "main", "name": "Our restaurant (sample)",
                                                "address": "1 Sample Street"}),
            _step("add", target="hours", data={"id": "main", "name": "Opening hours (sample)", "place": "main",
                                               "weekly": {d: [["17:00", "22:00"]]
                                                          for d in ("tue", "wed", "thu", "fri", "sat", "sun")}}),
            *_sittings(),
        ],
    },
    "contact": {
        "name": "contact", "title": "Contact form",
        "description": "A form that visitors' AIs can send messages through, and an inbox role for answering "
                       "them. Keys are issued separately, so nobody can write until you hand one out.",
        "good_for": "bakeries, freelancers, any small business taking questions",
        "steps": [
            _step("define", target="contact", type="form", description="Send us a message.",
                  fields=[{"name": "message", "type": "text", "required": True}],
                  settings={"form": {"purposes": ["Question", "Order", "Other"], "text_field": "message",
                                     "text_max": 600, "per_sender_per_day": 3, "per_day": 200}}),
            _step("set_role", role="customers", grants=[{"target": "contact", "ops": ["submit", "ticket"]}],
                  description="Customers' assistants: send a message and check for the reply."),
            _step("set_role", role="inbox",
                  grants=[{"target": "contact", "ops": ["inbox", "read_message", "reply"]}],
                  description="The owner's assistant: read and answer messages, nothing else."),
        ],
    },
    "calendar": {
        "name": "calendar", "title": "Private calendar",
        "description": "A calendar only you and your AI can see. It starts empty.",
        "good_for": "personal schedules, private planning",
        "steps": [_step("define", target="calendar", kind="Event", access="private")],
    },
}


def _structures(template: dict) -> list:
    return [args["target"] for op, args in template["steps"] if op == "define"]


def describe() -> list:
    """What each template is for, for someone (or an AI) choosing one. `blank` comes first."""
    return [{"name": t["name"], "title": t["title"], "description": t["description"],
             "structures": _structures(t), "good_for": t["good_for"]}
            for t in TEMPLATES.values()]


def apply(site, name: str) -> dict:
    """Apply a template to a new site, all or nothing."""
    template = TEMPLATES.get(name)
    if template is None:
        raise SiteError("bad_template", f"'{name}' is not a template.",
                        "Available templates: " + ", ".join(TEMPLATES) + ".", status=404)
    if site._defs():
        raise SiteError("not_empty", "This site already has content, and templates only start new sites.",
                        "Add to it with the operations (`define`, `add`), or create a new site from a template.",
                        status=409)
    today = site.now().astimezone(site.tz).date()
    with site.store.tx():
        for op, args in template["steps"]:
            ops.run(site, ops.LOCAL, op, _resolve(args, today))
    return {"template": name, "structures": _structures(template)}
