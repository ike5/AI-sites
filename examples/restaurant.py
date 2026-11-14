"""A neighbourhood restaurant whose tables any visiting agent can book.

Each table size at each sitting is a `Slot` record; `remaining` is how many of those tables are free.
Booking is open to callers without a key, with a low limit of active holds per caller.

    python -m examples.restaurant ./sites/restaurant && aisites serve ./sites/restaurant
    curl localhost:8000/bookable/tables
"""

import sys
from datetime import timedelta

from aisites import Site

SITTINGS = ("18:00", "19:30", "21:00")


def build(directory, clock=None):
    site, owner_key = Site.create(directory, name="Trattoria Lucia",
                                  description="Small Italian restaurant. Book a table for tonight or tomorrow.",
                                  timezone="America/Los_Angeles", language="en", currency="USD", clock=clock)
    site.define("places", kind="Place")
    site.define("tables", kind="Slot", refs={"place": "places"},
                description="Tables by sitting. `remaining` is how many tables of that size are free.",
                fields=["seats:integer", "deposit:decimal"],
                settings={"booking": {
                    "field": "remaining", "open": True, "hold_minutes": 10, "max_active_per_caller": 2,
                    "details": ["name!:string", "party_size!:integer", "phone:string", "notes:text"],
                    "payment": {"url": "https://pay.example/trattoria-lucia/{booking}", "amount_field": "deposit"},
                }})
    site["places"].add(id="main", name="Trattoria Lucia", address="412 Alberta St", city="Portland", country="US")
    today = site.now().astimezone(site.tz).date()
    for day in (today, today + timedelta(days=1)):
        for at in SITTINGS:
            for seats, count in ((2, 4), (4, 3)):
                site["tables"].add(id=f"{day}-{at.replace(':', '')}-{seats}", name=f"Table for {seats}, {at}",
                                   start=f"{day}T{at}", place="main", seats=seats, capacity=count,
                                   remaining=count, deposit="10.00")
    return site, owner_key


if __name__ == "__main__":
    _, key = build(sys.argv[1])
    print("owner key:", key)
