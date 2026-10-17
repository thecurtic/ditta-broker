"""The learner: per-indexer profiles projected from the outcome store,
and the density-based routing scorer that consumes them.

Design note (what it reads, what it writes, why): the learner reads ONE
thing — rows from the OutcomeStore (plus cached /corpus snapshots via the
StatsCache for densities) — and publishes two: ``profile(pubkey)`` (the
cards' learned stats) and ``scorer(query, indexers, stats)`` (a drop-in
for the node's ``rank`` seam, returning (score, announcement) pairs best
first). It writes nothing anywhere: profiles are a pure projection,
recomputed from the store on a refresh interval, so the module stays
deletable — remove it and routing falls back to coverage ranking, the
cards drop their learned line, and nothing else changes.

The scorer is a smoothed query-likelihood over corpus term DENSITIES —
p_i(t) = df_i(t)/N_i, blended with a general-English prior for unreported
terms — so a query dense in one corpus routes there even when a web-scale
generalist technically "covers" every term. On top of the density:

  - calibration discounts an indexer whose claimed statistics have not
    been converting into delivered results,
  - per-(indexer, term) Beta posteriors adjust terms the store has actual
    delivery evidence for (clicks weight successes; aggregate only, never
    per-user), sampled Thompson-style so exploration is built in,
  - reliability (EWMA of answered-vs-failed) multiplies the whole score.

Sampling is DETERMINISTIC PER (query, hour): the RNG is seeded from the
query text and a time bucket, so the quote a customer saw and the route
their payment buys make the same draws — Thompson exploration must never
be the reason a quote and an invoice disagree inside the hour.
"""

from __future__ import annotations

import hashlib
import json
import math
import random
import threading
import time

from . import routing
from .stats import _GENERAL_DF, _GENERAL_FLOOR


