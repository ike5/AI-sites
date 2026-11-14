"""Structure definitions stored as data, and everything derived from them.

A structure is a collection (records sharing a schema) or a tree (nested nodes). Its definition is plain
JSON, so it can be created through any door, including by an AI, and hosted without running site code.
"""

import datetime as dt
import functools
import json
import re
from decimal import Decimal, InvalidOperation
from typing import Annotated, Any, List, Literal, Optional
from zoneinfo import ZoneInfo

from pydantic import BaseModel, BeforeValidator, ConfigDict, ValidationError, create_model, model_validator

from . import timeview
from .errors import SiteError

NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,62}$")
TYPES = ("string", "text", "integer", "number", "decimal", "boolean", "date", "datetime", "json")
POLICIES = ("logged", "live", "private", "capacity")
ACCESS = ("public", "invite", "private")
RESERVED = {"key", "data"}


def check_name(name: Any, what: str) -> str:
    if not isinstance(name, str) or not NAME_RE.match(name):
        raise SiteError("bad_name", f"'{name}' is not a valid {what} name.",
                        "Use lowercase letters, digits and underscores, starting with a letter.")
    return name


class FieldSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    type: Literal["string", "text", "integer", "number", "decimal", "boolean", "date", "datetime", "json"] = "string"
    policy: Literal["logged", "live", "private", "capacity"] = "logged"
    required: bool = False
    many: bool = False
    description: str = ""
    choices: Optional[dict] = None
    semantic: Optional[str] = None
    default: Any = None

    @model_validator(mode="after")
    def _check(self):
        check_name(self.name, "field")
        if self.name in RESERVED:
            raise ValueError(f"'{self.name}' is a reserved field name")
        if self.policy == "capacity" and (self.type != "integer" or self.many):
            raise ValueError("a capacity field must be a single integer")
        if self.choices is not None:
            if set(self.choices) not in ({"tree"}, {"collection"}):
                raise ValueError('choices must be {"tree": name} or {"collection": name}')
        return self


def parse_field(spec: Any) -> FieldSpec:
    """A field from a dict, or from shorthand `name[!]:type[[]][:policy]`, e.g. `stock:integer:live`."""
    try:
        if isinstance(spec, FieldSpec):
            return spec
        if isinstance(spec, dict):
            return FieldSpec(**spec)
        parts = str(spec).split(":")
        name = parts[0].strip()
        out: dict = {"name": name.rstrip("!"), "required": name.endswith("!")}
        if len(parts) > 1 and parts[1]:
            t = parts[1].strip()
            out["many"] = t.endswith("[]")
            out["type"] = t[:-2] if out["many"] else t
        if len(parts) > 2 and parts[2]:
            out["policy"] = parts[2].strip()
        return FieldSpec(**out)
    except ValidationError as e:
        raise SiteError("bad_field", f"Field definition {spec!r} is not valid: {_first_error(e)}",
                        f"Types: {', '.join(TYPES)}. Policies: {', '.join(POLICIES)}. "
                        "Shorthand: name[!]:type[[]][:policy].")


class StructureDef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    type: Literal["collection", "tree", "form"] = "collection"
    kind: Optional[str] = None
    semantic: Optional[str] = None
    key: str = "id"
    access: Literal["public", "invite", "private"] = "public"
    description: str = ""
    fields: List[FieldSpec] = []
    # Feature settings such as "booking", each checked by the feature that owns it (see hooks.SETTINGS).
    settings: dict = {}

    def field(self, name: str) -> Optional[FieldSpec]:
        for f in self.fields:
            if f.name == name:
                return f
        return None

    def names(self, *policies: str) -> list:
        return [f.name for f in self.fields if not policies or f.policy in policies]

    @property
    def public(self) -> bool:
        return self.access == "public"

    def logged_names(self) -> list:
        return self.names("logged")

    def live_names(self) -> list:
        return self.names("live", "capacity")

    def dump(self) -> dict:
        out = self.model_dump(mode="json", exclude_none=True, exclude_defaults=False)
        if not out["settings"]:
            del out["settings"]
        return out


# ---------------------------------------------------------------- validation

def _dt_in(value: Any) -> Any:
    if isinstance(value, (dt.datetime, dt.date)):
        return value.isoformat()
    return value


LocalDateTime = Annotated[str, BeforeValidator(_dt_in)]

_PY = {
    "string": str, "text": str, "integer": int, "number": float, "decimal": Decimal,
    "boolean": bool, "date": dt.date, "datetime": LocalDateTime, "json": Any,
}


@functools.lru_cache(maxsize=256)
def _model(def_json: str):
    sdef = StructureDef(**json.loads(def_json))
    fields = {}
    for f in sdef.fields:
        py = _PY[f.type]
        if f.many:
            py = List[py]
        if f.required and f.default is None:
            fields[f.name] = (py, ...)
        else:
            fields[f.name] = (Optional[py], f.default)
    config = ConfigDict(extra="forbid", coerce_numbers_to_str=True)
    return create_model(f"Record_{sdef.name}", __config__=config, **fields)


