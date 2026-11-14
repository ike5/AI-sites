"""A child's play times, bookable only by friends' agents that hold a guest key.

The structure is invite-only and booking is not open: a parent issues each friend's family a key whose
role may read the play times and reserve, book, release and check a place.

    python -m examples.hangout ./sites/hangout
"""

import sys
from datetime import timedelta

from aisites import Site

FRIEND_OPS = ["read", "reserve", "book", "release", "booking"]


def build(directory, clock=None):
    site, owner_key = Site.create(directory, name="Mia's play times",
                                  description="When Mia can have friends over. Friends' families only.",
                                  timezone="America/Los_Angeles", language="en", clock=clock)
    site.define("playtimes", kind="Slot", access="invite",
                description="Afternoons Mia is free. `remaining` is how many friends can still come.",
                settings={"booking": {"field": "remaining", "open": False, "hold_minutes": 30,
                                      "max_active_per_caller": 2,
                                      "details": ["child!:string", "grown_up!:string", "pickup:string"]}})
    site.set_role("friends", [{"target": "playtimes", "ops": FRIEND_OPS}], description="Families of Mia's friends.")
    today = site.now().astimezone(site.tz).date()
    for offset, place in ((1, "our house"), (3, "the park on Elm St"), (6, "our house")):
        day = today + timedelta(days=offset)
        site["playtimes"].add(id=str(day), name=f"Play time at {place}", start=f"{day}T15:30",
                              end=f"{day}T17:30", capacity=3, remaining=3)
    return site, owner_key


def friend_key(site, label=""):
    """A key for one friend's family. The secret is shown once."""
    return site.issue_key("friends", label=label)["secret"]


if __name__ == "__main__":
    s, key = build(sys.argv[1])
    print("owner key:", key)
    print("friend key:", friend_key(s, "first friend"))
