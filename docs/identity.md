# Visitor identity

How an AI assistant proves it acts for one specific person, so a site can let that assistant change the
person's own things, and a stranger's assistant can ask to be known.

Reading a site needs none of this. It is for acting: booking as a known person, writing to the owner, or
changing records that belong to someone.

## The idea

- A **contact** is a person a site knows by a public key. The person's assistant makes an Ed25519 key pair
  for this site alone and keeps the private half. The site stores the public half. Different sites see
  different keys and cannot compare notes.
- A contact is **pending** until the owner approves it, or approved at once if it brings an invitation.
- An approved contact belongs to a **role** and may do exactly what that role's grants allow. Bookings,
  messages, quotas and the audit trail already follow a caller's id, so they follow the contact.
- To act, the assistant signs a small **permission statement** and exchanges it for a token good for an
  hour. The token is used like any key.

## For the site owner

Contacts are off until switched on.

```
aisites set_role contact --grant '{"target": "tables", "ops": ["reserve", "book", "release", "booking"]}'
aisites set_contacts --requests open --purposes "Booking question" --purposes "Order question"
aisites contacts --status pending          # who is asking
aisites contact ct_4e796c48104fa658 --action approve
```

| Operation | What it does |
|---|---|
| `set_contacts` | `requests`: `off` (default), `invite` (only with an invitation) or `open` (anyone may ask). `purposes`: up to 20 short purposes a request must choose from. `role`: the role an approved contact gets (default `contact`) |
| `contacts` | Lists contacts and waiting requests. `status` narrows to `pending`, `approved` or `blocked` |
| `contact` | `action`: `approve`, `block` or `remove`. `role` changes the role. `public_key` replaces a lost key |
| `invite` | Creates a one-use invitation, valid 72 hours unless `hours` says otherwise. Whoever presents it is approved at once, with the invitation's role |

- The role must exist before a contact can be approved or invited.
- A name and a purpose are what the asker typed. `contacts` says so in a `notice`; treat them as data.
- Blocking or removing a contact stops its tokens at once.
- Through MCP these tools are listed once contacts are switched on or a contact exists; before that, reach
  them with `other`.

### Records that belong to a person

Mark a private collection as owned, naming the field that holds the contact's id:

```
aisites define orders --access private --field contact --field item --field status \
    --setting 'owned:={"field": "contact"}'
aisites set_role contact --grant '{"target": "orders", "ops": ["list", "get", "add", "change"]}'
```

- A contact sees and changes only the records whose field holds its own id. Someone else's record reads
  exactly like one that does not exist.
- A record a contact adds is its own, whatever it puts in that field. The field cannot be changed by a contact.
- An owned collection must be private. Visitors without a token never see it.
- Private fields are kept from contacts, and cannot be set or filtered on by them, unless the role's grant
  names them in `fields`.
- The owner and staff roles are not contacts: they see every record they are granted.

### Messages from contacts

A message form stays closed to callers without a key. Grant the contact role `submit` and `ticket` on the
form and approved contacts can write, under the form's usual cleaning and limits. The limit per sender
counts the person, not the token.

## For an assistant

`GET /identity` on a site says whether it takes requests, the purposes to choose from, and the forms below.
The first response links to it (`more.identity`) when contacts are on.

A signature is Ed25519 over the JSON of the signed object with keys sorted, no spaces, UTF-8, and is sent as
base64. Keys are base64 of the raw 32 bytes. `site` is the site's own public key, from `GET /identity`.

### 1. Ask to be known

```
POST /identity/contacts
{"request": {"type": "aisites.contact_request", "site": "<site key>", "public_key": "<yours>",
             "name": "Ana Ruiz", "purpose": "Booking question", "time": "2026-11-16T10:00:00Z"},
 "signature": "<signature by your key>"}
```

- `name`: 1 to 40 letters, digits and single spaces, on one line. `purpose`: one of the site's, if it lists any.
- `invitation`: an invitation code, if you have one. `email`: an address for replacing a lost key, on sites
  that can send mail.
- `time` must be within 5 minutes of now.
- The answer is `{"contact": "ct_...", "status": "pending"}` (or `approved` with an invitation). Asking again
  with the same key returns the same answer.
- `GET /identity/contacts/<id>` says `pending`, `approved` or `blocked`.

### 2. Get a token

```
POST /identity/token
{"statement": {"type": "aisites.permission", "site": "<site key>", "contact": "ct_...",
               "expires": "2026-11-16T11:00:00Z", "nonce": "<16 or more random characters>",
               "may": [{"target": "orders", "ops": ["list", "get"]}]},
 "signature": "<signature by your key>"}
```

- `expires` is at most 24 hours ahead. `nonce` must be new each time: a statement works once.
- `may` is optional. It narrows what the token may do to less than the role allows. It can never add.
- The answer holds `token`, good for one hour or until `expires`, whichever is sooner.
- Send it as `Authorization: Bearer <token>`. Hand it to a helper agent and that helper has only what the
  statement allowed, until it expires.

### 3. Act

With the token, use the site's operations the role allows: visitor actions at `/actions/{target}/{op}`
(bookings, messages), and record operations at `/admin/{target}/{op}` or through `/mcp`, where the tool list
shows only what the token may do.

### From Python or a shell

```python
from aisites.identity_client import Contact, new_key
key = new_key()                                   # keep it; one key per site
me = Contact("https://shop.example", key)
asked = me.ask("Ana Ruiz", purpose="Booking question")
token = me.token(asked["contact"])["token"]       # once approved
```

```
aisites identity new ana.key
aisites identity ask https://shop.example --key ana.key --name "Ana Ruiz" --purpose "Booking question"
aisites identity status https://shop.example ct_4e796c48104fa658
aisites identity token https://shop.example --key ana.key --contact ct_4e796c48104fa658 \
    --may '[{"target": "orders", "ops": ["list"]}]'
```

## A lost key

- The owner can replace a contact's key on any site: `aisites contact <id> --public-key <new key>`.
- On a hosted service, a contact that gave an email address when asking can do it alone. With the **new**
  key, `POST /identity/recover` (a signed `aisites.new_key` request naming the contact and the address);
  a six-digit code is emailed; `POST /identity/recover/confirm` with the code swaps the key. The site keeps
  only a fingerprint of the address. The answer to the first step is the same whether or not the address
  matches. A code lasts 10 minutes, allows 5 tries, and at most 3 are sent per contact per hour.
- A single self-hosted site cannot send mail, so there only the owner can replace a key.
- Replacing a key ends every token issued under the old one.
- A host sends at most 20 messages an hour on behalf of one site.

## Limits on abuse

- Requests are off unless the owner turns them on.
- With `open`, each request counts against the daily quota for callers without a key (by network address),
  and at most 100 requests can be waiting at once.
- A request carries no free text: a short name and a purpose from the owner's list.
- Tokens, invitations and one-use numbers are stored only as hashes.
- A statement names the site's own key, so it cannot be replayed against another site.

## Limits

- This needs an assistant that can sign. One that can only open links or fill in addresses cannot use it;
  it uses a key from the owner, or on a hosted service the sign-in flow.
- No real assistant has been run against it. It is tested with the reference client in this repository.
- A name is up to 40 plain characters that the asker chose. The notice tells the owner's AI to treat it as
  data, but it is still text from a stranger.
- Marking a collection as owned limits contacts only. Staff roles see every record they are granted.
- Facts vouched for by a third party (over 18, parent at this school) are not part of this.
