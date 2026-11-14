"""A pharmacy chain: the same products, with stock and hours per branch.

    python -m examples.pharmacy ./sites/pharmacy && aisites serve ./sites/pharmacy
    curl 'localhost:8000/collections/stock?product=ibuprofen-200&stock.gt=0'
"""

import sys

from aisites import Site


def build(directory, clock=None):
    site, owner_key = Site.create(directory, name="Greenleaf Pharmacy", description="Two branches in Madrid.",
                                  timezone="Europe/Madrid", language="es", currency="EUR", clock=clock)
    site.define("places", kind="Place")
    site.define("hours", kind="Hours", refs={"place": "places"})
    site.define("products", kind="Product")
    site.define("stock", kind="Offer", refs={"product": "products", "place": "places"},
                description="Stock of each product at each branch.")

    site["places"].add(id="centro", name="Greenleaf Centro", address="Calle Mayor 12", city="Madrid", country="ES")
    site["places"].add(id="norte", name="Greenleaf Norte", address="Av. de Burgos 80", city="Madrid", country="ES")
    site["hours"].add(id="centro", place="centro",
                      weekly={d: [["09:00", "21:00"]] for d in ("mon", "tue", "wed", "thu", "fri", "sat")})
    site["hours"].add(id="norte", place="norte",
                      weekly={d: [["09:30", "14:00"], ["17:00", "20:30"]] for d in ("mon", "tue", "wed", "thu", "fri")})

    site["products"].add(sku="ibuprofen-200", name="Ibuprofeno 200 mg, 20 comprimidos", price="3.45",
                         price_currency="EUR")
    site["products"].add(sku="saline-spray", name="Spray nasal salino 100 ml", price="6.90", price_currency="EUR")
    for place, levels in {"centro": {"ibuprofen-200": 0, "saline-spray": 14},
                          "norte": {"ibuprofen-200": 32, "saline-spray": 5}}.items():
        for sku, count in levels.items():
            site["stock"].add(id=f"{sku}@{place}", product=sku, place=place, stock=count)
    return site, owner_key


if __name__ == "__main__":
    _, key = build(sys.argv[1])
    print("owner key:", key)
