# The reader standard

How an AI assistant gets an answer from an AI-sites site, in as few requests and as little text as possible.
This page is the whole of it. Document format: `1` (the `format` number in the first response).

## The idea

A site publishes a short list of **questions**. Each question has one address.

- Open a question with nothing added and it either answers, or lists its **options** as ready-made links.
- Open it with an argument and it **answers**: worked out by the server, with related records attached and
  the times needed to trust them.

The assistant never has to learn how the site stores things. The owner's AI, which does know, sets that up
once.

## The first response

`GET /` (the same document is at `/.well-known/aisite.json`, and every response links to it in a `Link`
header).

```json
{
  "aisites": "0.1", "format": 1,
  "name": "Greenleaf Pharmacy", "timezone": "Europe/Madrid", "language": "es", "currency": "EUR",
  "ask": [
    {"id": "hours", "kind": "Hours", "does": "Open or closed on a date, with the reason and when that next changes",
     "url": "https://site.example/ask/hours", "takes": ["date", "key"],
     "try": ["https://site.example/ask/hours?date=tomorrow"]},
    {"id": "stock", "kind": "Offer", "does": "Stock of each product at each branch.",
     "url": "https://site.example/ask/stock", "takes": ["q", "product", "place", "region"]}
  ],
  "how": ["1 Follow links: ...", "2 Fill in: ...", "3 Combine: ..."],
  "more": {"find": "...", "map": "...", "changes": "...", "history": "..."},
  "first_published": "...", "as_of": "..."
}
```

| Part | Meaning |
|---|---|
| `ask` | The questions. `does` is one line on what it answers, `url` its address, `takes` what may be added, `try` ready links for common forms |
| `kind` | The built-in kind behind a question. The same kind means the same question on every site |
| `needs` | Present when the question needs a key from the site owner |
| `do` | Present when the site lets visitors act (book, send a message); the address lists how |
| `more_questions` | Present when there are more than 12 questions; the address lists them all |
| `more` | Deeper material: search, the map of structures and fields, the change feed, the verifiable history |

The first response lists questions, not fields, so it stays small as a site grows.

## Three levels of assistant

The site does not ask what an assistant is. Using a level's form is the declaration. A level changes the
shape of an answer and never what can be seen.

| Level | The assistant can | How it reads | Requests |
|---|---|---|---|
| 1 | Only open links it has been shown | Opens a question, then one of its options | 3 at most for a site whose options fit one page |
| 2 | Fill in an address | Adds what the question `takes` | 2 |
| 3 | Combine requests | The forms under "Level 3" below | 1 |

Every answer still carries links unless told otherwise, so an assistant that overestimates itself loses one
request, not the task.

### Level 1: following links

```
GET https://site.example/                         -> the list
GET https://site.example/ask/stock                -> {"choose": "product", "options": [{"label": "...", "url": "..."}]}
GET https://site.example/ask/stock?product=...    -> the answer
```

Options come 50 to a page, with a `next` link to the following page. A `tree` structure is walked one level
per request.

### Level 2: filling in

Add anything the question `takes`. Any field of the question's records also works as a filter.

| Added | Meaning |
|---|---|
| `q=words` | Words in the record, or in the name of something it refers to. Case and accents are ignored and a word may be the beginning of a longer one: `ibuprofen` finds `Ibuprofeno` |
| `key=...` | One record, with what it refers to and what refers to it |
| `field=value`, `field.gte` `.gt` `.lte` `.lt` `.ne` | Filters on a field. Dates accept `today` and `tomorrow` |
| `field.has=words` | The same word matching, on one text field |
| `place.city=Madrid` | A filter on a referenced record's field |
| `date=tomorrow` | For hours: a date, `today` or `tomorrow` |

### Level 3: combining

| Form | What it does |
|---|---|
| `GET /ask/hours,stock?date=tomorrow&q=ibuprofen` | Several questions in one request. Each uses the arguments it takes. At most 8 |
| `POST /ask` with `{"ask": [{"id": "stock", "q": "spray"}, {"id": "hours", "key": "norte"}]}` | The same, when the questions need different arguments. Body at most 4 KB. Reading this way changes nothing and needs no key |
| `GET /ask/Offer?q=ibuprofen` | Ask by kind. The address is the same on every site, so the list need not be read first |
| `unless=<tag>` | Every answer carries a `tag`. Sent back, the reply is `{"unchanged": true}` if the answer would be the same |
| `links=0` | Leave links out; options give the value to fill in instead |
| `format=table` | Rows instead of objects, for list answers |
| `max=<bytes>` | Cut a list answer to this size, with a `next` link to the rest |

When several questions are asked at once, one that fails returns its error in place and the others still answer.

## What an answer carries

| Field | Meaning |
|---|---|
| `as_of` | When this answer was produced |
| `updated` | When a record's published values last changed |
| `confirmed` | When the owner last confirmed a record is still true |
| `live_as_of` | When a live value such as a stock count was read |
| `next_change` | When this answer stops being true, where that is known: an announced change, a record expiring, the next opening or closing time |
| `key` | The record's stable identifier, in every record and every table row |
| `with` | Records attached to this one, keyed by the reference followed |
| `tag` | A fingerprint of the answer, for `unless=` |

A time that every record in an answer shares is stated once beside them instead of in each.

What absence means:

