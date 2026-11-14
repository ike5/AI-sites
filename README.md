# AI-sites

A free, open-source, self-hosted framework for sites that are read and used by AI agents.

More and more visits to a site come from an AI acting for a person. AI-sites serves that visitor first:
typed data it can read cheaply, a clear statement of how fresh each fact is, a public history it can look
back through, and one small set of operations for whoever maintains the site, human or AI.

**Status: Milestones 1 and 2 of 4.** Reading, discovery, time, the four editing doors and multi-site hosting work
and are tested. Signed history with a verifier, redaction, and roles with an audit trail now work too. Actions and
identity come next; see [Roadmap](#roadmap).

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

`site create` prints an owner key once. The web admin API and remote MCP need it; the command line,
Python and local MCP work on the site folder directly.

Five complete example sites are in [`examples/`](examples): a school, a street cart, a game catalogue
with regional prices and a one-day discount, a pharmacy chain with stock per branch, and a private
personal calendar. Each runs with `python -m examples.<name> <folder>`.

## The model

Everything is built from three structures and one set of operations. The full guide is
[`aisites/authoring.md`](aisites/authoring.md), also printed by `aisites guide`.

| Structure | What it is | Human equivalent |
|---|---|---|
| Collection | Records sharing a schema, looked up by key | Table |
| Tree | Nested nodes with ids | Hierarchy, menu |
| Choice | A field whose value must come from a collection or tree | Dropdown |

| Level | Operations |
|---|---|
| Site | `describe`, `configure` |
| Structure | `define`, `alter`, `drop` |
| Record or node | `add`, `get`, `list`, `change`, `remove`, `move` |
| Time | `schedule`, `unschedule`, `confirm`, `hours` |
| History | `redact`, `checkpoint` |
| Access | `set_role`, `drop_role`, `issue_key`, `revoke_key`, `list_keys`, `audit` |

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

### Roles and the audit trail

A role is a name with a list of grants. A grant names a structure (or `*` for all), the operations allowed
on it, and optionally the only fields it may touch:

```json
{"target": "games", "ops": ["get", "list", "change", "schedule", "unschedule"], "fields": ["price"]}
```

Roles can be granted `add`, `get`, `list`, `change`, `remove`, `move`, `schedule`, `unschedule`, `confirm`
and `describe`. Every other operation is for the owner only. With a field list, `change` and `schedule` may
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
or the manifest. Read it with `aisites audit --who <key id, owner or local> --target <structure> --limit 20`.

## What a visiting agent gets

| Route | Purpose |
|---|---|
| `GET /.well-known/aisite.json` (also `/`) | Manifest: every structure with its kind, schema link and access |
| `GET /llms.txt` | The same overview as Markdown |
| `GET /collections/{name}` | Records. Filters `field=value`, `.gte .gt .lte .lt .ne`; `sort`, `limit`, `cursor`, `fields`, `format=table` |
| `GET /collections/{name}/{key}` | One record with `updated`, `confirmed`, `live_as_of`, `next_change` |
| `GET /collections/{name}/schema` | JSON Schema with `x-type`, `x-property`, `x-history`, `x-choices` |
| `GET /trees/{name}` | A tree; `node=` for a subtree, `depth=` to limit it |
| `GET /hours?date=` | Open or closed on a date (`today`, `tomorrow`, `YYYY-MM-DD`) |
| `GET /changes?since=` | What changed since a cursor |
| `GET /history`, `/collections/{name}/{key}/history` | Past values, paged; never loaded unless asked for |
| `GET /history/head` | Current head of the history, signed with the site key; anchor proofs once checkpointed |
| `GET /history/checkpoints` | Stored checkpoints, paged with `after` and `limit` |
| `GET /openapi.json` | Generated from the site's structures |

- Reads are never rate limited. Every response has an ETag and answers `If-None-Match` with 304.
- Every response links to the manifest, so an agent landing anywhere can find the entry point.
- HTML is returned only when `Accept` asks for it; it is a plain rendering of the same data.
- Errors share one shape: `{"error": {"code", "message", "hint"}}`.

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

## Limits you should know about

- The signature proves who published the history, not that its dates are true. Independent timestamping
  is not built yet.
- `next_change` does not reveal hidden scheduled changes, so it is a hint, not a guarantee.
- Private data on a shared host is readable by whoever runs that host.
- The framework makes a site self-describing, not findable; there is no directory of sites.
- Lists are filtered in memory, which suits collections of thousands of records, not millions.
- Invite-only structures are readable on the public routes only with the owner key until guest keys
  arrive in Milestone 3. A role granted `get` or `list` can read them through the admin door.
- A redaction works on one history entry at a time, so each entry carrying the value needs redacting.
- A redaction cannot recall copies that visitors already fetched.

## Roadmap

| Milestone | Contents |
|---|---|
| 1 (done) | Structures, kinds, four doors, discovery, time, change feed, history log, many sites per server |
| 2 (done) | Signed history head, checkpoints with a file anchor, verifier with pinning, redaction for legal takedowns, roles limited to structures, fields and operations, role keys, private audit trail |
| 3 | Reserving capacity (bookings, stock), custom actions, retry safety, quotas, guest keys, push notifications, message forms |
| 4 | Key-based identity: an agent proving it acts for a specific user |

[`docs/scenarios.md`](docs/scenarios.md) maps each target scenario to a milestone.

## Development

```
pip install -e '.[dev]'
pytest
```

MIT licence.
