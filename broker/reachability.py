"""Reachability: "searchable" means recently answered, not announced.

Design note (what it reads, what it writes, why): this module reads two
event streams — gossip's contact attempts (every /corpus pull or announce,
success or failure, via ``note_success``/``note_failure``) and the routed
query outcomes (via ``observe``, subscribed to the OutcomeLog). It writes
nothing anywhere; it publishes one thing: ``status(pubkey)`` —

    reachable    last successful contact within the window, and nothing
                 has failed since
    degraded     contact succeeded within the degraded horizon, but the
                 window has lapsed or the latest attempt failed
    unreachable  attempts exist, none has succeeded within the horizon
    never        no contact has ever been attempted or recorded

Routing consults ``allowed`` (reachable or degraded; never and
unreachable are not selected) and ranks healthy nodes above degraded
ones, so a degraded node is picked only when nothing better covers the
terms. The module is deliberately deletable: with ``reachability=None``
the node routes exactly as before.

The probe is gossip itself — every ``refresh_interval`` it pulls /corpus
from every known indexer, so the window defaults to a few cycles. A
``startup_grace_seconds`` allowance lets a freshly restarted broker (all
nodes "never") route normally until the first cycles land; after the
grace, never-contacted nodes are excluded as the plan requires.

State is in-memory: a restart forgets, the grace covers the gap, and the
first gossip cycle (seconds away) rebuilds it from reality rather than
from history.
"""

from __future__ import annotations

import threading
import time


class Reachability:
    def __init__(self, *, window_seconds: int = 1800,
                 degraded_horizon_seconds: int = 21600,
                 startup_grace_seconds: int = 120):
        self._window = window_seconds
        self._horizon = degraded_horizon_seconds
        self._grace_until = time.time() + startup_grace_seconds
        self._lock = threading.Lock()
        # pubkey -> {last_ok, last_fail, last_error}
        self._contacts: dict[str, dict] = {}

    # -- the two event streams ---------------------------------------------

    def seed(self, pubkey: str, last_ok: float) -> None:
        """Pre-load a past success from persistent evidence at startup.

        Reachability itself is in-memory and resets on restart, but the
        stats cache's last-fetch times and the outcome store's recent
        answers DID persist — so a restart can inherit "we reached these
        recently" instead of pretending the week never happened (which
        made a card read 'not yet contacted' beside its own delivery
        history). Only ever moves a node's last success EARLIER-to-later;
        never overwrites a live success recorded since boot.
        """
        with self._lock:
            entry = self._contacts.setdefault(pubkey, {})
            if last_ok > entry.get("last_ok", 0):
                entry["last_ok"] = last_ok

    def note_success(self, pubkey: str) -> None:
        with self._lock:
            entry = self._contacts.setdefault(pubkey, {})
            entry["last_ok"] = time.time()

    def note_failure(self, pubkey: str, error: str) -> None:
        with self._lock:
            entry = self._contacts.setdefault(pubkey, {})
            entry["last_fail"] = time.time()
            entry["last_error"] = str(error)[:300]

    def observe(self, outcome) -> None:
        """OutcomeLog subscriber: an answered query is a contact success,
        a failed ask is a contact failure. Free evidence — no probe can
        beat the traffic itself."""
        for node in outcome.nodes:
            if node.stage == "responded":
                self.note_success(node.pubkey)
            elif node.stage == "failed":
                self.note_failure(node.pubkey, node.error or "failed")

    # -- the published view --------------------------------------------------

    def status(self, pubkey: str) -> dict:
        now = time.time()
        with self._lock:
            entry = dict(self._contacts.get(pubkey) or {})
        last_ok = entry.get("last_ok")
        last_fail = entry.get("last_fail")
        out = {"last_ok": int(last_ok) if last_ok else None,
               "last_error": entry.get("last_error"),
               "last_error_at": int(last_fail) if last_fail else None}
        if last_ok is None and last_fail is None:
            out["status"] = "never"
        elif last_ok is not None and now - last_ok <= self._window \
                and (last_fail is None or last_fail <= last_ok):
            out["status"] = "reachable"
        elif last_ok is not None and now - last_ok <= self._horizon:
            out["status"] = "degraded"
        else:
            out["status"] = "unreachable"
        return out

    def allowed(self, pubkey: str) -> bool:
        """Whether routing may select this node at all."""
        state = self.status(pubkey)["status"]
        if state == "never":
            return time.time() < self._grace_until
        return state in ("reachable", "degraded")

    def health_rank(self, pubkey: str) -> int:
        """0 = healthy, 1 = degraded: a stable partition key so degraded
        nodes are picked only when nothing better covers the terms."""
        return 0 if self.status(pubkey)["status"] in ("reachable",
                                                      "never") else 1
