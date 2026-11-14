"""Registries that feature modules plug into, kept in a module nothing else depends on.

SETTINGS maps a structure setting name (e.g. "booking") to a function `(site, sdef, value) -> value` that
checks and normalises it. TIMERS maps the prefix of a timer's `ref` (e.g. "booking" for "booking:<id>") to
a function `(site, timer_row)` run when that time passes, in place of a plain change-feed row.
"""

from typing import Callable, Dict

SETTINGS: Dict[str, Callable] = {}
TIMERS: Dict[str, Callable] = {}
