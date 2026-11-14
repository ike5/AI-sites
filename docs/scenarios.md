# Scenarios

Each scenario the framework is meant to serve, the milestone that delivers it, and the primitives it
uses. A new scenario should be added here and mapped onto existing primitives before anything new is
designed; if it cannot be, that gap is the thing to discuss.

## Delivered in Milestone 1

| Scenario | Primitives | Example | Test |
|---|---|---|---|
| A parent's AI asks whether the school is open tomorrow | `Hours` + `Closure` kinds, `/hours?date=tomorrow`, site time zone | `examples/school.py` | `test_school_is_it_open_tomorrow` |
| A street cart announces where it is for two days and a promotion day; the owner's AI updates it | `Notice` kind with `valid_until`, MCP door, site language and currency | `examples/hotdog_cart.py` | `test_hotdog_cart_notices_expire_and_owner_ai_updates` |
| A publisher lists prices per region and discounts two of ten titles for one day | `Product` + `Offer` kinds, `schedule` (hidden until it starts), sealed history entry, private field | `examples/game_catalogue.py` | `test_game_catalogue_regional_prices_and_one_day_discount` |
| An agent compares prices across several sites | Kinds with shared field meanings, many sites on one server | `examples/game_catalogue.py` | `test_agent_compares_prices_across_two_sites_on_one_server` |
| A pharmacy chain shows stock at each branch | `Place` kind, `Offer` with `place`, `capacity` field served with `live_as_of`, range filters | `examples/pharmacy.py` | `test_pharmacy_stock_per_branch` |
| A person keeps a private calendar their AI reads and writes | `Event` kind, `private` access, zone per event, date-range filters | `examples/personal_calendar.py` | `test_private_calendar_through_the_owners_ai` |

## Planned

| Scenario | Milestone | Primitives it will use |
|---|---|---|
| Ten years of verifiable history; legal takedown without breaking verification | 2 | Signed head, checkpoints, verifier, redaction of committed values |
| A product manager may change prices but nothing else | 2 | Roles limited to structure, field and operation; private audit trail |
| A restaurant: check availability and book a table | 3 | `Slot` kind, reserve/release on `capacity`, retry safety, holds that expire |
| A child's play times, readable only by invited friends' AIs | 3 | `invite` access with guest keys |
| An agent is told when something changes (snow day, service disruption) | 3 | Webhooks and a streaming feed on top of `/changes` |
| A visitor's AI sends the owner a message | 3 (invited), 4 (identified) | Owner-defined form, one capped text field, untrusted labelling, restricted inbox role |
| An agent changes a user's account on their behalf | 4 | Key-based identity with a signed, time-limited permission |
| Payment for a booking or purchase | 3 (hand-off), later (providers) | Action returns a payment hand-off; the framework never sees card data |

## Later, not yet scheduled

| Scenario | What it needs |
|---|---|
| Weekly or monthly repeating events | The existing calendar standard's repeat rules; calendar file import and export |
| Parcel or flight status by reference number | Lookup by secret reference without listing |
| Back-and-forth conversation between two AIs | Threads; each turn is a further injection risk |
| Finding sites in the first place | A directory or search index, outside the framework |
| Facts vouched for by a third party (parent at this school, over 18) | Credentials from trusted issuers |
