"""Built-in kinds: ready-made field sets whose meanings follow schema.org.

Two sites that both use `Product` mean the same thing by `price`, so an agent can compare them without
guessing. Authors add their own fields on top, or skip kinds entirely for custom structures.
"""

from typing import Any, Callable, Dict

from . import timeview
from .errors import SiteError


def _f(name, type="string", policy="logged", required=False, many=False, semantic=None, description="",
       default=None):
    out = {"name": name, "type": type, "policy": policy, "required": required, "many": many,
           "description": description}
    if semantic:
        out["semantic"] = semantic
    if default is not None:
        out["default"] = default
    return out


_VALID = [
    _f("valid_from", "datetime", semantic="schema:validFrom",
       description="The record is hidden from visitors before this time."),
    _f("valid_until", "datetime", semantic="schema:validThrough",
       description="The record drops out of visitor reads from this time, so stale information expires."),
]

KINDS: Dict[str, dict] = {
    "Business": {
        "semantic": "schema:LocalBusiness", "key": "id",
        "description": "Who runs this site and how to reach them.",
        "fields": [
            _f("name", required=True, semantic="schema:name"),
            _f("description", "text", semantic="schema:description"),
            _f("telephone", semantic="schema:telephone"),
            _f("email", semantic="schema:email"),
            _f("url", semantic="schema:url"),
            _f("place", semantic="schema:location", description="Key of a place record."),
        ],
    },
    "Place": {
        "semantic": "schema:Place", "key": "id",
        "description": "A physical location such as a branch, campus or street corner.",
        "fields": [
            _f("name", required=True, semantic="schema:name"),
            _f("aliases", many=True, semantic="schema:alternateName",
               description="Other names people use for it, so a search finds it."),
            _f("address", semantic="schema:streetAddress"),
            _f("city", semantic="schema:addressLocality"),
            _f("region", semantic="schema:addressRegion"),
            _f("country", semantic="schema:addressCountry", description="ISO 3166 code, e.g. MX."),
            _f("postal_code", semantic="schema:postalCode"),
            _f("latitude", "number", semantic="schema:latitude"),
            _f("longitude", "number", semantic="schema:longitude"),
            _f("notes", "text"),
        ],
    },
    "Hours": {
        "semantic": "schema:OpeningHoursSpecification", "key": "id",
        "description": "A weekly opening pattern. Dated exceptions go in a Closure collection.",
        "fields": [
            _f("name", semantic="schema:name"),
            _f("place", semantic="schema:location", description="Key of a place record."),
            _f("weekly", "json", required=True, semantic="schema:openingHoursSpecification",
               description='Weekday to [open, close] pairs in 24-hour local time, e.g. '
                           '{"mon": [["08:00", "15:00"]]}. A missing day is closed.'),
            _f("timezone", description="Region zone name; defaults to the site's."),
        ],
    },
    "Closure": {
        "semantic": "schema:OpeningHoursSpecification", "key": "id",
        "description": "A dated exception to opening hours: a closure, or special hours on those dates.",
        "fields": [
            _f("hours", description="Key of the hours record this applies to; omit to apply to all."),
            _f("start_date", "date", required=True, semantic="schema:validFrom"),
            _f("end_date", "date", semantic="schema:validThrough",
               description="Last day, inclusive; defaults to start_date."),
            _f("closed", "boolean", default=True, description="False means special hours in `intervals`."),
            _f("intervals", "json", description='Special [open, close] pairs when not closed.'),
            _f("reason", semantic="schema:description"),
        ],
    },
    "Notice": {
        "semantic": "schema:SpecialAnnouncement", "key": "id",
        "description": "A dated announcement such as a temporary location or a promotion.",
        "fields": [
            _f("title", required=True, semantic="schema:name"),
            _f("text", "text", semantic="schema:text"),
            _f("place", semantic="schema:spatialCoverage", description="Key of a place record."),
            _f("starts", "datetime", semantic="schema:startDate", description="When the thing announced begins."),
            _f("ends", "datetime", semantic="schema:endDate", description="When the thing announced ends."),
            _f("timezone", description="Region zone name; defaults to the site's."),
        ] + _VALID,
    },
    "Page": {
        "semantic": "schema:WebPage", "key": "slug",
        "description": "Prose such as policies or an about page.",
        "fields": [
            _f("slug", semantic="schema:identifier"),
            _f("title", required=True, semantic="schema:name"),
            _f("body", "text", required=True, semantic="schema:text"),
        ],
    },
    "Product": {
        "semantic": "schema:Product", "key": "sku",
        "description": "Something offered for sale, with a default price.",
        "fields": [
            _f("sku", semantic="schema:sku"),
            _f("name", required=True, semantic="schema:name"),
            _f("aliases", many=True, semantic="schema:alternateName",
               description="Other names people use for it (brand, common name, another language), so a search "
                           "finds it."),
            _f("description", "text", semantic="schema:description"),
            _f("price", "decimal", semantic="schema:price"),
            _f("price_currency", semantic="schema:priceCurrency", description="ISO 4217 code, e.g. USD."),
            _f("stock", "integer", policy="capacity", semantic="schema:inventoryLevel"),
            _f("category", semantic="schema:category"),
        ],
    },
    "Offer": {
        "semantic": "schema:Offer", "key": "id",
        "description": "A price or stock level for one product in one region or at one place.",
        "fields": [
            _f("product", required=True, semantic="schema:itemOffered", description="Key of a product record."),
            _f("region", semantic="schema:eligibleRegion", description="ISO 3166 code, e.g. MX."),
            _f("place", semantic="schema:availableAtOrFrom", description="Key of a place record."),
            _f("price", "decimal", semantic="schema:price"),
            _f("price_currency", semantic="schema:priceCurrency", description="ISO 4217 code, e.g. MXN."),
            _f("stock", "integer", policy="capacity", semantic="schema:inventoryLevel"),
            _f("timezone", description="Region zone name; defaults to the site's."),
        ] + _VALID,
    },
    "Slot": {
        "semantic": "schema:Event", "key": "id",
        "description": "A bookable time slot with limited capacity.",
        "fields": [
            _f("name", required=True, semantic="schema:name"),
            _f("start", "datetime", required=True, semantic="schema:startDate"),
            _f("end", "datetime", semantic="schema:endDate"),
            _f("timezone", description="Region zone name; defaults to the site's."),
            _f("place", semantic="schema:location", description="Key of a place record."),
            _f("capacity", "integer", semantic="schema:maximumAttendeeCapacity"),
            _f("remaining", "integer", policy="capacity", semantic="schema:remainingAttendeeCapacity"),
        ],
    },
    "Event": {
        "semantic": "schema:Event", "key": "id",
        "description": "A calendar entry. Usually kept in a private collection.",
        "fields": [
            _f("title", required=True, semantic="schema:name"),
            _f("start", "datetime", required=True, semantic="schema:startDate"),
            _f("end", "datetime", semantic="schema:endDate"),
            _f("timezone", description="Region zone name such as America/Los_Angeles; defaults to the site's."),
            _f("where", semantic="schema:location"),
            _f("with", many=True, semantic="schema:attendee"),
            _f("notes", "text", semantic="schema:description"),
        ],
    },
}


