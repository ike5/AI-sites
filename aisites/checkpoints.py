"""Signed history head, stored checkpoints and the anchors that receive them.
"""

from __future__ import annotations

import json
from typing import Any

from . import anchors as anchors_mod, signing
from .errors import SiteError
from .ops import OPS, Op, register


class CheckpointMixin:
    # ------------------------------------------------------------ signed head and checkpoints (Milestone 2)
    #
    # The signature proves that the holder of the site key published a head; it does not prove the dates
    # in the history are true. A rewrite is caught only against a head someone saved earlier (a verifier's
    # pin, an anchor's copy). Independent timestamping is left to a later anchor.

    def signed_head(self) -> dict:
        """The current log head, signed now with the site key."""
        key = signing.load_key(self.dir)
        seq, head_hash, _ = self.history.head()
        body = {"seq": seq, "hash": head_hash, "time": self.now_z(), "key": signing.public_key(key)}
        return {"checkpoint": body, "signature": signing.sign(body, key), "algorithm": signing.ALGORITHM}

    def anchor_config(self) -> list:
        return json.loads(self.store.meta("anchors", "[]"))

    def set_anchor_config(self, items: list) -> list:
        """Persist anchors so `checkpoint` uses them through every door. Not part of the public config."""
        anchors_mod.from_config(items)
        self.store.set_meta("anchors", json.dumps(items))
        return items

    @property
    def anchors(self) -> list:
        """Anchors given in Python, then those stored in the site."""
        return self._anchors + anchors_mod.from_config(self.anchor_config())

    def checkpoint(self) -> dict:
        """Store the signed head, hand it to each anchor, and store their proofs beside it."""
        signed = self.signed_head()
        body = signed["checkpoint"]
        # Stored before anchoring, so the checkpoint survives an anchor that hangs or fails.
        self.store.x("INSERT OR REPLACE INTO checkpoints (seq, time, body, signature, proofs) VALUES (?,?,?,?,?)",
                     (body["seq"], body["time"], json.dumps(body, sort_keys=True), signed["signature"], "[]"))
        proofs = []
        for anchor in self.anchors:
            name = getattr(anchor, "name", type(anchor).__name__)
            try:
                proof = anchor.anchor(signed)
            except Exception as e:  # one failing anchor must not cost the others their proofs
                proof = {"error": type(e).__name__}  # public, so no message: it may hold server paths
            if proof is not None:
                proofs.append({"anchor": name, **proof})
        self.store.x("UPDATE checkpoints SET proofs=? WHERE seq=?", (json.dumps(proofs), body["seq"]))
        return {**signed, "proofs": proofs}

    def checkpoints(self, after: Any = 0, limit: Any = 100) -> dict:
        """Stored checkpoints, oldest first."""
        try:
            after, limit = max(0, int(after)), max(1, min(1000, int(limit)))
        except (TypeError, ValueError):
            raise SiteError("bad_page", "after and limit must be whole numbers.")
        rows = self.store.q("SELECT * FROM checkpoints WHERE seq > ? ORDER BY seq LIMIT ?", (after, limit + 1))
        out: dict = {"checkpoints": [{"checkpoint": json.loads(r["body"]), "signature": r["signature"],
                                      "algorithm": signing.ALGORITHM, "proofs": json.loads(r["proofs"])}
                                     for r in rows[:limit]],
                     "as_of": self.now_z()}
        if len(rows) > limit:
            out["next_after"] = rows[limit - 1]["seq"]
        return out

    def head_view(self) -> dict:
        """What GET /history/head serves: the signed head, plus anchor proofs when it is checkpointed."""
        out = {**self.signed_head(), "first_published": self.first_published()}
        row = self.store.one("SELECT seq, time, proofs FROM checkpoints ORDER BY seq DESC LIMIT 1")
        if row and row["seq"] == out["checkpoint"]["seq"]:
            out["proofs"] = json.loads(row["proofs"])
            out["checkpointed"] = row["time"]
        return out

    def checkpoint_tag(self) -> str:
        """Checkpoints change no record, so they are added to the ETag of the routes that show them."""
        row = self.store.one("SELECT COUNT(*) AS n, COALESCE(MAX(time), '') AS t FROM checkpoints")
        return f".h{self.history.head()[0]}.{row['n']}.{row['t']}"


register(
    Op("checkpoint", "Sign the current history head, store it, and send it to the site's anchors. Returns "
                     "the signed checkpoint with each anchor's proof.",
       lambda site: site.checkpoint()),
)
OPS["checkpoint"].positional, OPS["checkpoint"].flags = (), {}
