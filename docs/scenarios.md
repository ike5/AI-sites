# Scenarios

Each scenario the framework is meant to serve, the milestone that delivers it, and the primitives it
uses. A new scenario should be added here and mapped onto existing primitives before anything new is
designed; if it cannot be, that gap is the thing to discuss.

## Delivered

| Scenario | Milestone | Primitives | Example | Test |
|---|---|---|---|---|
| A parent's AI asks whether the school is open tomorrow | 1 | `Hours` + `Closure` kinds, `/hours?date=tomorrow`, site time zone | `examples/school.py` | `test_school_is_it_open_tomorrow` |
| A street cart announces where it is for two days and a promotion day; the owner's AI updates it | 1 | `Notice` kind with `valid_until`, MCP door, site language and currency | `examples/hotdog_cart.py` | `test_hotdog_cart_notices_expire_and_owner_ai_updates` |
| A publisher lists prices per region and discounts two of ten titles for one day | 1 | `Product` + `Offer` kinds, `schedule` (hidden until it starts), sealed history entry, private field | `examples/game_catalogue.py` | `test_game_catalogue_regional_prices_and_one_day_discount` |
| An agent compares prices across several sites | 1 | Kinds with shared field meanings, many sites on one server | `examples/game_catalogue.py` | `test_agent_compares_prices_across_two_sites_on_one_server` |
| A pharmacy chain shows stock at each branch | 1 | `Place` kind, `Offer` with `place`, `capacity` field served with `live_as_of`, range filters | `examples/pharmacy.py` | `test_pharmacy_stock_per_branch` |
| A person keeps a private calendar their AI reads and writes | 1 | `Event` kind, `private` access, zone per event, date-range filters | `examples/personal_calendar.py` | `test_private_calendar_through_the_owners_ai` |
| Ten years of verifiable history; legal takedown without breaking verification | 2 | Signed head, checkpoints, verifier with pinning, redaction of committed values | `examples/game_catalogue.py` | `test_takedown_end_to_end_keeps_history_verifiable` |
| A product manager may change prices but nothing else | 2 | Roles limited to structure, field and operation; role keys; private audit trail | `examples/game_catalogue.py` | `test_product_manager_changes_and_schedules_prices_only` |
| A restaurant: check availability and book a table | 3 | `Slot` kind, `booking` setting, `reserve` / `book` / `release`, holds that expire, retry safety, payment hand-off | `examples/restaurant.py` | `test_restaurant_flow_through_the_web` |
| A child's play times, readable only by invited friends' AIs | 3 | `invite` access, guest key with `read` and booking grants | `examples/hangout.py` | `test_hangout_needs_a_friends_key` |
| A visitor's AI sends the owner a message | 3 (key-holders) | `form` structure, one cleaned text field, `inbox` without text, untrusted envelope, inbox role, single reply | `examples/contact_form.py` | `test_visiting_agent_flow` |
| An agent is told when something changes (snow day, service disruption) | 3 | `/changes/stream` (server-sent events) on top of `/changes` | — | `test_change_made_while_open_is_delivered` |
| Someone signs up on a host and gets a site their own AI builds | Hosted service | Email one-time code, AI-started sign-up, templates, account key, owner-key recovery | — | `test_ai_starts_registration_and_person_confirms` |
| An assistant that only accepts a sign-in flow connects to a site | Hosted service | Standard sign-in flow (OAuth 2.1), approval page, one site per connection, Disconnect | — | `test_whole_flow_from_a_person_who_is_not_signed_in` |
| An agent compares offers across every site on a host in one request | Hosted service | `GET /ask/Offer?q=` on the host, kinds shared across sites, `listed` | — | `test_one_request_asks_every_site` |
| A person signs in with a passkey, and handing out control needs it | Hosted service | Passkeys, confirmation per action, lost-device wait | — | `test_with_a_passkey_handing_out_control_needs_it` |
| An assistant that can only follow links finds where a product is in stock and whether that branch is open | Reader standard | Questions with options, full links, offers attached to place and hours | `examples/pharmacy.py` | `test_level_1_reaches_the_pharmacy_answer_in_three_requests` |
| An assistant asks several things in one request and only gets what changed | Reader standard | `/ask/a,b`, `POST /ask`, ask by kind, `unless=<tag>`, `max` | — | `test_several_questions_in_one_request` |
| An agent changes a user's account on their behalf | Visitor identity | Contact with a key per site, signed permission statement, one-hour token, owned records | — | `test_a_contact_sees_and_changes_only_its_own_records` |
| A stranger's AI sends the owner a message | Visitor identity | Contact request (one fixed form), owner approval or invitation, contact role granted `submit` | — | `test_a_stranger_asks_the_owner_approves_and_the_assistant_acts` |
| A search engine takes a copy of a site and keeps it fresh | Crawl standard | `/crawl` feed, `since=<cursor>`, field meanings stated once, signed head | `examples/pharmacy.py` | `test_since_gives_only_what_changed` |
| An owner keeps a site out of model training but findable in search | Crawl standard | `use`, `configure training=false`, `robots.txt` by crawler name | — | `test_the_owner_states_what_is_allowed` |

## Planned

| Scenario | Milestone | Primitives it will use |
|---|---|---|
| Payment for a booking or purchase | 3 (hand-off, delivered), later (providers) | Action returns a payment hand-off; the framework never sees card data |

## Later, not yet scheduled

| Scenario | What it needs |
|---|---|
| Weekly or monthly repeating events | The existing calendar standard's repeat rules; calendar file import and export |
| Parcel or flight status by reference number | Lookup by secret reference without listing |
| Back-and-forth conversation between two AIs | Threads; each turn is a further injection risk |
| Finding sites in the first place | A directory or search index, outside the framework |
| A search engine is told the moment something changes | Outbound pings tied to an accountable caller |
| Facts vouched for by a third party (parent at this school, over 18) | Credentials from trusted issuers |
| An agent is called back when something changes (webhooks) | Outbound requests tied to an accountable caller |
