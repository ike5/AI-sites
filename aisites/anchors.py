"""Anchors: places outside the site's control that receive each signed checkpoint.

The site key can sign any history it likes, including a rewritten one with old dates. An anchor makes a
rewrite visible by holding a copy the site cannot quietly change. `FileAnchor` writes to a file you keep
elsewhere (a git repository, a synced folder); it is only as independent as that file. An independent
timestamping anchor is deliberately left to a later milestone and plugs in through the same `Anchor`
protocol, so stored checkpoints and served proofs keep their shape.
"""

import json
from pathlib import Path
from typing import Any, Optional, Protocol, runtime_checkable

from .errors import SiteError


@runtime_checkable
class Anchor(Protocol):
    name: str

    def anchor(self, checkpoint: dict) -> Optional[dict]:
        """Record a signed checkpoint. Return a JSON-able proof, or None if there is nothing to show."""
        ...


class FileAnchor:
    """Appends each signed checkpoint as one JSON line."""

    name = "file"

    def __init__(self, path: Any):
        self.path = Path(path)

    def anchor(self, checkpoint: dict) -> Optional[dict]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "a+", encoding="utf-8") as f:
            f.seek(0)
            line = sum(1 for _ in f) + 1
            f.write(json.dumps(checkpoint, sort_keys=True, ensure_ascii=False) + "\n")
        # Proofs are served to everyone, so they carry the file's name, never where it sits on the server.
        return {"anchor": self.name, "file": self.path.name, "line": line}


def from_config(items: Any) -> list:
    """Anchors from stored config such as `[{"type": "file", "path": "/srv/anchors/site.jsonl"}]`."""
    out = []
    for item in items or []:
        if isinstance(item, dict) and item.get("type") == "file" and item.get("path"):
            out.append(FileAnchor(item["path"]))
        else:
            raise SiteError("bad_anchor", f"Anchor config {json.dumps(item)} is not understood.",
                            'Use {"type": "file", "path": "<file outside the site folder>"}.')
    return out