| You see | It means |
|---|---|
| `"items": []`, `"total": 0` | The site keeps this, and nothing matches |
| A field missing from a record | The site does not say |
| 404 `not_found` | The site does not offer this. A structure the reader may not see answers exactly the same way |
| 401 `needs_key` | It exists and needs a key from the owner |

Every error has a `code`, a `message` and usually a `hint` saying what to do instead.

## Freshness

Every response has an `ETag` made from the answer itself, so it changes exactly when the answer does,
including answers that change with the clock (`open_now`, anything filtered on `today`). Send it back in
`If-None-Match` to get `304` when nothing changed. `unless=<tag>` is the same idea for an assistant that can
only change the address it asks for.

## What each kind answers

| Kind | Opened with nothing added | With an argument |
|---|---|---|
| Hours | Today's answer for each hours record, with a link for tomorrow | `date`, `key` |
| Offer | The products on offer, as options | Offers with price, stock and when it was read, the product, the place, and the place's hours today |
| Product, Place, Page, Business | Records by name or title, as options. A single record is answered directly | `key`: the record with what it refers to and what refers to it. `q` or a filter: one match in full, several briefly |
| Notice | Notices in force now | `q` and filters |
| Slot, Event | Entries from today onwards | `q` and filters |
| A tree | Its top level, as options | `node`: that node and the next level |
| A collection with no kind | Records by their first text field | As for Product |

Closures have no question of their own: they are part of the hours answer.

## Questions an owner defines

For needs the defaults miss, an owner (or their AI) saves a read as a question, through any door:

```
aisites question in_stock --does "Where a product is in stock right now" --read stock \
    --takes q --takes product --takes place.city --with place --with place.hours \
    --fields stock --fields place.name --options product   stock.gt=0
```

| Part | Meaning |
|---|---|
| `id` | Becomes the address `/ask/<id>` |
| `does` | The one line shown in the list |
| `read` | The collection it reads |
| `where` | Filters always applied |
| `takes` | What the asker may add: `q`, or filter names |
| `with`, `fields`, `sort` | What to attach, which fields to show, the order |
| `options` | A field in `takes` whose values are offered as links when nothing is added |

A question is checked by running it when it is defined, as the least privileged reader it is for. It can
only show what a visitor could already read. The owner's questions are listed first.

## Writing your own reads

The questions run on one general read, which is also open directly. `GET /map` lists every structure with
its fields and where each reference leads, and `GET /collections/{name}` takes the filters above plus:

| Added | Meaning |
|---|---|
| `with=place,place.hours` | Attach referenced records, up to 3 steps and 5 chains. A collection that refers back is attached by its name, at most 20 records per parent |
| `fields=price,place.name` | Choose fields, including inside attached records |
| `sort`, `limit`, `cursor`, `format=table` | Order, paging and compact rows |

`GET /find?q=words` returns up to 10 candidates across the whole site, for when it is unclear where to look.

## What is never shown

Attached records, word matching and questions all go through the same view as a direct read, for the same
reader. Private fields, private structures, hidden records and unannounced scheduled changes stay hidden at
every level, and a filter or search on a private field is answered exactly like one on a field that does not
exist.

## Measured

`python tools/measure.py` builds three example sites and puts the same questions to each four ways. "Before"
is the path needed before questions existed: the old first response, then raw collections. Tokens are
estimated as bytes / 4.

| Question | Path | Requests | Bytes | Tokens (about) |
|---|---|---|---|---|
| School: is it open tomorrow? | before | 2 | 2,822 | 706 |
| | level 1 | 2 | 1,896 | 474 |
| | level 2 | 2 | 1,896 | 474 |
| | level 3 | 1 | 502 | 126 |
| Pharmacy: where is ibuprofen in stock, and is that branch open? | before | 5 | 3,317 | 829 |
| | level 1 | 3 | 2,875 | 719 |
| | level 2 | 2 | 2,083 | 521 |
| | level 3 | 1 | 673 | 168 |
| Games: what does GAME-1 cost in Mexico? | before | 2 | 1,806 | 452 |
| | level 1 | 3 | 2,836 | 709 |
| | level 2 | 2 | 1,544 | 386 |
| | level 3 | 1 | 322 | 80 |

- The "before" path needed an assistant that could build addresses and already knew the record's key. A
  level 1 assistant could not take it at all, which is why level 1 costs more bytes than "before" on the
  games question: it walks a list of options to get there.
- Asking again when nothing changed: 673 bytes in full, 97 with `unless=<tag>`.
- First response: school 2,368 to 1,394 bytes, pharmacy 2,028 to 1,410, games 1,541 to 1,202.

## Limits

- Level 1 exists because some assistants only open addresses they have been shown. That rule is documented
  for Claude's web fetch tool. Other assistants' rules are not confirmed, and no real assistant has yet been
  run against a site.
- Options carry full addresses, about 70 bytes each. A large catalogue is read a page at a time.
- Matching is by words and their beginnings. A paraphrase, or another language's word for the same thing,
  misses unless the site lists it in `aliases` (a field of the Product and Place kinds).
- Questions cover records of one structure, narrowed by their own and referenced fields, with linked records
  attached. Sums and averages are not covered.
- Search and attaching scan records in memory. That suits small sites, not tens of thousands of records.
- `format=table` shows fields of referenced records but not of records that refer back, which can be many.
- The measurements count requests and bytes. They do not show whether a model answers correctly.
