"""A school publishing its hours, so a parent's AI can answer "is it open tomorrow?".

    python -m examples.school ./sites/school && aisites serve ./sites/school
    curl 'localhost:8000/hours?date=tomorrow'
"""

import sys
from datetime import timedelta

from aisites import Site

WEEK = {day: [["08:00", "15:00"]] for day in ("mon", "tue", "wed", "thu", "fri")}


def build(directory, clock=None):
    site, owner_key = Site.create(directory, name="Lincoln Elementary School",
                                  description="Hours, closures and notices for Lincoln Elementary.",
                                  timezone="America/Los_Angeles", language="en", clock=clock)
    site.define("places", kind="Place")
    site.define("hours", kind="Hours", refs={"place": "places"})
    site.define("closures", kind="Closure", refs={"hours": "hours"})
    site.define("notices", kind="Notice")
    site.define("pages", kind="Page")

    site["places"].add(id="main", name="Main campus", address="120 Lincoln Ave", city="Portland", country="US")
    site["hours"].add(id="school", name="School day", place="main", weekly=WEEK)
    site["hours"].add(id="office", name="Front office", place="main",
                      weekly={day: [["07:30", "16:30"]] for day in WEEK})

    today = site.now().astimezone(site.tz).date()
    training = today + timedelta(days=3)
    site["closures"].add(id="staff-training", hours="school", start_date=training.isoformat(),
                         reason="Staff training day, no classes")
    site["pages"].add(slug="attendance", title="Attendance policy",
                      body="Call the front office before 08:30 if your child will be absent.")
    return site, owner_key


if __name__ == "__main__":
    _, key = build(sys.argv[1])
    print("owner key:", key)
