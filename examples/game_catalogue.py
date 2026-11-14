"""A game publisher: ten titles, prices per region, and 20% off two titles for one day only.

    python -m examples.game_catalogue ./sites/games && aisites serve ./sites/games
    curl 'localhost:8000/collections/offers?region=MX&format=table'
"""

import sys
from datetime import timedelta
from decimal import Decimal

from aisites import Site

TITLES = ["Starfall", "Iron Meridian", "Paper Kingdoms", "Deep Current", "Night Circuit",
          "Hollow Peak", "Sundial", "Low Orbit", "Glass Harbor", "Ember Road"]
REGIONS = {"US": ("USD", Decimal("1")), "MX": ("MXN", Decimal("17")), "JP": ("JPY", Decimal("150"))}


def build(directory, clock=None):
    site, owner_key = Site.create(directory, name="Northwind Games", description="Catalogue and prices.",
                                  timezone="America/New_York", language="en", currency="USD", clock=clock)
    site.define("genres", type="tree")
    site.define("games", kind="Product", refs={"category": "genres"},
                fields=["release_date:date", "wholesale_cost:decimal:private"])
    site.define("offers", kind="Offer", refs={"product": "games"})

    site["genres"].add({"id": "action", "label": "Action"})
    site["genres"].add({"id": "strategy", "label": "Strategy"})
    site["genres"].add({"id": "racing", "label": "Racing"}, parent="action")

    for i, title in enumerate(TITLES, start=1):
        sku = f"GAME-{i}"
        usd = Decimal("59.99") if i <= 5 else Decimal("39.99")
        site["games"].add(sku=sku, name=title, price=str(usd), price_currency="USD", stock=1000,
                          category="strategy" if i % 2 else "action", wholesale_cost=str(usd / 2))
        for region, (currency, rate) in REGIONS.items():
            price = (usd * rate).quantize(Decimal("1") if currency == "JPY" else Decimal("0.01"))
            site["offers"].add(id=f"{sku}-{region}", product=sku, region=region, price=str(price),
                               price_currency=currency)

    sale_day = (site.now().astimezone(site.tz) + timedelta(days=5)).date().isoformat()
    for sku in ("GAME-3", "GAME-7"):
        full = Decimal(site["games"].get(sku)["data"]["price"])
        site["games"].schedule(sku, price=str((full * Decimal("0.8")).quantize(Decimal("0.01"))), on=sale_day)
    return site, owner_key


if __name__ == "__main__":
    _, key = build(sys.argv[1])
    print("owner key:", key)
