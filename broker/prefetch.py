"""The head start: route a paid query while the payer fetches the preimage.

The broker minted the invoice on its own node, so it can watch that node
and learn of settlement the moment the wallet pays — seconds before the
human finds the preimage and pastes it. This module holds the pending
request server-side from challenge to settlement, starts the route on
settlement, and hands the finished outcome to the redemption that follows.
Authorization is not this module's concern: the redemption still passes the
receiver's credential check (a presented preimage, or the same node's word
for an empty preimage slot); the head start moves work, never trust.

This deliberately bends the broker's statelessness: a bounded, memory-only
map of pending requests, lost harmlessly on restart (redemption then just
routes live). Only single-use credentials take part — a batch spans bodies
and has nothing to prefetch.

States, per payment hash, under one lock:
    waiting  -> settlement not yet seen; claim() cancels and routes live
    routing  -> settlement seen, route in flight; claim() awaits the SAME
                route, so the work (and the indexer payments) happen once
    done     -> outcome held (None if the route raised); claim() takes it
"""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor


class Prefetcher:
    def __init__(self, route, settled, *, poll_interval: float = 2.0,
                 ttl: int = 3600, workers: int = 4):
        """``route(**args) -> RouteOutcome``; ``settled(hash) -> bool``."""
        self._route = route
        self._settled = settled
        self._poll = poll_interval
        self._ttl = ttl
        self._lock = threading.Lock()
        self._entries: dict[str, dict] = {}
        self._pool = ThreadPoolExecutor(max_workers=workers)
        self._stop = threading.Event()
        self._watcher: threading.Thread | None = None

    def register(self, payment_hash: str, args: dict) -> None:
        """Remember a challenged request until it settles, redeems, or ages
        out. ``args`` are the route_query keyword arguments."""
        with self._lock:
            self._entries[payment_hash] = {
                "state": "waiting", "args": args, "outcome": None,
                "event": threading.Event(),
                "expires": time.time() + self._ttl,
            }
            if self._watcher is None:
                self._watcher = threading.Thread(
                    target=self._watch, daemon=True)
                self._watcher.start()

    def claim(self, payment_hash: str, wait_seconds: float = 90.0):
        """The redemption's side of the bargain: take the prefetched
        outcome, await one in flight, or take over an unsettled entry.
        Returns a RouteOutcome or None (meaning: route live yourself)."""
        with self._lock:
            entry = self._entries.get(payment_hash)
            if entry is None:
                return None
            if entry["state"] == "waiting":
                # The preimage outran our poller. Cancel; route live.
                del self._entries[payment_hash]
                return None
            if entry["state"] == "done":
                del self._entries[payment_hash]
                return entry["outcome"]
        # In flight: wait for the same route rather than start a second
        # one — the work and the indexer payments must happen once.
        entry["event"].wait(wait_seconds)
        with self._lock:
            self._entries.pop(payment_hash, None)
        return entry["outcome"] if entry["state"] == "done" else None

    def status(self, payment_hash: str) -> dict:
        """Off-protocol peek for a waiting client's 'payment detected' cue."""
        with self._lock:
            entry = self._entries.get(payment_hash)
            state = entry["state"] if entry else "unknown"
        return {"settled": state in ("routing", "done"),
                "ready": state == "done"}

    def stop(self) -> None:
        self._stop.set()
        self._pool.shutdown(wait=False)

    # -- internals ---------------------------------------------------------

    def _watch(self) -> None:
        while not self._stop.wait(self._poll):
            now = time.time()
            with self._lock:
                stale = [h for h, e in self._entries.items()
                         if e["expires"] < now and e["state"] != "routing"]
                for h in stale:
                    del self._entries[h]
                candidates = [h for h, e in self._entries.items()
                              if e["state"] == "waiting"]
            for payment_hash in candidates:
                try:
                    paid = self._settled(payment_hash)
                except Exception:
                    continue  # a flaky lookup only delays the head start
                if not paid:
                    continue
                with self._lock:
                    entry = self._entries.get(payment_hash)
                    if entry is None or entry["state"] != "waiting":
                        continue  # claimed (or gone) while we looked
                    entry["state"] = "routing"
                self._pool.submit(self._run, payment_hash, entry)

    def _run(self, payment_hash: str, entry: dict) -> None:
        try:
            entry["outcome"] = self._route(**entry["args"])
        except Exception:
            entry["outcome"] = None  # claim() falls back to a live route
        with self._lock:
            entry["state"] = "done"
        entry["event"].set()
