"""Many sites on one server.

Each site is a folder holding its own database and key. A site is reached at /s/<name>/, or at its own
hostname when `hosts.json` in the sites folder maps one to it, e.g. {"school.example.org": "school"}.
Sites are loaded on first request, so one created while the server runs is picked up without a restart.

Once the folder has been set up with `aisites host init`, every other address is answered by the hosted
service (sign-up, account pages, site creation); see `platform.py`. Without that, nothing changes.
"""

import json
import re
import threading
from pathlib import Path
from typing import Any, Optional

from .api import create_app
from .site import Site, VERSION

SITE_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$")


class Host:
    def __init__(self, sites_dir: Any, clock: Any = None, mailer: Any = None):
        self.dir = Path(sites_dir)
        self.clock = clock
        self.mailer = mailer
        self._apps: dict = {}
        self._lock = threading.Lock()
        self._platform: Any = None
        self._platform_app: Any = None

    @property
    def platform(self) -> Any:
        """The hosted service for this folder, or None if the folder was never set up as a host."""
        from .platform import Platform
        with self._lock:
            if self._platform is None and Platform.enabled(self.dir):
                from .platform_web import create_platform_app
                self._platform = Platform(self.dir, self.clock, self.mailer)
                self._platform_app = create_platform_app(self._platform, self)
            return self._platform

    def names(self) -> list:
        return sorted(p.name for p in self.dir.iterdir() if (p / "site.db").exists()) if self.dir.exists() else []

    def app_for(self, name: str) -> Optional[Any]:
        if not SITE_NAME.match(name):
            return None
        platform = self.platform
        with self._lock:
            if name not in self._apps:
                if not (self.dir / name / "site.db").exists():
                    return None
                site = Site(self.dir / name, self.clock)
                if platform is not None:
                    # Tokens from the host's sign-in flow for assistants count on this site.
                    site.token_resolver = lambda token, n=name: platform.connections.principal_for(token, n)
                self._apps[name] = create_app(site)
            return self._apps[name]

    def _by_hostname(self, hostname: str) -> Optional[str]:
        path = self.dir / "hosts.json"
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text()).get(hostname)
        except (ValueError, OSError):
            return None

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] == "lifespan":
            while True:
                message = await receive()
                if message["type"] == "lifespan.startup":
                    await send({"type": "lifespan.startup.complete"})
                elif message["type"] == "lifespan.shutdown":
                    await send({"type": "lifespan.shutdown.complete"})
                    return
        if scope["type"] != "http":
            return
        path = scope["path"]
        root = scope.get("root_path", "")
        name = None
        parts = path[len(root):].split("/")
        by_path = len(parts) >= 3 and parts[1] == "s"
        if by_path:
            name = parts[2]
            root = f"{root}/s/{name}"
        else:
            hostname = dict(scope.get("headers") or []).get(b"host", b"").decode().split(":")[0].lower()
            name = self._by_hostname(hostname)
        platform = self.platform
        if name is None and platform is not None:
            return await self._platform_app(scope, receive, send)
        if name and platform is not None and platform.is_suspended(name):
            body = json.dumps({"error": {"code": "suspended",
                                         "message": "This site has been suspended by its host."}}).encode()
            await send({"type": "http.response.start", "status": 403,
                        "headers": [(b"content-type", b"application/json")]})
            await send({"type": "http.response.body", "body": body})
            return
        app = self.app_for(name) if name else None
        if app is None:
            body = json.dumps({"error": {
                "code": "not_found", "message": "No site here.",
                "hint": "This server hosts several sites; each lives at /s/<name>/."},
                "aisites": VERSION}).encode()
            await send({"type": "http.response.start", "status": 404,
                        "headers": [(b"content-type", b"application/json")]})
            await send({"type": "http.response.body", "body": body})
            return
        child = dict(scope)
        child["root_path"] = root
        if by_path and platform is not None and platform.config["public_url"]:
            child["aisites.base"] = platform.link(f"/s/{name}")
        if by_path and platform is not None and platform.connections.available():
            child["aisites.resource_metadata"] = platform.connections.resource_metadata_url(name)
        if path == root:  # /s/name with no trailing slash
            child["path"] = root + "/"
        await app(child, receive, send)
