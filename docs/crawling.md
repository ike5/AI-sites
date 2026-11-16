# The crawl standard

How a search engine finds an AI-sites site, reads all of it, and keeps its copy fresh. Document format: `1`.

A site has no pages for people. Every address answers in JSON (or plain text), whatever a browser asks for,
so there is nothing to index the way web pages are indexed. This page is what a crawler does instead.

[`standard.md`](standard.md) is the other half: how an assistant answering one question reads a site.

## In short

```
GET /.well-known/aisite.json     what the site is, and `use`: what it may be used for
GET /crawl                       everything public, in one feed; follow `next`; keep `cursor`
GET /crawl?since=<cursor>        later: only what changed, with a new cursor
```

## 1. Finding a site

| Address | What it gives |
|---|---|
| `GET /.well-known/aisite.json` (also `/`) | The first response. `use` says what the site may be used for; `more.crawl` is the feed |
| `GET /robots.txt` | Plain text: says this is an AI-site, where to start, and the owner's wishes for crawlers that read only this file |
| `GET /sitemap.xml` | Four addresses per site: the first response, `/llms.txt`, `/map` and `/crawl`, with the time of the last public change |
| Every response | A `Link` header, `rel="describedby"`, pointing at the first response |

On a host that serves many sites:

| Address on the host | What it gives |
|---|---|
| `GET /.well-known/aisites.json` | Every site that may be found: `site`, `name`, `description`, `language`, `url`, `crawl`, `kinds`, `records`, `updated`, `use`. 100 per page, with a `next` link |
| `GET /robots.txt`, `GET /sitemap.xml` | The same as for one site, covering every site, because `robots.txt` only works at the root of an address |

A site is left out of a host's list and sitemap if it is suspended, its owner set `listed=false`, or its
owner turned search off.

## 2. Reading all of a site: `GET /crawl`

```json
{"aisites": "0.1", "format": 1,
 "site": {"name": "Greenleaf Pharmacy", "timezone": "Europe/Madrid", "language": "es", "currency": "EUR",
          "url": "https://site.example", "public_key": "..."},
 "use": {"search": true, "training": true},
 "how": "Read this feed once, following `next` ...",
 "structures": [{"name": "stock", "kind": "Offer", "means": "schema:Offer",
                 "fields": {"product": "schema:itemOffered", "price": "schema:price", "stock": "schema:inventoryLevel"},
                 "refs": {"product": "products", "place": "places"}}],
 "head": {"seq": 21, "hash": "sha256:...", "signed": "https://site.example/history/head"},
 "record_address": "https://site.example/collections/{in}/{key}",
 "records": [{"in": "stock", "key": "ibuprofen-200@norte", "data": {"product": "ibuprofen-200", "stock": 32},
              "updated": "...", "live_as_of": "..."}],
 "cursor": 14, "as_of": "..."}
```

| Part | Meaning |
|---|---|
| `site` | Name, description, time zone, language, currency, address, and the key its history is signed with |
| `use` | What the owner allows; see below |
| `structures` | What the site holds. `means` is the schema.org type. `fields` gives each field's schema.org term, or its type where it has none. `refs` says which structure a field refers to |
| `trees` | Hierarchies such as categories, whole |
| `record_address` | Where any record lives. Put a record's `in` and `key` into it to get an address to cite |
| `records` | Every public record: `in` (its structure), `key`, `data`, and the times that say how fresh it is |
| `head` | The position in the site's signed history this copy was taken at. `signed` is where the signature is |
| `cursor` | Where this read stands in the site's changes. Keep the one from the first page |
| `next` | Present when there is more. Follow it until it is absent |

- **Meaning is stated once**, in `structures`, not in every record. Built-in kinds use the same terms on
  every site.
- **Pages are bounded:** about 200 KB each. `max=<bytes>` asks for smaller ones (not below 2 KB). `site`,
  `structures`, `trees` and `head` come on the first page only.
- **Rules, not moments:** opening hours come as the weekly pattern and the dated closures. "Open now" is
  true for an instant; ask `/ask/Hours` when the answer for a date is wanted.
- **Planned changes** that the owner has announced are in the record (`upcoming_changes`, `next_change`).
  Unannounced ones are not.
- **Times** on a record: `updated` (published values last changed), `confirmed` (the owner said it is still
  true), `live_as_of` (when a live value such as stock was read).
- Only what a visitor could read. Private and invite-only structures, private fields, records outside their
  dates and message forms are absent, whoever asks.

## 3. Staying fresh: `GET /crawl?since=<cursor>`

Only what changed after that cursor.

