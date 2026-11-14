from datetime import datetime, timedelta, timezone

import pytest

from aisites import Site


class Clock:
    """A settable clock, so tests can move a site through time."""

    def __init__(self):
        self.t = datetime(2026, 11, 14, 18, 0, tzinfo=timezone.utc)  # Saturday 10:00 in Los Angeles

    def __call__(self):
        return self.t

    def set(self, *parts):
        self.t = datetime(*parts, tzinfo=timezone.utc)

    def advance(self, **delta):
        self.t += timedelta(**delta)


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def made(tmp_path, clock):
    """(site, owner_key) for an empty site in Los Angeles time."""
    site, key = Site.create(tmp_path / "site", name="Test site", timezone="America/Los_Angeles",
                            currency="USD", clock=clock)
    yield site, key
    site.close()


@pytest.fixture
def site(made):
    return made[0]
