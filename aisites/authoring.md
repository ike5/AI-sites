# Authoring an AI-sites site

This is the whole model. It is the same through every door: Python, the command line, the admin web API
and MCP. If you are an AI editing a site, call `describe` first.

Through MCP the owner is shown only the tools the site uses. Tools for bookings, message forms, trees, opening
hours, staff roles and contacts appear once the site has one of them. Until then, `other` lists them (with no
`name`) and runs them (with `name` and `arguments`). Reconnect to see new tools.

## Four structures

| Structure | What it is | Human equivalent |
|---|---|---|
| Collection | Records that share a schema, looked up by key | Table |
| Tree | Nested nodes, each with an `id` and a `label` | Hierarchy, menu |
| Choice | A field whose value must be a key of a collection or a node of a tree | Dropdown |
| Form | A structure visitors can add to but not read | Contact form |

## Operations

| Level | Operation | Rule |
|---|---|---|
| Site | `describe` | Short form by default: each structure's fields and the built-in kinds' names. `full=true` gives every field definition and each built-in kind's fields. `describe <structure>` gives one structure |
| Site | `configure` | Change name, description, timezone, language, currency, and `listed` (whether a host may include the site when one question is put to every site it serves) |
| Site | `set_limits` | Owner only. Shows or sets visitor quotas: `anonymous_per_day`, `key_per_day`, `concurrent`, `trust_proxy`. Works through all doors |
| Structure | `define` | Create a collection (optionally of a kind) or a tree. Fails if the name exists |
| Structure | `alter` | Add or remove fields, change access or description, set references |
| Structure | `drop` | Delete it and its contents. Fails if another structure refers to it |
| Record / node | `add` | Fails if the key exists. A missing key is generated |
| Record / node | `get` | One record with its scheduled changes, or a node with its subtree. `with=` attaches linked records |
| Record / node | `list` | Filter, sort and page records. `q=` matches words; `with=` attaches linked records. Or read a tree |
| Record / node | `change` | Fails if the key does not exist. `null` clears a field |
| Record / node | `remove` | Fails if the key does not exist or something refers to it |
| Node | `move` | New parent and/or position. Fails if it would create a loop |
| Time | `schedule` | A change that starts by itself and, with an end, reverts by itself |
| Time | `unschedule` | Cancel a scheduled change by id |
| Time | `confirm` | Mark a record as still true now, without changing it |
| Time | `hours` | Open or closed on a date, as visitors are told |
| History | `redact` | Owner only. Removes a past value from the public history by `seq`, with optional `fields` and `reason`. Refused while the site still serves the value or a scheduled change has it |
| History | `checkpoint` | Owner only. Stores the signed head and sends it to each anchor |
| Access | `set_role` | Owner only. Sets a role's name and grants |
| Access | `drop_role` | Owner only. Fails while unrevoked keys still use the role |
| Access | `issue_key` | Owner only. Gives a role key; the secret is shown once |
| Access | `revoke_key` | Owner only. Stops the key working at once |
| Access | `list_keys` | Owner only. Lists roles with their grants, and keys by id and label; never a secret |
| Access | `audit` | Owner only. Reads the private audit trail, filtered by `who`, `target` and `limit` |
| Contacts | `set_contacts` | Owner only. `requests`: `off` (default), `invite` or `open`. `purposes`: what a request must choose from. `role`: the role an approved contact gets (default `contact`) |
| Contacts | `contacts` | Owner only. Lists contacts and waiting requests. `status` narrows to `pending`, `approved` or `blocked` |
| Contacts | `contact` | Owner only. `action`: `approve`, `block` or `remove`. `role` changes the role; `public_key` replaces a lost key |
| Contacts | `invite` | Owner only. Makes a one-use invitation, valid 72 hours unless `hours` says otherwise. Whoever presents it is approved at once |
| Visitor | `reserve` | Places a hold and lowers `capacity` at once, never below zero. Unconfirmed holds release themselves |
| Visitor | `book` | Confirms the caller's own hold with the details the owner requires. Returns a payment hand-off if configured |
| Visitor | `release` | Cancels the caller's own hold or booking and returns the capacity |
| Visitor | `booking` | Shows the status of the caller's own booking. Others' bookings are "not found" |
| Visitor | `submit` | Adds a message to a form. Returns a ticket id |
| Visitor | `ticket` | Shows the sender a message's status (`received`, `read`, `answered`) and its one reply |
| Bookings | `bookings` | Lists a structure's bookings with their details. Can be granted to a staff role. The owner can release any booking |
| Messages | `inbox` | Lists messages without their free text, so an AI can triage. Can be granted to a role |
| Messages | `read_message` | Returns one message in an envelope whose first entry marks the text as data from an outside party |
| Messages | `reply` | Sends the one reply allowed per message |
| Questions | `ask` | Asks a question as a visitor would, to check what they are told. With no id, lists the questions. Reads only |
| Questions | `question` | Defines, replaces or removes (`--remove`) a question visitors can ask. Owner only |
| Questions | `find` | Finds records anywhere on the site by words in their names and text. Reads only |

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

