# Hosted service

The engine lets AI agents read, edit and act on a site, and it can be self-hosted on its own, as the
[README](../README.md) describes. The hosted service is an optional layer on top. A host operator runs one
server. Anyone can sign up by email, create a site from a template, and build it with their own AI. It is the
same code, so anyone else can run their own host.

## Set up a host

A sites folder becomes a host with `aisites host init`. Without that, the multi-site server behaves exactly as
before. Running `host init` again changes the settings it is given and keeps the ones it is not given.

Development, with mail written to a file:

```
aisites host init ./sites --name "Sitewise" --public-url http://localhost:8000 --mail-file ./outbox.jsonl
aisites serve --sites ./sites
```

`--mail-file` writes each email as one line of JSON to that file instead of sending it, so the sign-in code
can be read with `tail -1 ./outbox.jsonl`. `--public-url` is the address used in emailed links; for a local
trial it must be the address the server is reached at.

Real mail:

```
aisites host init ./sites --name "Sitewise" --public-url https://sites.example.org \
  --smtp-host smtp.resend.com --smtp-port 587 --smtp-user resend \
  --smtp-sender hello@yourdomain.example --smtp-tls starttls
aisites serve --sites ./sites
```

The Resend values are an example. Check them against Resend's own documentation, or use the values that any
other SMTP provider gives you.

| Option | Meaning |
|---|---|
| `--name` | The host's name |
| `--public-url` | The public address of the host |
| `--sites-per-account N` | How many sites one account may create. Default 3 |
| `--mail-file PATH` | Write emails to a file instead of sending them (development) |
| `--smtp-host`, `--smtp-port`, `--smtp-user` | The SMTP server that sends the mail |
| `--smtp-sender ADDRESS` | The sender address. Required with `--smtp-host` |
| `--smtp-tls` | `starttls` (default), `ssl` or `none` |

`aisites serve --sites` also takes `--host` and `--port`, as for any served site.

The mail password is never written to a settings file. It is read from the `AISITES_SMTP_PASSWORD` environment
variable, or from a file named `smtp.password` in the sites folder.

## How people sign up

A person signs up on the account page:

1. Open `/account` on the host and enter your email address. A six-digit code is sent to it.
2. Enter the code. Signing up, signing in and recovering access are the same flow. There are no passwords.
3. On the account page, create a site from a template, or open one of your sites.

An AI can start the sign-up for a person:

1. The AI calls `POST /signup` with the person's email address.
2. The host emails the person a six-digit code and the address of the confirmation page.
3. The AI tells the person to open that page (the `confirm_at` address in the answer) and enter the code.
4. The person opens the page. It says that confirming gives that assistant a key to create and manage their
   sites. The person enters the code.
5. The AI calls `GET /signup/{ticket}` with its claim as the bearer token. It gets 202 `pending` until the
   person confirms, then 200 with an `account_key`, once. A second collection is refused.
6. The AI creates a site with `POST /sites`, using the account key.

Only someone who can read the email gets the code, and the email and the page both say what confirming grants.
If the person did not ask an assistant to do this, they should do nothing. A registration expires after 15
minutes. A code mailed for an assistant cannot sign a browser in, and a browser sign-in code cannot confirm an
assistant.

To connect an AI to a new site, add the site's `mcp` address as a connector and send the owner key as the header
`Authorization: Bearer <owner key>`. Call the `describe` tool first. An AI that uses the web API instead can `GET`
the site's `admin` address, which lists every operation, and call `POST /admin/{target}/{op}` with the same
header.

## One question across every site