```json
{"since": 14, "record_address": "...",
 "records": [{"in": "stock", "key": "ibuprofen-200@norte", "data": {"stock": 30}, "updated": "...", "live_as_of": "..."}],
 "removed": [{"in": "stock", "key": "saline-spray@centro"}],
 "head": {...}, "cursor": 17, "as_of": "..."}
```

| Part | Meaning |
|---|---|
| `records` | Records added or changed, in full |
| `removed` | Records that are gone, or no longer visible, by `in` and `key` |
| `structures` | Present when a definition changed. A structure no longer listed is gone, and so are its records |
| `trees`, `site` | Present when a tree or the site's own details changed |
| `cursor` | Send this next time |
| `next` | Present when the list of changes is long. Follow it; each page carries its own `cursor` |

- Nothing changed gives a short answer with the same cursor.
- Changes caused by time alone, such as a notice expiring or a planned change starting, are included.
- Read the feed once, then call `since=` with the cursor from its **first** page. Anything that changed
  while a long first read was under way is caught by that call.
- `ETag` and `If-None-Match` work here as everywhere.

## 4. What the site allows: `use`

| Setting | Meaning | Default |
|---|---|---|
| `search` | The site may be indexed to answer questions and be cited | `true` |
| `training` | The site may be collected to train models | `true` |

The owner sets it with `aisites configure search=true training=false`.

- `use` is in the first response and in the feed.
- `robots.txt` says the same to today's crawlers by the names they use. Training off asks GPTBot, ClaudeBot,
  Google-Extended, CCBot, Applebot-Extended, meta-externalagent and Bytespider to stay out. Search off asks
  OAI-SearchBot, Claude-SearchBot, PerplexityBot, Bingbot, Googlebot, DuckDuckBot and Applebot to stay out.
- With search off, every response also carries `X-Robots-Tag: noindex`, and the site is left out of a
  host's list.
- With both off, `/crawl` answers `403 crawl_off`.
- **An assistant fetching for a person is never turned away.** `robots.txt` always ends with
  `User-agent: *` / `Allow: /`, and never closes `/ask` or addresses with arguments.

## What to index

Addresses that repeat facts found elsewhere answer with `X-Robots-Tag: noindex`: `/ask/...`, `/find`,
`/changes`, `/history`, and any address with arguments. They stay open to fetch. The feed, the first
response, `/map`, `/llms.txt`, each collection and each record's own address do not carry it.

## Checking a copy

`head` names a position in the site's history. `GET /history/head` returns that position signed with the
site's key (`site.public_key`). A search engine that keeps `head` with its copy can later show what the site
said at that point. Verifying the history is described in the README.

## For the site owner

- Nothing to set up: a site is crawlable from the start, with search and training allowed.
- `aisites crawl` shows exactly what a crawler is given. Through MCP, run it with `other`.
- `aisites configure search=false`, `training=false` or `listed=false` to opt out of each.
- If the site's address cannot be told from the request (behind some proxies), set it with
  `aisites configure url=https://your.site`, so the addresses in the feed and sitemap are right.

## Measured

`python tools/measure.py`, taking a copy of each example site:

| Site | Way | Requests | Bytes |
|---|---|---|---|
| School | The crawl feed | 1 | 3,744 |
| | The map, then each collection | 6 | 5,083 |
| | Following question links | 6 | 3,726 |
| Pharmacy | The crawl feed | 1 | 4,600 |
| | The map, then each collection | 5 | 5,595 |
| | Following question links | 12 | 9,538 |
| Game catalogue | The crawl feed | 1 | 9,098 |
| | The map, then each collection | 4 | 9,412 |
| | Following question links | 26 | 19,228 |

Checking again when nothing changed is one request of 340 bytes.

## Limits

- No search engine reads this feed today. It is a proposal, usable by any crawler that can fetch JSON.
- Classic search (Google, Bing) indexes web pages and reads structured data only inside HTML. With no HTML,
  expect little presence there. That is the cost of having no pages for people.
- `use` is a request. It binds only crawlers that choose to honour it.
- The crawler names written into `robots.txt` go out of date and need maintaining (`aisites/crawl.py`).
- Finding a site nobody has linked to or listed is outside this: something has to know the address, or the
  host it is on.
- Telling a search engine the moment something changes (a ping) is not built. A crawler asks with `since=`.
- The feed is built by scanning records in memory. That suits small sites, not tens of thousands of records.
- A host's directory, `robots.txt` and sitemap exist on a hosted service (a folder set up with
  `aisites host init`). A site on its own domain has its own.
- The hosted service's account pages for owners are HTML. They are not site content and carry no site data.
