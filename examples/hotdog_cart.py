"""A street cart in Mexico: where it is for the next two days, and a two-for-one day.

The owner never edits files. Their AI calls the same operations through MCP or the admin web API.

    python -m examples.hotdog_cart ./sites/cart && aisites serve ./sites/cart
    curl localhost:8000/collections/notices
"""

import sys
from datetime import timedelta

from aisites import Site


def build(directory, clock=None):
    site, owner_key = Site.create(directory, name="Carrito El Güero",
                                  description="Hot dogs estilo Sonora. Ubicación y promociones del día.",
                                  timezone="America/Tijuana", language="es", currency="MXN", clock=clock)
    site.define("places", kind="Place")
    site.define("notices", kind="Notice", refs={"place": "places"})
    site.define("menu", kind="Product")

    site["places"].add(id="quinta-cabrillo", name="Esquina de Quinta y Cabrillo", city="Tijuana", country="MX")
    site["menu"].add(sku="sonora", name="Hot dog estilo Sonora", price="35.00", price_currency="MXN")
    site["menu"].add(sku="doble", name="Hot dog doble tocino", price="48.00", price_currency="MXN")

    now = site.now().astimezone(site.tz)
    in_two_days = (now + timedelta(days=2)).replace(hour=23, minute=0, second=0)
    site["notices"].add(id="ubicacion", title="Estamos en Quinta y Cabrillo", place="quinta-cabrillo",
                        text="Hoy y mañana, de 18:00 a 23:00.",
                        valid_until=in_two_days.replace(tzinfo=None).isoformat())
    promo_day = (now + timedelta(days=1)).date()
    site["notices"].add(id="dos-por-uno", title="2x1 en hot dogs estilo Sonora",
                        text="Solo este día, todo el turno.",
                        starts=f"{promo_day}T18:00", ends=f"{promo_day}T23:00",
                        valid_until=f"{promo_day}T23:00")
    return site, owner_key


if __name__ == "__main__":
    _, key = build(sys.argv[1])
    print("owner key:", key)