The admin web API and remote MCP (`/mcp`) accept `Authorization: Bearer <owner key>` or a role key. The command line,
Python and local MCP (`aisites mcp <folder>`) act on the site folder directly.

## Fields

A field is `name[!]:type[[]][:policy]`, or the same as an object. `!` means required, `[]` means a list.

- Types: `string`, `text` (Markdown), `integer`, `number`, `decimal` (money), `boolean`, `date`,
  `datetime`, `json`.
- Policies:
  - `logged` (default): every change goes into the public history.
  - `live`: served with a "read at" time, not kept in history. Use for stock and other fast values.
  - `capacity`: a live whole number that visitors can reserve against once the structure has a `booking` setting.
  - `private`: never served to visitors, never logged.

## Access

Each structure is `public`, `invite` or `private`. Private structures are invisible to everyone but the
owner and never enter the history. Invite-only structures are listed. A key whose role has the `read` grant
can read them on the public routes, in the visitor view with no private fields. A role granted `get` or
`list` can also read them through the admin web API or MCP.

## Roles

A role is a name and a list of grants. A grant names a structure (or `*` for all) and the operations allowed
on it:

```json
{"target": "games", "ops": ["get", "list", "change", "schedule", "unschedule"], "fields": ["price"]}
```

- Grantable operations: `add`, `get`, `list`, `change`, `remove`, `move`, `schedule`, `unschedule`, `confirm`
  and `describe`, on a structure the role has a grant on. The staff operations `bookings` (on a bookable
  structure) and `inbox`, `read_message` and `reply` (on a form) can also be granted. Every other operation is
  owner-only, including any added later.
- A guest role is granted visitor operations, such as `reserve`, `book`, `release` and `booking` on a bookable
  structure, or `submit` and `ticket` on a form, and `read` on an invite-only structure. Staff and guest grants
  are separate: having one gives nothing of the other.
- `fields` is optional and means "only these fields". With it, `change` and `schedule` may set only those
  fields, and `unschedule` only if the scheduled change sets only those fields. `remove` and `move` are
  refused, because they affect the whole record.
- A role does not see private fields unless its grant has no field limit or lists the private field.
- A role's change takes effect immediately. There is no approval step.
- Every change, successful or refused, is audited privately: who, role, operation, target, key, field names
  and outcome, never values. Calling `Site` methods directly from Python is not audited; only calls through
  the operation table are.

## Settings

A structure can carry feature settings, such as `booking` (bookable capacity) or `form` (message forms).
Set them with `define` or `alter`:

```
aisites alter tables --setting 'booking:={"hold_minutes": 30}'
```

- Giving part of a setting changes just that part. Here only the hold length changes.
- `null` for a whole setting removes it.
- Settings are part of the structure's public definition.

## Built-in kinds

Use a kind when one fits, so other agents know what your fields mean. Add your own fields with `fields`.

| Kind | For | Notable fields |
|---|---|---|
| `Business` | Who runs the site | name, telephone, email, url |
| `Place` | A location or branch | name, address, city, country, latitude, longitude, aliases |
| `Hours` | Weekly opening pattern | weekly `{"mon": [["08:00", "15:00"]]}`, place, timezone |
| `Closure` | Dated exception to hours | hours, start_date, end_date, closed, intervals, reason |
| `Notice` | Announcement that expires | title, text, starts, ends, valid_from, valid_until |
| `Page` | Prose | slug, title, body |
| `Product` | Something for sale | sku, name, price, price_currency, stock, aliases |
| `Offer` | A price or stock level per region or place | product, region, place, price, price_currency, stock |
| `Slot` | Bookable time with capacity | name, start, end, capacity, remaining |
| `Event` | Calendar entry | title, start, end, timezone, where, with, notes |

`refs` turns a field into a choice: `define offers --kind Offer --ref product=games --ref place=places`.

## Questions visitors can ask

Every structure is already a question: a visitor opens `/ask/<name>` and gets its records, or its options.
These make the answers better:

- Declare references with `refs` when you define a structure. A declared reference lets one request bring the
  linked records along (`with=`), so the answer needs no second request.
- Give `Product` and `Place` records `aliases`: other names a visitor may type. Search matches them.
- Use `ask` to see what a visitor is told. With no id it lists the questions: `aisites ask stock q=ibuprofen`.
- Add a question of your own with `question`. This one shows where a product is in stock, with the branch and
  its hours:

```
aisites question in_stock --does "Where a product is in stock right now" --read stock \
    --takes q --takes product --takes place.city --with place --with place.hours \
    --fields stock --fields place.name --options product   stock.gt=0
```

- `does`: the one line shown in the list of questions.
- `read`: the collection it reads.
- `where`: filters always applied. Here, the `name=value` pair at the end (`stock.gt=0`).
- `takes`: what the asker may add: `q`, or filter names.
- `with`, `fields`, `sort`: what to attach, which fields to show, and the order.
- `options`: a field in `takes` whose values are offered as links when nothing is added.

