# Authoring an AI-sites site

This is the whole model. It is the same through every door: Python, the command line, the admin web API
and MCP. If you are an AI editing a site, call `describe` first.

## Three structures

| Structure | What it is | Human equivalent |
|---|---|---|
| Collection | Records that share a schema, looked up by key | Table |
| Tree | Nested nodes, each with an `id` and a `label` | Hierarchy, menu |
| Choice | A field whose value must be a key of a collection or a node of a tree | Dropdown |

## Operations

| Level | Operation | Rule |
|---|---|---|
| Site | `describe` | Settings, structures and built-in kinds; or one structure's definition |
| Site | `configure` | Change name, description, timezone, language, currency |
| Structure | `define` | Create a collection (optionally of a kind) or a tree. Fails if the name exists |
| Structure | `alter` | Add or remove fields, change access or description, set references |
| Structure | `drop` | Delete it and its contents. Fails if another structure refers to it |
| Record / node | `add` | Fails if the key exists. A missing key is generated |
| Record / node | `get` | One record with its scheduled changes, or a node with its subtree |
| Record / node | `list` | Filter, sort and page records; or read a tree |
| Record / node | `change` | Fails if the key does not exist. `null` clears a field |
| Record / node | `remove` | Fails if the key does not exist or something refers to it |
| Node | `move` | New parent and/or position. Fails if it would create a loop |
| Time | `schedule` | A change that starts by itself and, with an end, reverts by itself |
| Time | `unschedule` | Cancel a scheduled change by id |
| Time | `confirm` | Mark a record as still true now, without changing it |
| Time | `hours` | Open or closed on a date, as visitors are told |

Every operation is one transaction and returns the new state. Errors have a `code`, a `message` and
usually a `hint` saying what to do instead.

## The four doors

```python
site["games"].schedule("GAME-3", price="47.99", on="2026-12-15")                 # Python
```
```
aisites schedule games GAME-3 price=47.99 --on 2026-12-15                         # command line
POST /admin/games/schedule   {"key": "GAME-3", "set": {"price": "47.99"}, "on": "2026-12-15"}
MCP tool `schedule`          {"target": "games", "key": "GAME-3", "set": {"price": "47.99"}, "on": "2026-12-15"}
```

The admin web API and remote MCP (`/mcp`) need `Authorization: Bearer <owner key>`. The command line,
Python and local MCP (`aisites mcp <folder>`) act on the site folder directly.

## Fields

A field is `name[!]:type[[]][:policy]`, or the same as an object. `!` means required, `[]` means a list.

- Types: `string`, `text` (Markdown), `integer`, `number`, `decimal` (money), `boolean`, `date`,
  `datetime`, `json`.
- Policies:
  - `logged` (default): every change goes into the public history.
  - `live`: served with a "read at" time, not kept in history. Use for stock and other fast values.
  - `capacity`: a live whole number that can be reserved against (Milestone 3).
  - `private`: never served to visitors, never logged.

## Access

Each structure is `public`, `invite` or `private`. Private structures are invisible to everyone but the
owner and never enter the history. Invite-only structures are listed but, until guest keys arrive in
Milestone 3, readable only with the owner key.

## Built-in kinds

Use a kind when one fits, so other agents know what your fields mean. Add your own fields with `fields`.

| Kind | For | Notable fields |
|---|---|---|
| `Business` | Who runs the site | name, telephone, email, url |
| `Place` | A location or branch | name, address, city, country, latitude, longitude |
| `Hours` | Weekly opening pattern | weekly `{"mon": [["08:00", "15:00"]]}`, place, timezone |
| `Closure` | Dated exception to hours | hours, start_date, end_date, closed, intervals, reason |
| `Notice` | Announcement that expires | title, text, starts, ends, valid_from, valid_until |
| `Page` | Prose | slug, title, body |
| `Product` | Something for sale | sku, name, price, price_currency, stock |
| `Offer` | A price or stock level per region or place | product, region, place, price, price_currency, stock |
| `Slot` | Bookable time with capacity | name, start, end, capacity, remaining |
| `Event` | Calendar entry | title, start, end, timezone, where, with, notes |

`refs` turns a field into a choice: `define offers --kind Offer --ref product=games --ref place=places`.

## Time

- **Zones.** Use region names (`America/Los_Angeles`), never abbreviations (`PST`). A `datetime` is local
  wall time in the record's `timezone` field, or the site's zone if it has none. Readers also get the UTC
  instant.
- **Expiry.** A structure with `valid_from` / `valid_until` fields hides a record outside that window.
  Give anything that goes stale an end, so old information removes itself.
- **Scheduled changes.** `schedule` takes `start` and optional `end`, or `on` for one whole day. Without
  `end` the change is permanent from `start`. By default visitors cannot see it until it starts; pass
  `announce` to show it in advance. During a scheduled change, `change` edits the underlying value, which
  shows again when the scheduled change ends.
- **Confirm.** If something is still true, `confirm` it. Visitors see when it was last confirmed.
- **Filters.** Date and time filters accept `today` and `tomorrow`, and ranges: `start.gte=today`.

## Recipes

Opening hours with a closure:

```
aisites define hours --kind Hours
aisites define closures --kind Closure --ref hours=hours
aisites add hours id=main 'weekly:={"mon": [["08:00","15:00"]], "tue": [["08:00","15:00"]]}'
aisites add closures hours=main start_date=2026-11-17 reason="Staff training"
```

A notice that expires:

```
aisites define notices --kind Notice
aisites add notices title="At Fifth and Cabrillo" valid_until=2026-11-16T22:00
```

A one-day discount, kept quiet until the day:

```
aisites schedule games GAME-3 price=47.99 --on 2026-12-15
```

A private calendar:

```
aisites define calendar --kind Event --access private
aisites add calendar title="Bridget" start=2026-11-23T14:00 timezone=America/Los_Angeles
aisites list calendar start.gte=today start.lt=2026-11-28 --sort start
```
