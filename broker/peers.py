"""Announcement store with the spec's supersession semantics.

An announcement supersedes all prior announcements from the same node pubkey,
in full. The store keeps exactly one announcement per pubkey — the one with
the greatest timestamp — and never merges fields across announcements.

An announcement whose timestamp lies more than ``future_skew`` seconds ahead
of local time is ignored (Announcement): under mandatory supersession a single
far-future timestamp would otherwise wedge a pubkey's identity beyond
correction. Every announcement is signature-verified before it is stored;
verification is the caller's job via ``wire.validate`` and is asserted here.
"""

from __future__ import annotations

import time

from .wire import ValidationError, validate


class PeerStore:
    def __init__(self, future_skew: int = 3600, db_path=None):
        """``db_path`` turns on persistence: announcements survive a
        restart (re-validated on load — the disk is not trusted over the
        wire rules), so the broker knows its network from second one
        instead of waiting on the first gossip cycle."""
        self._by_pubkey: dict[str, dict] = {}
        self._future_skew = future_skew
        self._con = None
        if db_path is not None:
            import json
            import sqlite3
            from pathlib import Path

            db_path = Path(db_path)
            db_path.parent.mkdir(parents=True, exist_ok=True)
            self._con = sqlite3.connect(db_path, check_same_thread=False)
            self._con.execute(
                "CREATE TABLE IF NOT EXISTS announcements ("
                "pubkey TEXT PRIMARY KEY, announcement TEXT NOT NULL)")
            self._con.commit()
            for (raw,) in self._con.execute(
                    "SELECT announcement FROM announcements").fetchall():
                try:
                    self.add(json.loads(raw))
                except Exception:
                    pass  # one bad row must not keep the store from loading

    def __len__(self) -> int:
        return len(self._by_pubkey)

    def add(self, announcement: dict, *, now: int | None = None) -> bool:
        """Validate, verify, and store an announcement under supersession.

        Returns True if it became (or remained) the current announcement for
        its pubkey, False if it was ignored (stale, future-dated, or invalid).
        """
        try:
            validate(announcement, "announcement")
        except ValidationError:
            return False

        now = int(time.time()) if now is None else now
        ts = announcement["timestamp"]
        if ts > now + self._future_skew:
            return False

        pubkey = announcement["pubkey"]
        current = self._by_pubkey.get(pubkey)
        if current is not None and current["timestamp"] >= ts:
            # A strictly-greater timestamp supersedes; equal or older does not.
            return current is announcement
        self._by_pubkey[pubkey] = announcement
        if self._con is not None:
            import json

            self._con.execute(
                "INSERT OR REPLACE INTO announcements (pubkey, announcement)"
                " VALUES (?, ?)",
                (pubkey, json.dumps(announcement, separators=(",", ":"))))
            self._con.commit()
        return True

    def ingest_peer_list(self, response: dict, *, now: int | None = None) -> int:
        """Add every embedded announcement that verifies; return the count."""
        added = 0
        for entry in response.get("peers", []):
            if self.add(entry, now=now):
                added += 1
        return added

    def all(self) -> list[dict]:
        return list(self._by_pubkey.values())

    def get(self, pubkey: str) -> dict | None:
        return self._by_pubkey.get(pubkey)

    def indexers(self) -> list[dict]:
        """Current announcements from nodes that declare the indexer role.

        Only protocol version 0: an unrecognized version MAY be relayed but
        MUST NOT be routed on (Extensibility) — propagation and use are
        separate privileges, and this list is what routing uses.
        """
        return [a for a in self._by_pubkey.values()
                if "indexer" in a.get("roles", [])
                and a.get("protocol_version") == 0]