A question is checked when it is defined. It runs as the least privileged reader it is for, so it can only show what
a visitor could already read. Remove one with `--remove`. The full reader standard is in
[`docs/standard.md`](../docs/standard.md).

## People the site knows

Callers without a key are told apart only by network address. Contacts are told apart by their own key, so their
limits hold across tokens. Switch them on with the owner's commands; the role they get must exist first:

```
aisites set_role contact --grant '{"target": "tables", "ops": ["reserve", "book", "release", "booking"]}'
aisites set_contacts --requests open --purposes "Booking question" --purposes "Order question"
aisites contacts --status pending          # who is asking
aisites contact ct_4e796c48104fa658 --action approve
```

- `aisites invite --label "For Ana"` makes a one-use invitation, valid 72 hours. Whoever presents it is approved at once.
- To keep each person's records apart, mark a private collection as owned, naming the field that holds the
  contact's id:

```
aisites define orders --access private --field contact --field item --field status \
    --setting 'owned:={"field": "contact"}'
aisites set_role contact --grant '{"target": "orders", "ops": ["list", "get", "add", "change"]}'
```

- A contact sees only the records whose field holds its own id. Private fields are kept from contacts unless the
  role's grant names them in `fields`.

The request, the permission statement and the limits are in [`docs/identity.md`](../docs/identity.md).

## Time

- **Zones.** Use region names (`America/Los_Angeles`), never abbreviations (`PST`). A `datetime` is local
  wall time in the record's `timezone` field, or the site's zone if it has none. Readers also get the UTC
  instant.
- **Expiry.** A structure with `valid_from` / `valid_until` fields hides a record outside that window.
  Give anything that goes stale an end, so old information removes itself.
- **Scheduled changes.** `schedule` takes `start` and optional `end`, or `on` for one whole day. Without
  `end` the change is permanent from `start`. By default visitors cannot see it until it starts; pass
  `announce` to show it in advance. During a scheduled change, `change` edits the underlying value, which
  shows again when the scheduled change ends. The capacity field that bookings draw on cannot be scheduled.
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

A product manager who may only change prices. Create the role, then issue the key:

```
aisites set_role pricing --grant '{"target":"games","ops":["get","list","change","schedule","unschedule"],"fields":["price"]}'
aisites issue_key pricing --label "Product manager"
```

A legal takedown. Change the value to its replacement with `change`, then redact the old one. Each history
entry that carries the value needs its own redaction; find their sequence numbers in
`GET /collections/{name}/{key}/history`. Redaction is refused while the site still serves the value. The
reason is public, so write it for readers:

```
aisites change pages about body="Corrected text"
aisites redact 37 --fields body --reason "Court order 12/2026"
```

Let others check your history. Keep an anchor outside the site's control, such as a file in a git repository,
then checkpoint the signed head. Anyone can verify the history with a pin they keep, and a later run detects a
rewritten or truncated history:

```
aisites anchors --add-file /path/outside/heads.jsonl
aisites checkpoint
aisites verify <url-or-site-folder> --pin <pin file>
```

A restaurant that takes bookings. Visitors book with `reserve`, then `book` with the details the owner requires:

```
aisites define tables --kind Slot --setting 'booking:={"open": true, "hold_minutes": 15, "details": ["name!:string", "party_size!:integer"]}'
aisites add tables id=fri-8pm name="Friday, 8pm" start=2026-11-20T20:00 capacity=4 remaining=4
aisites reserve tables fri-8pm --quantity 2
```

A visiting agent reads `GET /bookable/tables` first to learn the hold length and the details it must send.

Play times for invited friends only. Each friend's family gets a key with the guest grants:

```
aisites define playtimes --kind Slot --access invite --setting 'booking:={"open": false, "hold_minutes": 30}'
aisites set_role friends --grant '{"target":"playtimes","ops":["read","reserve","book","release","booking"]}'
aisites issue_key friends --label "Sam's family"
```

A contact form with an inbox role. Senders need a key or an approved contact (see
[People the site knows](#people-the-site-knows)). The inbox role can read and answer but nothing else:

```
aisites define contact --type form --field order_number:string --field 'message!:text' --setting 'form:={"purposes": ["Order question", "Other"], "text_field": "message", "text_max": 600}'
aisites set_role customers --grant '{"target":"contact","ops":["submit","ticket"]}'
aisites issue_key customers --label "First customer"
aisites set_role inbox --grant '{"target":"contact","ops":["inbox","read_message","reply"]}'
aisites issue_key inbox --label "Owner's assistant"
```

Raise or lower visitor quotas. With no arguments the command shows the current values:

```
aisites set_limits
aisites set_limits anonymous_per_day:=50
```

Set `trust_proxy:=true` only when the site runs behind your own proxy, so forwarded addresses are believed.