class Learner:
    def __init__(self, store, *, window_days: int = 7,
                 refresh_seconds: int = 300, density_lambda: float = 0.8,
                 ewma_alpha: float = 0.2):
        self._store = store
        self._window_days = window_days
        self._refresh_s = refresh_seconds
        self._lambda = density_lambda
        self._alpha = ewma_alpha
        self._lock = threading.Lock()
        self._profiles: dict[str, dict] = {}
        self._refreshed_at = 0.0

    # -- the projection ----------------------------------------------------

    def refresh(self, *, force: bool = False) -> None:
        if not force and time.time() - self._refreshed_at < self._refresh_s:
            return
        since = int((time.time() - self._window_days * 86400) * 1000)
        try:
            with self._store._lock:
                cur = self._store._con.execute(
                    "SELECT pubkey, transport, stage, latency_ms, term_dfs, "
                    "results_in_merged, unique_results, results_returned, "
                    "clicks FROM outcomes WHERE ts >= ? AND stage IN "
                    "('responded', 'failed') ORDER BY id", (since,))
                rows = cur.fetchall()
        except Exception:
            return  # a store hiccup only delays the refresh
        profiles: dict[str, dict] = {}
        for (pubkey, transport, stage, latency, dfs_json, merged, unique,
             returned, clicks) in rows:
            p = profiles.setdefault(pubkey, {
                "reliability": None, "reliability_by": {},
                "latencies": [], "answered": 0, "failed": 0,
                "merged": 0, "unique": 0,
                "terms": {},            # term -> [success_weight, failures]
            })
            ok = 1.0 if stage == "responded" else 0.0
            p["reliability"] = ok if p["reliability"] is None else \
                (1 - self._alpha) * p["reliability"] + self._alpha * ok
            by = p["reliability_by"]
            by[transport] = ok if transport not in by else \
                (1 - self._alpha) * by[transport] + self._alpha * ok
            if stage == "responded":
                p["answered"] += 1
                if latency:
                    p["latencies"].append(latency)
                p["merged"] += merged or 0
                p["unique"] += unique or 0
                weight = 1.0 + min(float(clicks or 0), 5.0)
                for term in json.loads(dfs_json):
                    entry = p["terms"].setdefault(term, [0.0, 0])
                    if (merged or 0) > 0:
                        entry[0] += weight
                    else:
                        entry[1] += 1
            else:
                p["failed"] += 1
        for p in profiles.values():
            lat = sorted(p.pop("latencies"))
            p["latency_p50"] = lat[len(lat) // 2] if lat else None
            p["latency_p90"] = lat[int(len(lat) * 0.9)] if lat else None
            tried = len(p["terms"])
            delivered = sum(1 for sw, _f in p["terms"].values() if sw > 0)
            p["calibration"] = delivered / tried if tried else None
        with self._lock:
            self._profiles = profiles
            self._refreshed_at = time.time()

    def profile(self, pubkey: str) -> dict | None:
        """The card view of what the learner knows, or None without data."""
        self.refresh()
        with self._lock:
            p = self._profiles.get(pubkey)
            if p is None or (p["answered"] + p["failed"]) == 0:
                return None
            total = p["answered"] + p["failed"]
            return {
                "success_rate": round(p["answered"] / total, 3),
                "queries": total,
                "latency_p50_ms": p["latency_p50"],
                "latency_p90_ms": p["latency_p90"],
                "unique_share": round(p["unique"] / p["merged"], 3)
                if p["merged"] else None,
                "calibration": (
                    "good" if p["calibration"] is None
                    or p["calibration"] >= 0.6
                    else "fair" if p["calibration"] >= 0.3 else "poor"),
            }

    # -- the scorer (a drop-in for the node's rank seam) -------------------

    def rng_for(self, query: str, salt: str = "") -> random.Random:
        """Deterministic per (query, hour): quote and route agree."""
        bucket = int(time.time() // 3600)
        seed = int.from_bytes(hashlib.sha256(
            f"{query}|{bucket}|{salt}".encode()).digest()[:8], "big")
        return random.Random(seed)

    def _density(self, term: str, terms: dict, doc_count: int) -> float:
        prior = _GENERAL_DF.get(term, _GENERAL_FLOOR)
        df = terms.get(term, (0, 0))[0]
        observed = df / doc_count if doc_count > 0 else 0.0
        return self._lambda * observed + (1 - self._lambda) * prior

    def scorer(self, query: str, indexers: list[dict], stats) -> list:
        """Best-first (score, announcement) pairs.

        score_i = reliability_i × Π_t [ calibration_i · density_i(t) ]
                  × Π_{t with evidence} clip(θ_t / 0.5)
        with θ_t ~ Beta(1 + successes, 1 + failures) sampled per query-hour.
        """
        self.refresh()
        rng = self.rng_for(query)
        terms = routing.tokenize(query)
        with self._lock:
            profiles = dict(self._profiles)
        scored = []
        for ann in indexers:
            pubkey = ann["pubkey"]
            doc_count = (ann.get("corpus") or {}).get("document_count", 0)
            known = stats.terms(pubkey) if stats is not None else {}
            p = profiles.get(pubkey)
            cal = 1.0
            if p and p.get("calibration") is not None:
                cal = 0.25 + 0.75 * p["calibration"]
            log_score = 0.0
            for t in terms:
                log_score += math.log(
                    max(1e-9, cal * self._density(t, known, doc_count)))
                ev = (p or {}).get("terms", {}).get(t)
                if ev and (ev[0] + ev[1]) > 0:
                    theta = rng.betavariate(1 + ev[0], 1 + ev[1])
                    log_score += math.log(
                        min(2.0, max(0.2, theta / 0.5)))
            reliability = p["reliability"] if p and p.get("reliability") \
                is not None else 0.8   # mild optimism for the unknown
            log_score += math.log(max(0.05, reliability))
            # Cumulative delivery history: favor indexers whose merged
            # results have tended to be ones nobody else returned — they
            # bring something new, not more of the same.
            if p and p.get("merged"):
                unique_share = p["unique"] / p["merged"]
                log_score += math.log(0.7 + 0.6 * min(1.0, unique_share))
            scored.append((log_score, ann))
        scored.sort(key=lambda pair: (-pair[0],
                                      pair[1].get("pubkey", "")))
        return scored