A host answers one question for every site it serves: `GET /ask/Offer?q=ibuprofen` returns the matching
offers from each site in one request, and `GET /find?q=words` searches them all. `GET /` lists the kinds
that can be asked. Each site is asked as a visitor would ask it. The details are in
[`docs/standard.md`](standard.md#across-a-host).

- A site's owner can leave their site out: `aisites configure listed=false`.
- Suspended sites are left out.
- To switch it off for the whole host, set `"ask_across_sites": false` in `platform.json` in the sites folder
  and restart.
- Each site is read in turn, 50 per request. This suits tens of sites.

## Being found

The host's directory (`GET /.well-known/aisites.json`), `robots.txt` and sitemap cover every site it serves.
A site is left out if it is suspended, or its owner set `listed=false` or `search=false`. Each site's wishes
about training and search are written into the host's `robots.txt` by path (`/s/<name>/`). The details are in
[`crawling.md`](crawling.md).

## Passkeys

A passkey lets a person sign in to the account page without an emailed code. The sign-in page has a "Sign in with a
passkey" button, and no email address is typed. The emailed code still works as a way in.

Because email stays a way in, an account that has a passkey needs it for everything that hands out control. Signed in
by email alone, these are refused with 403 `needs_passkey`:

- approving an assistant on the consent page (Deny never needs a passkey)
- issuing an account key
- issuing a new owner key for a site
- adding or removing a passkey
- confirming an AI-started sign-up. The confirmation page then asks for the passkey as well as the emailed code

Signed in by email alone, a person can still see the account, create a site, revoke keys, disconnect assistants and
sign out. The JSON API used with an account key, such as `POST /sites/{name}/key`, is not gated. An account key
already gives that control.

A passkey confirmation is for one named action, in one browser session. It is good for 2 minutes and one use.

### Adding and removing

On the account page, under "Passkeys", give a passkey a name and add it. An account may have at most 10. The list
shows each passkey's name, when it was added, when it was last used, and a Remove button.

### If you lose your passkey device

Signed in by email, open "I have lost my passkey" on the account page, enter the account's email address, and ask
for the passkeys to be removed. Removal happens 24 hours later. A notice is emailed at once. Using any passkey
before then cancels the removal, and so does "Cancel the removal" on the account page. The address entered must
be the account's own. The wait relies on email, so this path is only as strong as the person's mailbox.

### What the host must have

- The `webauthn` library, an optional extra: `pip install 'aisites[passkeys]'`.
- The public address (`--public-url`) must be `https://` with no path. `http://localhost:<port>` and
  `http://127.0.0.1:<port>` also work, for trying it out. This is the same condition as the sign-in flow for
  assistants.
- If either is missing, passkeys are off. The account page says why, and everything else works as before.
- Passkeys need JavaScript in the browser. The account pages that offer a passkey include one small inline script,
  allowed by a per-page nonce in the `Content-Security-Policy` header. Other pages send `script-src 'none'`. The
  emailed code is the path that works without scripts.

### How passkeys are kept

- Only the public half of a passkey is stored.
- A passkey belongs to the host's public address. Changing `--public-url` makes existing passkeys unusable. People
  sign in by email and add them again.
- Passkey sign-in challenges are limited to 60 an hour per network address. A challenge lasts 5 minutes and is good
  for one answer.
- Tested with a software passkey device in the test suite, and with Chromium's built-in virtual passkey device
  against a local server (add, sign out, sign in with it, issue a key, approve an assistant). Not tested with real
  phones or security keys.

## Connecting an assistant that uses a sign-in flow

Some assistants, ChatGPT on the web for example, cannot send a key in a header. They connect through a standard
sign-in flow instead (OAuth 2.1, as the Model Context Protocol specifies it).

The person gives the assistant only the site's MCP address: `https://<host>/s/<name>/mcp`. The assistant sends
the person to the host. The person signs in with their emailed code (or a passkey) and sees a page that names the
assistant, where it will return to, and the site. They press Allow or Deny. If the account has a passkey, pressing
Allow asks for it.

Allow gives that assistant full control of that one site, the same power as the site's owner key. It gets no
access to the person's other sites or their account. If the assistant names no site, the person chooses one of
their own sites on the page.

The assistant's name on the page is the assistant's own claim, and the host cannot check it. The address the
assistant returns to is checked against the one it registered.

### Managing it

- The account page has a "Connected assistants" list: name, site, when connected, when last used, and a
  Disconnect button. Disconnect stops the assistant at once.
- Changes made by a connected assistant appear in the site's private audit trail as `assistant:<connection id>`,
  with the role `owner`.
- Suspending a site stops its connected assistants too.

### What the host must have

- The flow is on only when the host's public address (`--public-url` of `aisites host init`) is an `https://`
  address with no path. `http://localhost:<port>` and `http://127.0.0.1:<port>` also work, for trying it out.
  Otherwise the flow is off. `aisites host init` says so, and sites keep working with keys in a header.
- Changing the public address needs a restart of `aisites serve`.
- A self-hosted single site (served without `--sites`) is unchanged: keys in a header, no sign-in flow.
- The flow works at a site's `/s/<name>/mcp` address on the host's public address. It is not offered on a site's
  own hostname mapped through `hosts.json`.

### Lifetimes

| Item | Lifetime |
|---|---|
| Approval request | 10 minutes |
| Authorization code | 2 minutes, works once |
| Access token | 1 hour |
| Refresh token | Replaced every time it is used. Lapses after 30 days without use. An assistant that did not ask to renew gets none |

Tokens and codes are stored only as hashes.

### Endpoints

| Route | Purpose |
|---|---|
| `GET /.well-known/oauth-authorization-server` | The host's discovery document |
| `GET /.well-known/oauth-protected-resource/s/{name}/mcp` | Tells an assistant which host signs people in for that site |
| `GET /authorize` | Where the assistant sends the person. Leads to the approval page |
| `POST /token` | The assistant exchanges its code for a token, or renews one |
| `POST /register` | The assistant registers itself, with its name and return addresses. At most 20 an hour per network address. Registrations never used are removed after 7 days |
| `POST /revoke` | The assistant ends its own connection |
| `GET /account/authorize?request=...` | For the person: the approval page |

An unauthenticated request to a site's MCP address gets 401 with a `WWW-Authenticate` header that names the
site's discovery document. That is how an assistant finds the flow.

### Assistants identified by an address

An assistant may identify itself by the `https://` address of a document it publishes, instead of registering.
The host fetches that document with these guards: https only, public network addresses only, no redirects
followed, a 5 second limit, a 64 KB limit, and the document must name the same address. The result is kept for
a day.

The operator can turn this off with `aisites host init <folder> --client-metadata-urls off`.

Known limit: the fetch connects directly and does not use an outbound proxy. On a server that reaches the
internet only through a proxy, these assistants cannot be identified this way. Self-registration still works.

## Routes

The host serves these at its root, beside the sites at `/s/<name>/`.

| Route | Purpose |
|---|---|
| `GET /` | What this host is and how to register, as JSON. `sites_directory` gives the list of its sites |
| `GET /llms.txt` | The same, as Markdown |
| `GET /templates` | The templates, with descriptions |
| `GET /ask` | The kinds that can be asked across the host's sites, with how many sites answer each |
| `GET /ask/{Kind}` | One question on every site the host serves, such as `GET /ask/Offer?q=ibuprofen`; see [One question across every site](#one-question-across-every-site) |
| `GET /find?q=` | A few matching records from each site |
| `GET /.well-known/aisites.json` | The directory of the sites that may be found; see [Being found](#being-found) |
| `GET /robots.txt`, `GET /sitemap.xml` | For crawlers, covering every site on the host |
| `GET /account` | For people: sign in; then list your sites, create one, issue a new owner key for a site, issue or revoke account keys, disconnect connected assistants, add or remove passkeys, sign out |
| `POST /signup` | For an AI: starts sign-up with `{"email": "..."}`. The answer has `ticket`, `claim`, `expires`, `confirm_at`, `collect_at`, `tell_the_person`, and `next` |
| `GET /account/confirm?ticket=...` | For the person: takes the emailed code |
| `GET /signup/{ticket}` | For the AI, with `Authorization: Bearer <claim>`: 202 `pending`, then 200 with `account_key` (starts `ais_acct_`), once |
| `GET /sites`, `POST /sites` | With `Authorization: Bearer <account key>`: list your sites; create one |
| `POST /sites/{name}/key` | With an account key: issue a new owner key for one of your sites. The old one stops working |
| `POST /account/passkey/options`, `POST /account/passkey/session` | Starts a sign-in with a passkey, then finishes it and starts a session |
| `POST /account/passkey/confirm/options`, `POST /account/passkey/confirm` | Presents a passkey for one named action, such as approving an assistant |
| `POST /account/passkeys/options`, `POST /account/passkeys` | Adds a passkey |
| `POST /account/passkeys/{id}/remove` | Removes one passkey |
| `POST /account/passkeys/reset`, `POST /account/passkeys/reset/cancel` | Starts the lost-device removal of passkeys, or cancels it |
| `POST /account/confirm/options` | The passkey step on the confirmation page for an AI-started sign-up |

`POST /sites` takes `name`, `template`, `title`, `timezone`, `language`, `currency` and `description`. It answers
201 with the site's addresses (`site`, `manifest`, `mcp`, `admin`), the structures it created, and its
`owner_key`. The owner key is shown once.

An account key cannot see or re-key another account's sites; they look absent. An account key is not a site key.

`POST /sites/{name}/key` is the recovery path for a lost owner key.

## Templates

| Name | Title | Creates |
|---|---|---|
| `blank` | Blank site | nothing |
| `school` | School or office hours | places, hours, closures, notices, pages |
| `notice_board` | Notice board | places, notices, menu |
| `shop` | Shop catalogue | categories, products |
| `restaurant` | Restaurant with bookable tables | places, tables, hours |
| `contact` | Contact form | contact (a form, with `customers` and `inbox` roles) |
| `calendar` | Private calendar | calendar |

A template is a list of ordinary operations, so it can do nothing an owner could not do by hand. Sample content
uses dates relative to the day the site is created.

Templates also work when self-hosting: `aisites site create ./school --name "Lincoln" --template school`.
`aisites templates` lists them.

A site name is 3 to 63 lowercase letters, digits, `-` or `_`, starting with a letter or digit. Some names are
reserved, for example `account`, `register`, `signup`, `authorize`, `token`, `revoke`, `sites` and `templates`.

## Running it

Operator commands take the sites folder as their first argument.

- **Accounts.** `aisites host accounts ./sites` lists accounts by fingerprint, with their sites. The host does not
  know anyone's email address.
- **Suspension.** `aisites host suspend ./sites <site>` makes the site answer 403 `suspended` on every route.
  `aisites host unsuspend ./sites <site>` restores it. Only sites created through sign-up can be suspended this way.
- **Limit per account.** `--sites-per-account` sets how many sites one account may create. Default 3.

Files in the sites folder:

| File | Contents |
|---|---|
| `platform.db` | The service's own small database |
| `platform.json` | Settings, written by `aisites host init` |
| `platform.key` | The secret used for fingerprints |
| `smtp.password` | Optional mail password, if `AISITES_SMTP_PASSWORD` is not set |
| `hosts.json` | Optional hostname mapping, as for any multi-site server |
| one folder per site | Each site, with its own database, as before |

## Sign-in and privacy

- **Codes.** A code is six digits and is sent by email. It is valid for ten minutes and works once. It is void
  after five wrong attempts. A newer code replaces an older one.
- **Rate limits.** At most 5 codes per email address per hour, and 20 per network address per hour. Beyond that
  the answer is 429 with `Retry-After`.
- **Email addresses.** An address is used once, to send the code. It is then kept only as a keyed fingerprint.
  The operator therefore cannot email site owners.
- **Lost keys on sites.** A contact of a site on this host that gave an email address can replace a lost key with a
  six-digit code, emailed through this host's mail settings. The site keeps only a fingerprint of the address. See
  [`identity.md`](identity.md).
- **Browser session.** A session lasts 30 days. Its cookie is `HttpOnly`, `SameSite=Lax`, and `Secure` over HTTPS.
  It is limited to the `/account` path, so it is never sent to the sites the host serves. Every form on the
  account page carries a per-session token.

## Limits

- Real mail delivery was not exercised when this was built. The SMTP mailer was tested against a stand-in. Check
  the first real send on your host.
- The operator cannot email site owners, because only a fingerprint of each address is stored.
- An account key can create and manage all of its account's sites. It is shown once. It can be revoked on the
  account page, and it stops working at once.
- The sign-in flow for assistants has been tested with the MCP library's own client, which follows the same
  standard, and with a real browser playing the person. It has not been tested against the real ChatGPT or Claude
  apps. Check the first real connection on a public host.
- The sign-in flow needs the host to be reachable over HTTPS at its public address.
- A connected assistant has the same power over that site as the owner key, until it is disconnected. There is
  not yet a way to connect an assistant with a limited role.
- Passkeys exist (see [Passkeys](#passkeys)). They belong to the host's public address, and the lost-device path
  relies on email. Google or Apple sign-in is not built.
- Not built: a built-in assistant; billing and plans; custom domain and TLS set-up (the existing `hosts.json`
  mapping still applies); sign-in for visitors in a browser.
