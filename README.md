# AI-sites

A free, open-source, self-hosted framework for sites that are read and used by AI agents.

More and more visits to a site come from an AI acting for a person. AI-sites serves that visitor first:
typed data it can read cheaply, a clear statement of how fresh each fact is, a public history it can look
back through, and one small set of operations for whoever maintains the site, human or AI.

**Status: Milestones 1, 2 and 3 of 4.** Reading, discovery, time, the four editing doors and multi-site hosting work
and are tested. Signed history with a verifier, redaction, and roles with an audit trail work too. Acting on a site
(bookings, guest keys, message forms, streaming) now works. Key-based identity comes next; see [Roadmap](#roadmap). A first slice of a hosted service now exists: sign up by email, create a site from a template, and connect your own AI.

## Quick start

```
pip install -e .

aisites site create ./school --name "Lincoln Elementary" --timezone America/Los_Angeles
export AISITES_SITE=./school

aisites define hours --kind Hours
aisites define closures --kind Closure --ref hours=hours
aisites add hours id=school 'weekly:={"mon":[["08:00","15:00"]],"tue":[["08:00","15:00"]]}'
aisites add closures hours=school start_date=2026-11-17 reason="Staff training"

aisites serve ./school
```

A visiting agent now needs one request:

```
$ curl 'localhost:8000/hours?date=2026-11-17'        # or date=today, date=tomorrow
{"results":[{"key":"school","date":"2026-11-17","weekday":"tue","timezone":"America/Los_Angeles",
             "open":false,"intervals":[],"source":"closure:…","reason":"Staff training"}],"as_of":"…"}
```

Questions make this shorter. The first response lists a site's questions, and each one answers in one request
with the linked records attached. With the pharmacy example (`python -m examples.pharmacy ./sites/pharmacy`,
then `aisites serve ./sites/pharmacy`):

```
$ curl localhost:8000/                                     # the list of questions
$ curl 'localhost:8000/ask/stock?q=ibuprofen&stock.gt=0'   # stock, with the branch and its hours today
{"question":"stock","collection":"stock","items":[{"key":"ibuprofen-200@norte","data":{"stock":32},
  "live_as_of":"…","with":{"product":{…},"place":{…}}}],"total":1,"as_of":"…"}
```

The whole reader standard is in [`docs/standard.md`](docs/standard.md).

`site create` prints an owner key once. The web admin API and remote MCP need it; the command line,
Python and local MCP work on the site folder directly.

A site can also start from a template: `aisites site create ./school --name "Lincoln" --template school`.
`aisites templates` lists them.

Eight complete example sites are in [`examples/`](examples): a school, a street cart, a game catalogue
with regional prices and a one-day discount, a pharmacy chain with stock per branch, a private
personal calendar, a restaurant that takes bookings, a child's play times for invited friends, and a
contact form. Each runs with `python -m examples.<name> <folder>`.

## The model

Everything is built from four structures and one set of operations. The full guide is
[`aisites/authoring.md`](aisites/authoring.md), also printed by `aisites guide`.

| Structure | What it is | Human equivalent |
|---|---|---|
| Collection | Records sharing a schema, looked up by key | Table |
| Tree | Nested nodes with ids | Hierarchy, menu |
| Choice | A field whose value must come from a collection or tree | Dropdown |
| Form | A structure visitors can add to but not read | Contact form |

| Level | Operations |
|---|---|
| Site | `describe`, `configure`, `set_limits` |
| Structure | `define`, `alter`, `drop` |
| Record or node | `add`, `get`, `list`, `change`, `remove`, `move` |
| Time | `schedule`, `unschedule`, `confirm`, `hours` |
| History | `redact`, `checkpoint` |
| Access | `set_role`, `drop_role`, `issue_key`, `revoke_key`, `list_keys`, `audit` |
| Visitor actions | `reserve`, `book`, `release`, `booking`, `submit`, `ticket` |
| Bookings and messages | `bookings`, `inbox`, `read_message`, `reply` |
| Questions | `ask`, `question`, `find` |

Structures are defined as data, not code, so an AI can create them and a host can serve many sites
without running anyone's Python.

### Four doors, one behaviour

```python
site["games"].schedule("GAME-3", price="47.99", on="2026-12-15")          # Python
```
```
aisites schedule games GAME-3 price=47.99 --on 2026-12-15                  # command line
POST /admin/games/schedule  {"key":"GAME-3","set":{"price":"47.99"},"on":"2026-12-15"}   # admin web API
MCP tool `schedule`         {"target":"games","key":"GAME-3","set":{"price":"47.99"},"on":"2026-12-15"}
```

All four call the same function. A test runs one script of operations through each door and requires the
resulting sites to be identical.

For an AI editing a site: `aisites mcp <folder>` serves the tools over stdio; a served site exposes them
at `/mcp` with the owner key or a role key as a bearer token. `GET /admin/ops` describes every operation.

The owner's MCP tool list is kept short. Tools for bookings, message forms, trees, opening hours and staff roles
are listed once the site uses them. Until then, `other` lists them and runs them, and a call that switches a
feature on says so in `tools_added`. A connected assistant sees new tools the next time it connects. Hiding is
about how much an assistant reads, not permission: every call is still checked. Measured with
`python tools/measure.py`: all 37 operations are about 19 KB (about 4,700 tokens). The owner of the pharmacy
example is shown 23 tools, about 13.4 KB (about 3,400 tokens). A pricing role in the game catalogue example is
shown 5 tools, about 3.8 KB.

### Shared meaning

Built-in kinds give fields the same meaning on every site, following schema.org, so an agent can compare
prices or hours across sites: `Business`, `Place`, `Hours`, `Closure`, `Notice`, `Page`, `Product`,
`Offer`, `Slot`, `Event`. Add your own fields to a kind, or define fully custom structures.

### Field policies and access

| Policy | Served to visitors | In public history |
|---|---|---|
| `logged` (default) | yes | yes |
| `live` / `capacity` | yes, with the time it was read | no |
| `private` | no | no |

Each structure is `public`, `invite` or `private`. Private structures are invisible without the owner key.

### Guest keys

A guest key is an ordinary role key whose role is granted visitor operations. There is no separate concept:

```
aisites set_role diners --grant '{"target":"tables","ops":["read","reserve","book","release","booking"]}'
aisites issue_key diners --label "Regulars"
```

- The grant `read` lets a key read an invite-only structure on the public read routes. It gets the visitor
  view, with no private fields.
- Staff grants (such as `change`) and guest grants (such as `reserve`) are separate. Having one gives nothing
  of the other.

### Roles and the audit trail

A role is a name with a list of grants. A grant names a structure (or `*` for all), the operations allowed
on it, and optionally the only fields it may touch:

```json
{"target": "games", "ops": ["get", "list", "change", "schedule", "unschedule"], "fields": ["price"]}
```

Roles can be granted `add`, `get`, `list`, `change`, `remove`, `move`, `schedule`, `unschedule`, `confirm`
and `describe`, and on a form `inbox`, `read_message` and `reply`. `bookings` can be granted to a staff role.
Guest roles are granted visitor operations (see [Guest keys](#guest-keys)). Every other operation is for the owner only. With a field list, `change` and `schedule` may
set only those fields, and `remove` and `move` are refused because they affect the whole record. A role's
change takes effect at once; there is no approval step.

A product manager who may change only prices:

```
aisites set_role pricing --grant '{"target":"games","ops":["get","list","change","schedule","unschedule"],"fields":["price"]}'
aisites issue_key pricing --label "Product manager"
```

The key's secret starts with `ais_key_`. It is shown once and stored only as a hash. `aisites revoke_key <key id>`
stops it at once. Role keys work on the admin web API and on `/mcp`.

A change outside the grant is refused with HTTP status 403:

```json
{"error":{"code":"forbidden","message":"Role 'pricing' may not change field name in 'games'.","hint":"In 'games' it may change only: price."}}
```

Every operation that changes the site is written to a private audit trail, whether it succeeded or was refused:
time, who, role, operation, target, record key, the names of the fields set, and the outcome. Values are never
recorded. `who` is a key id, `owner` or `local`. The trail never appears in the public history, the change feed
or the map. Read it with `aisites audit --who <key id, owner or local> --target <structure> --limit 20`.

## What a visiting agent gets

| Route | Purpose |
|---|---|
| `GET /.well-known/aisite.json` (also `/`) | First response: the list of questions (`ask`), each with its `url` and what it takes |
| `GET /ask` | Every question, with what each takes |
| `GET /ask/{id}` | Answers a question; with nothing added, lists its options as links |
| `POST /ask` | Several questions in one request: `{"ask": [{"id": ..., ...}]}` |
| `GET /find?q=` | Records anywhere on the site matching words, up to 10 by default |
| `GET /map` | Every structure with its fields, where each reference leads, and its `actions` |
| `GET /llms.txt` | The questions as Markdown links |
| `GET /collections/{name}` | Records. Filters `field=value`, `.gte .gt .lte .lt .ne`, `field.has=words`, `place.city=` through a reference; `q=words`; `with=` attaches linked records; `sort`, `limit`, `cursor`, `fields`, `format=table` |
| `GET /collections/{name}/{key}` | One record with `updated`, `confirmed`, `live_as_of`, `next_change`; `with=` attaches linked records |
| `GET /collections/{name}/schema` | JSON Schema with `x-type`, `x-property`, `x-history`, `x-choices` |
| `GET /trees/{name}` | A tree; `node=` for a subtree, `depth=` to limit it |
| `GET /hours?date=` | Open or closed on a date (`today`, `tomorrow`, `YYYY-MM-DD`), with `next_change` |
| `GET /changes?since=` | What changed since a cursor |
| `GET /changes/stream` | The same feed as server-sent events; resumes from `since=` or `Last-Event-ID` |
| `GET /actions` | Every action a visitor can take, per structure, with its arguments as JSON Schema |
| `POST /actions/{target}/{op}` | Take a visitor action with a JSON object of arguments; custom actions are at `POST /actions/{name}` |
| `GET /bookable/{target}` | What a booking needs: hold length, details schema, whether there is a payment step |
| `GET /forms/{name}` | A message form's fields, purposes, limits and how to submit |
| `GET /history`, `/collections/{name}/{key}/history` | Past values, paged; never loaded unless asked for |
| `GET /history/head` | Current head of the history, signed with the site key; anchor proofs once checkpointed |
| `GET /history/checkpoints` | Stored checkpoints, paged with `after` and `limit` |
| `GET /openapi.json` | Generated from the site's structures |

- Reads are never limited by quotas. The change stream allows 5 open streams per network address. Every response
  has an ETag and answers `If-None-Match` with 304.
- Every response links to the first response, so an agent landing anywhere can find the entry point.
- HTML is returned only when `Accept` asks for it; it is a plain rendering of the same data.
- Errors share one shape: `{"error": {"code", "message", "hint"}}`.

## Acting on a site

A site can also take actions, such as booking a table or sending a message. The owner decides what is
allowed. Each action is one request with a JSON object of arguments.

### The visitor door

```
POST /actions/tables/reserve
Idempotency-Key: 6f1c0d2e9a4b4c7e8f0a1b2c3d4e5f60
{"key": "fri-8pm", "quantity": 2}
```

- `GET /actions` lists every action a visitor can take, per structure, with its arguments as JSON Schema and
  whether it needs a key. The first response has `do` pointing here when visitors can act. `GET /map` links to it
  (`links.actions`), and each structure there lists its `actions`.
- Retry safety: a changing action needs an `Idempotency-Key` header with any fresh random value. Repeating the
  same key and body returns the first answer with `Idempotent-Replay: true` and does not act twice. The same key
  with a different body gets 409 `idempotency_mismatch`. Without the header the answer is 400 `needs_idempotency_key`.
- Preview: add `?dry_run=true` to run the action and roll it back. The answer is `{"dry_run": true, "result": {...}}`.
  Nothing is stored and nothing is charged.
- Quotas apply to changing actions only. Defaults: 1000 per key per UTC day, 20 per network address per UTC day for
  callers without a key, and 4 changing calls in flight per caller. Over the limit the answer is 429 with `Retry-After`.
  The owner is not limited. Change them with `aisites set_limits anonymous_per_day:=50` (also `key_per_day`,
  `concurrent`, `trust_proxy`). `aisites set_limits` with no arguments shows them.
- Callers without a key are identified by a hash of their network address; addresses are not stored. A forwarded
  address (`X-Forwarded-For`) is believed only when `trust_proxy` is true, which should be set only behind your own proxy.
- Every changing visitor action is written to the private audit trail.

### Bookings

```
aisites define tables --kind Slot --setting 'booking:={"open": true, "hold_minutes": 15, "details": ["name!:string", "party_size!:integer"]}'
```

- A collection with a capacity field becomes bookable with the `booking` setting. `hold_minutes` is 1 to 30,
  default 15. `field` defaults to the structure's only capacity field. With `"open": true` anyone may book;
  otherwise a key granted the operations is needed.
- `reserve` (`key`, optional `quantity`) places a hold and lowers the capacity at once, never below zero.
  `book` (`booking`, `details`) confirms the caller's own hold with the details the owner requires. If payment
  is configured, it returns a payment hand-off.
- `release` (`booking`) cancels a hold or booking and returns the capacity. `booking` (`booking`) shows the status
  of the caller's own booking. A hold that is not confirmed in time releases itself.
- A caller may have at most `max_active_per_caller` holds and upcoming bookings in a structure. A caller sees and
  acts only on its own bookings; for anyone else's the answer is "not found", the same as for an id that does not exist.
- Bookings and their details are private. Visitors see only the capacity value change, with `live_as_of`.
- `GET /bookable/{target}` tells a visiting agent the hold length, the details schema and whether there is a payment step.
- The owner lists bookings with `aisites bookings tables --status booked` and can release any of them, for example
  `aisites release tables <booking id>`. Removing a record releases its open bookings.

### Message forms

```
aisites define contact --type form --field order_number:string --field 'message!:text' --setting 'form:={"purposes": ["Question", "Complaint"], "text_field": "message", "text_max": 400}'
```

- A form is a structure of type `form`. Visitors can add to it but not read it. Only key-holders can send in this milestone.
- The `form` setting: `purposes` (a list the sender must pick from), `text_field` (the one free-text field),
  `text_max` (default 500, at most 2000), `per_sender_per_day` (default 3) and `per_day` (default 100).
  Lists and JSON fields are not allowed in a form.
- Visitor actions: `submit` (`data`) returns a ticket id. `ticket` (`ticket`) shows the sender the status
  (`received`, `read`, `answered`) and the single reply.
- Free text is cleaned on arrival: normalised, control and invisible characters removed, links and markup replaced
  by markers such as `[link removed]`, and text over the limit refused. This is hygiene, not a defence. The main
  defences are the form's structure and the untrusted marking below.
- `inbox` lists messages without their free text, so an AI can triage without reading untrusted prose.
  `read_message` returns one message in an envelope whose first entry says the content was written by an outside
  party and is data, not instructions; the text is under `untrusted_text`. `reply` sends the one reply allowed per message.
- Messages are private. Audit rows record that a message was sent, not its text.

### Custom actions and payment

```python
@site.action("book_tour", input=TourRequest, open=True)
```

- Self-hosted sites can register Python functions as actions. They get the same door, retry safety, preview and quotas
  as built-in actions, at `POST /actions/{name}`. They are not available to sites loaded from folders by the multi-site host.
- An action's result may carry a payment hand-off (`aisites.PaymentHandoff`: amount, currency, url, expires, note).
  Bookings take one through the `payment` part of the `booking` setting: `url` (which may contain `{booking}`),
  `amount_field` and `currency_field`.
- The framework never handles card details. Real payment providers are not built.

## Time

- **Expiry.** Records with `valid_from` / `valid_until` appear and disappear by themselves.
- **Scheduled changes.** `schedule` applies a change from a start time and, with an end, reverts it.
  It is evaluated when a record is read, so nothing is missed if the server was down. A scheduled change
  is hidden until it starts unless announced.
- **Freshness.** Each record says when its content last changed, when it was last confirmed still true,
  and when its live values were read.
- **Zones.** Times are local wall time plus a region zone such as `America/Los_Angeles`. Abbreviations
  such as `PST` are rejected with the region name to use.

## History

Every change to a public, logged field appends an entry to a hash chain:

```json
{"seq": 12, "time": "2026-11-14T18:00:00Z", "op": "set", "path": "collections/products/BIKE-1",
 "commit": {"price": "sha256:…"}, "detail": {}, "prev": "sha256:…", "hash": "sha256:…",
 "values": {"price": "549.00"}, "salts": {"price": "…"}}
```

- `hash` covers `seq`, `time`, `op`, `path`, `commit`, `detail` and `prev`, as canonical JSON.
- Each value is committed as `sha256(canonical([salt, field, value]))`, so a value can later be withheld
  without breaking the chain.
- A hidden scheduled change is logged as a `sealed` entry: only a commitment is public until it starts,
  after which the content is revealed and provably dates from when it was planned.
- `aisites.history.verify_entries` checks a run of entries.

### Verifying

`aisites verify <url-or-site-folder> [--pin FILE]` checks the whole history: each entry's hash and link to the
one before it, every served value against its commitment, the head's signature, and that the signing key is
the one recorded in the first entry. It exits with status 1 on any problem and prints a JSON report with `ok`,
`problems`, `entries`, `sealed`, `redacted`, `head` and `pin`. It detects an altered value, a deleted entry, a
different signing key, a forged signature, a history rewritten since the pin, and a truncated history.

With `--pin FILE`, it also checks that the head saved by an earlier successful run is still part of the history.
The file is updated when the check passes. A redaction is not a rewrite, so a pin saved before a redaction still
verifies. From Python: `aisites.verify.verify_site(fetch, pin=None)`.

### Checkpoints and anchors

`GET /history/head` returns the current head, signed with the site's Ed25519 key. The owner-only `checkpoint`
operation (`aisites checkpoint`) stores the signed head and sends it to each configured anchor. An anchor is a
place outside the site's control that receives each checkpoint. The file anchor appends one line per checkpoint
to a file, for example in a git repository or a synced folder:

```
aisites anchors --add-file /path/outside/heads.jsonl
```

`aisites anchors` lists anchors and `--clear` removes them. In Python: `Site(directory, anchors=[...])`. The
anchor interface is where independent network timestamping will plug in; it is not built yet.

The signature proves that the holder of the site key published the head. It does not prove that the dates in
the history are true.

### Redaction

`aisites redact 37 --fields description --reason "Court order 12/2026"` removes a past value from the public
history, for example after a legal takedown. Arguments: the entry's `seq`, optional `fields` and optional
`reason`. It is owner-only and works through all four doors.

- The entry stays, with a marker: `"redacted": {"description": {"at": "<time>", "by": <seq of the redact entry>}}`.
  The field is no longer in `values` or `salts`. The chain still verifies, because each hash covers a commitment
  to the value and not the value itself.
- A new `redact` entry names the entry and fields. The reason, if given, is public: one line, at most 200
  characters. The history never says who redacted; the audit trail does.
- Redaction is refused with `still_current` (HTTP 409) while the site still serves the value or has it in a
  scheduled change. Change or remove it first. A sealed entry is redacted as a whole and can then never be revealed.
- The value is also overwritten in the database files on disk, not only removed from what the site serves.

## Many sites on one server

```
aisites serve --sites ./sites        # each subfolder is a site, served at /s/<name>/
```

A `hosts.json` in the sites folder maps hostnames to sites: `{"school.example.org": "school"}`. Sites
created while the server runs are picked up on first request. One site's key is not valid on another.

## Hosted service

A host operator can run one server that anyone may sign up to. Turn the sites folder into a host, then serve it:

```
aisites host init ./sites --name "Sitewise" --public-url http://localhost:8000 --mail-file ./outbox.jsonl
aisites serve --sites ./sites
```

People sign up by email with a six-digit code, create a site from a template, and connect their own AI. An
assistant that uses a sign-in flow needs only the site's `mcp` address. Use
`--mail-file` for development; real mail needs the `--smtp-*` options. The details are in
[`docs/hosting.md`](docs/hosting.md).

An account can also sign in with a passkey instead of an emailed code. Once an account has a passkey, approving an
assistant or issuing a key also needs it, so the emailed code alone cannot hand out control. A passkey needs the
`webauthn` extra and an `https://` public address. See [Passkeys](docs/hosting.md#passkeys).

## Limits you should know about

- The signature proves who published the history, not that its dates are true. Independent timestamping
  is not built yet.
- `next_change` does not reveal hidden scheduled changes, so it is a hint, not a guarantee.
- Private data on a shared host is readable by whoever runs that host.
- The framework makes a site self-describing, not findable; there is no directory of sites.
- Lists are filtered in memory, which suits collections of thousands of records, not millions.
- Callers without a key are told apart only by network address, so the per-address quota is weak against someone
  with many addresses until Milestone 4.
- Text cleaning in forms reduces but does not prevent injected instructions.
- Custom actions are not available on a multi-site host.
- A redaction works on one history entry at a time, so each entry carrying the value needs redacting.
- A redaction cannot recall copies that visitors already fetched.
- Real mail delivery for the hosted service was not exercised when it was built. Check the first real send on your host.
- A hosted service operator cannot email site owners, because it keeps only a keyed fingerprint of each address.
- Level 1 (an assistant that only opens links it has been shown) rests on a rule documented for one assistant's
  web fetch tool. No real assistant has yet been run against a site.
- Matching is by words and their beginnings, not by meaning. A paraphrase misses unless the site lists it in `aliases`.
- Search and attaching scan records in memory. That suits small sites, not tens of thousands of records.

## Roadmap

| Milestone | Contents |
|---|---|
| 1 (done) | Structures, kinds, four doors, discovery, time, change feed, history log, many sites per server |
| 2 (done) | Signed history head, checkpoints with a file anchor, verifier with pinning, redaction for legal takedowns, roles limited to structures, fields and operations, role keys, private audit trail |
| 3 (done) | Reserving capacity through bookings, custom actions with payment hand-off, retry safety, quotas, guest keys, message forms, streaming change feed. Webhooks are not built |
| 4 | Key-based identity: an agent proving it acts for a specific user |

### Reader standard

- Done: questions with options, full links, word matching, linked reads and several questions in one request.
  See [`docs/standard.md`](docs/standard.md).

### Hosted service

- Done: sign-up by email, templates, account keys, owner-key recovery, suspension, the sign-in flow for
  assistants that cannot send a key in a header, and passkeys for signing in to the account page and for
  approving sensitive actions ([`docs/hosting.md`](docs/hosting.md)).
- Next: one question across every site on a host; visitor identity.
- Later: Google or Apple sign-in, billing, custom domains, a built-in assistant.

[`docs/scenarios.md`](docs/scenarios.md) maps each target scenario to a milestone.

## Development

```
pip install -e '.[dev]'
pytest
```

MIT licence.
