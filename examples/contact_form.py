"""A small bakery with a contact form that its customers' AIs can write to.

Customers get a key (role `customers`) that may `submit` and `ticket`. The owner's assistant gets an
`inbox` key: it can triage with `inbox`, read one message with `read_message` and answer with `reply`,
and do nothing else.

    python -m examples.contact_form ./sites/bakery && aisites serve ./sites/bakery
    curl localhost:8000/forms/contact
"""

import sys

from aisites import Site


def build(directory, clock=None):
    site, owner_key = Site.create(directory, name="Rosa's Bakery", description="Bread and cakes to order.",
                                  timezone="America/Chicago", currency="USD", clock=clock)
    site.define("contact", type="form", description="Questions about orders and cakes.",
                fields=[{"name": "order_number", "type": "string",
                         "description": "Your order number, e.g. R-1042, if the message is about an order."},
                        {"name": "pickup_date", "type": "date", "description": "The pickup date it concerns."},
                        {"name": "message", "type": "text", "required": True}],
                settings={"form": {"purposes": ["Order question", "Custom cake", "Allergy question", "Other"],
                                   "text_field": "message", "text_max": 600,
                                   "per_sender_per_day": 3, "per_day": 200}})
    site.set_role("customers", [{"target": "contact", "ops": ["submit", "ticket"]}],
                  "Customers' assistants: send a message and check for the reply.")
    site.set_role("inbox", [{"target": "contact", "ops": ["inbox", "read_message", "reply"]}],
                  "The owner's assistant: read and answer contact messages, nothing else.")
    return site, owner_key


if __name__ == "__main__":
    site, key = build(sys.argv[1])
    print("owner key:", key)
    print("customer key:", site.issue_key("customers", "first customer")["secret"])
    print("inbox key:", site.issue_key("inbox", "owner's assistant")["secret"])