def _first_error(e: ValidationError, hide: Any = ()) -> str:
    parts = []
    for err in e.errors():
        if err["loc"] and str(err["loc"][0]) in hide:
            continue
        loc = ".".join(str(p) for p in err["loc"]) or "value"
        parts.append(f"{loc}: {err['msg']}")
    return "; ".join(parts[:5]) or "it needs a value this key may not set"


def validate(sdef: StructureDef, data: dict, site_tz: ZoneInfo, hide: Any = ()) -> dict:
    """Check and normalise a whole record. Returns JSON-ready data with unset fields left out.

    `hide` names fields the caller may not know about; errors and hints leave them out.
    """
    if not isinstance(data, dict):
        raise SiteError("invalid", "Record data must be an object of field values.")
    try:
        model = _model(json.dumps(sdef.dump(), sort_keys=True))
        clean = model(**data).model_dump(mode="json", exclude_none=True)
    except ValidationError as e:
        raise SiteError("invalid", f"'{sdef.name}' record is not valid: {_first_error(e, hide)}",
                        "Fields: " + ", ".join(f"{f.name} ({f.type}{', required' if f.required else ''})"
                                               for f in sdef.fields if f.name not in hide))
    tz = record_zone(sdef, clean, site_tz)
    for f in sdef.fields:
        if f.type == "datetime" and f.name in clean:
            v = clean[f.name]
            clean[f.name] = [timeview.normalize_local(x, tz) for x in v] if f.many \
                else timeview.normalize_local(v, tz)
    return clean


def record_zone(sdef: StructureDef, data: dict, site_tz: ZoneInfo) -> ZoneInfo:
    """A record with a `timezone` field carries its own zone; otherwise the site's applies."""
    name = data.get("timezone") if sdef.field("timezone") else None
    return timeview.zone(name) if name else site_tz


def instants(sdef: StructureDef, data: dict, site_tz: ZoneInfo) -> dict:
    """UTC instants for the record's single datetime fields, so agents need no zone arithmetic."""
    tz = record_zone(sdef, data, site_tz)
    out = {}
    for f in sdef.fields:
        if f.type == "datetime" and not f.many and f.name in data:
            out[f.name] = timeview.local_to_z(data[f.name], tz)
    return out


# ---------------------------------------------------------------- public JSON Schema

_JSON_TYPES = {
    "string": {"type": "string"},
    "text": {"type": "string", "contentMediaType": "text/markdown"},
    "integer": {"type": "integer"},
    "number": {"type": "number"},
    "decimal": {"type": "string", "format": "decimal"},
    "boolean": {"type": "boolean"},
    "date": {"type": "string", "format": "date"},
    "datetime": {"type": "string", "format": "local-date-time"},
    "json": {},
}


def public_schema(sdef: StructureDef, prefix: str = "", include_private: bool = False) -> dict:
    props, required = {}, []
    for f in sdef.fields:
        if f.policy == "private" and not include_private:
            continue
        p = dict(_JSON_TYPES[f.type])
        if f.many:
            p = {"type": "array", "items": p}
        if f.description:
            p["description"] = f.description
        p["x-history"] = {"logged": "logged", "live": "live", "capacity": "live", "private": "private"}[f.policy]
        if f.policy == "capacity":
            p["x-capacity"] = True
        if f.semantic:
            p["x-property"] = f.semantic
        if f.choices:
            kind, target = next(iter(f.choices.items()))
            p["x-choices"] = f"{prefix}/{'trees' if kind == 'tree' else 'collections'}/{target}"
        props[f.name] = p
        if f.required:
            required.append(f.name)
    out = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": sdef.name, "type": "object", "x-key": sdef.key,
        "properties": props, "required": required,
    }
    if sdef.description:
        out["description"] = sdef.description
    if sdef.kind:
        out["x-kind"] = sdef.kind
    if sdef.semantic:
        out["x-type"] = sdef.semantic
    return out


# ---------------------------------------------------------------- filter comparison

def comparable(f: FieldSpec, value: Any, tz: ZoneInfo) -> Any:
    """A stored field value in a form that orders correctly."""
    if value is None:
        return None
    if f.type in ("integer", "number", "decimal"):
        return Decimal(str(value))
    if f.type == "datetime":
        return timeview.local_to_z(value, tz)
    if f.type == "json":
        return json.dumps(value, sort_keys=True)
    return value


def filter_value(f: FieldSpec, raw: Any, site_tz: ZoneInfo, now: dt.datetime) -> Any:
    """A filter value from a query string or JSON, converted to compare with `comparable` output."""
    try:
        if f.type in ("integer", "number", "decimal"):
            return Decimal(str(raw))
        if f.type == "boolean":
            if isinstance(raw, bool):
                return raw
            low = str(raw).lower()
            if low in ("true", "1", "yes"):
                return True
            if low in ("false", "0", "no"):
                return False
            raise ValueError
        if f.type == "date":
            return timeview.resolve_date(raw, site_tz, now).isoformat()
        if f.type == "datetime":
            return timeview.to_z(timeview.resolve_instant(raw, site_tz, now))
        if f.type == "json":
            return json.dumps(raw, sort_keys=True)
        return str(raw)
    except (InvalidOperation, ValueError):
        raise SiteError("bad_filter", f"'{raw}' is not a valid {f.type} for field '{f.name}'.")
