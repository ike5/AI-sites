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

1. The AI calls `POST /register` with the person's email address.
2. The host emails the person a code. The email names the pairing phrase and the confirmation address.
3. The AI tells the person the pairing phrase and where to confirm.
4. The person opens the confirmation page, checks that it shows the same phrase, and enters the code. The page
   says that confirming gives that assistant a key to create and manage their sites.
5. The AI calls `GET /register/{ticket}` with its claim as the bearer token. It gets 202 `pending` until the
   person confirms, then 200 with an `account_key`, once. A second collection is refused.
6. The AI creates a site with `POST /sites`, using the account key.

The pairing phrase protects against someone else's assistant registering your address and you confirming by
reflex. A registration expires after 15 minutes. A code mailed for an assistant cannot sign a browser in, and a
browser sign-in code cannot confirm an assistant.

To connect an AI to a new site, add the site's `mcp` address as a connector and send the owner key as the header
`Authorization: Bearer <owner key>`. Call the `describe` tool first. An AI that uses the web API instead can `GET`
the site's `admin` address, which lists every operation, and call `POST /admin/{target}/{op}` with the same
header.

## Routes

The host serves these at its root, beside the sites at `/s/<name>/`.

| Route | Purpose |
|---|---|
| `GET /` | What this host is and how to register, as JSON |
| `GET /llms.txt` | The same, as Markdown |
| `GET /templates` | The templates, with descriptions |
| `GET /account` | For people: sign in; then list your sites, create one, issue a new owner key for a site, issue or revoke account keys, sign out |
| `POST /register` | For an AI: starts registration with `{"email": "..."}`. The answer has `ticket`, `claim`, `pairing_phrase`, `confirm_at`, `collect_at`, `expires`, and a sentence to tell the person |
| `GET /account/confirm?ticket=...` | For the person: shows the pairing phrase and takes the emailed code |
| `GET /register/{ticket}` | For the AI, with `Authorization: Bearer <claim>`: 202 `pending`, then 200 with `account_key` (starts `ais_acct_`), once |
| `GET /sites`, `POST /sites` | With `Authorization: Bearer <account key>`: list your sites; create one |
| `POST /sites/{name}/key` | With an account key: issue a new owner key for one of your sites. The old one stops working |

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
reserved, for example `account`, `register`, `sites` and `templates`.

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
- **Browser session.** A session lasts 30 days. Its cookie is `HttpOnly`, `SameSite=Lax`, and `Secure` over HTTPS.
  It is limited to the `/account` path, so it is never sent to the sites the host serves. Every form on the
  account page carries a per-session token.

## Limits

- Real mail delivery was not exercised when this was built. The SMTP mailer was tested against a stand-in. Check
  the first real send on your host.
- The operator cannot email site owners, because only a fingerprint of each address is stored.
- An account key can create and manage all of its account's sites. It is shown once. It can be revoked on the
  account page, and it stops working at once.
- An assistant that accepts only a sign-in flow, and cannot send a key in a header, cannot connect yet. That flow
  is the next step and is not built.
- Not built: a built-in assistant; billing and plans; custom domain and TLS set-up (the existing `hosts.json`
  mapping still applies); sign-in for visitors in a browser.
