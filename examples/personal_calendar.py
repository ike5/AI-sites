"""A private personal calendar, read and written by the owner's own AI.

    python -m examples.personal_calendar ./sites/me
    aisites add calendar title="Bridget" start=2026-11-23T14:00 timezone=America/Los_Angeles -s ./sites/me
    aisites list calendar start.gte=today --sort start -s ./sites/me
"""

import sys

from aisites import Site


def build(directory, clock=None):
    site, owner_key = Site.create(directory, name="My calendar", timezone="America/New_York", clock=clock)
    site.define("calendar", kind="Event", access="private")
    return site, owner_key


if __name__ == "__main__":
    _, key = build(sys.argv[1])
    print("owner key:", key)
