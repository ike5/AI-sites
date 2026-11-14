"""AI-sites: a self-hosted framework for sites that are read and used by AI agents."""

from .actions import PaymentHandoff
from .errors import SiteError
from .site import Handle, Site, VERSION

__all__ = ["Site", "Handle", "SiteError", "PaymentHandoff", "VERSION"]
__version__ = "0.1.0"