def _check_hours(data: dict) -> None:
    if "weekly" in data:
        data["weekly"] = timeview.check_weekly(data["weekly"])
    if data.get("timezone"):
        timeview.zone(data["timezone"])


def _check_closure(data: dict) -> None:
    if data.get("end_date") and data["end_date"] < data["start_date"]:
        raise SiteError("invalid", "end_date is before start_date.")
    if not data.get("closed", True):
        data["intervals"] = timeview.check_intervals(data.get("intervals"), "intervals")


def _check_span(start: str, end: str) -> Callable[[dict], None]:
    def check(data: dict) -> None:
        if data.get("timezone"):
            timeview.zone(data["timezone"])
        if data.get(start) and data.get(end) and data[end] <= data[start]:
            raise SiteError("invalid", f"{end} must be after {start}.")
    return check


# Extra checks a kind applies to a record after ordinary field validation.
VALIDATORS: Dict[str, Callable[[dict], Any]] = {
    "Hours": _check_hours,
    "Closure": _check_closure,
    "Slot": _check_span("start", "end"),
    "Event": _check_span("start", "end"),
    "Notice": _check_span("valid_from", "valid_until"),
    "Offer": _check_span("valid_from", "valid_until"),
}


def describe() -> list:
    return [{"kind": name, "semantic": k["semantic"], "description": k["description"], "key": k["key"],
             "fields": [f["name"] + ("!" if f["required"] else "") + ":" + f["type"]
                        + ("[]" if f["many"] else "")
                        + ("" if f["policy"] == "logged" else ":" + f["policy"]) for f in k["fields"]]}
            for name, k in KINDS.items()]
