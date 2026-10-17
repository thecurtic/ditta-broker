"""Outcomes: one structured record per routed query, and its subscribers.

(The emit-and-subscribe seam here was previously named "chronicle" — a
plain-English word choice, renamed to avoid any suggestion of the
similarly-named external protocol, which this is not and does not touch.
This is broker-local bookkeeping with no wire format.)

Nothing in the pipeline owns reputation or caching. Instead the pipeline
emits a record of what observably happened — per node: asked, answered,
latency, results returned, results that survived merge, msat paid — and
read-models project over the stream. Reputation is a projection (a running
score routing may consult); caching is a projection (what do I already hold,
how fresh); neither is a pipeline stage, which is why every attempt to place
them "in the pipeline" fails.

The record keeps to observable facts. ``survived`` is a fact about this run
("merge kept N of your URLs"), but note it inherits merge's judgment: a
reputation built on it dies with a stupid merge, while one built on delivery
facts (answered, latency, paid) survives any merge. What to project from the
record is the reader's policy, not the pipeline's.

The reference ships the seam and no readers: subscribe callables see each
outcome after the response is already on its way, and a subscriber's failure
never fails a query.
"""

from __future__ import annotations

from dataclasses import dataclass, field


# The furthest stage an indexer reached for one query. Ordered and
# monotonic per indexer; ``failed`` is terminal (a failed node that was
# paid keeps paid_msat > 0 — the record makes that visible by design).
STAGES = ("considered", "routed", "paid", "responded", "failed")


@dataclass
class NodeOutcome:
    """Observable facts about one indexer the broker considered."""

    pubkey: str
    endpoint: str
    cost_msat: int            # what economics authorized for this ask
    stage: str = "considered"
    transport: str = "clearnet"   # "clearnet" | "tor"
    price_msat: int = 0       # the advertised price routing used
    coverage: float = 0.0     # fraction of query terms its stats cover
    dropped: str | None = None    # why a considered node was not routed
    ok: bool = False
    error: str | None = None  # refusal reason when not ok
    latency_ms: int = 0
    returned: int = 0         # results the node sent back
    survived: int = 0         # of those, how many merge kept (merge's opinion)
    paid_msat: int = 0        # what was actually spent
    payment_hash: str | None = None

    def to_dict(self) -> dict:
        d = {"pubkey": self.pubkey, "endpoint": self.endpoint,
             "transport": self.transport, "stage": self.stage,
             "price_msat": self.price_msat, "paid_msat": self.paid_msat,
             "coverage": round(self.coverage, 3)}
        if self.dropped:
            d["dropped"] = self.dropped
        if self.stage in ("responded", "failed"):
            d["latency_ms"] = self.latency_ms
        if self.error:
            d["error"] = self.error
        if self.stage == "responded":
            d["result_count"] = self.returned
            d["survived"] = self.survived
        if self.payment_hash:
            d["payment_hash"] = self.payment_hash
        return d


@dataclass
class RouteOutcome:
    """The record for one routed query — also the caller's result.

    ``results`` is the merged list (each entry carrying ``sources``);
    ``raw`` holds each responding indexer's set exactly as received, in its
    original order, populated before merge runs and never mutated by it.
    """

    query: str
    pricing_type: str
    request_id: str = ""
    ask: dict = field(default_factory=dict)   # the customer's knobs, echoed
    quote_msat: int = 0
    charged_msat: int = 0
    started_ms: int = 0
    finished_ms: int = 0
    results: list[dict] = field(default_factory=list)
    nodes: list[NodeOutcome] = field(default_factory=list)
    raw: dict = field(default_factory=dict)   # pubkey -> results[] as sent

    @property
    def asked(self) -> list[str]:
        return [n.pubkey for n in self.nodes if n.stage != "considered"]

    @property
    def paid_msat(self) -> int:
        return sum(n.paid_msat for n in self.nodes)

    @property
    def errors(self) -> dict[str, str]:
        return {n.pubkey: n.error for n in self.nodes if n.error}

    def to_dict(self, *, include_raw: bool = False) -> dict:
        d = {"request_id": self.request_id, "query": self.query,
             "ask": self.ask, "quote_msat": self.quote_msat,
             "charged_msat": self.charged_msat,
             "cost_msat": self.paid_msat,
             "started": self.started_ms, "finished": self.finished_ms,
             "indexers": [n.to_dict() for n in self.nodes],
             "merged": self.results}
        if include_raw:
            d["raw"] = self.raw
        return d


class OutcomeLog:
    """A subscription point, not a store. Readers keep their own state."""

    def __init__(self):
        self._subscribers: list = []

    def subscribe(self, callback) -> None:
        """``callback(outcome: RouteOutcome)`` is called after each route."""
        self._subscribers.append(callback)

    def emit(self, outcome: RouteOutcome) -> None:
        for callback in self._subscribers:
            try:
                callback(outcome)
            except Exception:
                pass  # a reader's failure never fails a query


class OutcomeStore:
    """The first OutcomeLog reader: an append-only SQLite table.

    One row per indexer per query, denormalized so the learner, the
    reachability status, and the front page can read it with plain SQL and
    nothing else. Derived signals (merge survival, uniqueness) are written
    here deliberately — this is the experimental broker's store; a
    reference broker records only objective delivery facts.

    ``clicks`` stays NULL until a client reports one; retention is pruned
    on write. Subscribed via ``OutcomeLog.subscribe(store.record)``, so a
    store failure can never fail a query.
    """

    def __init__(self, db_path, *, stats=None, retention_days: int = 90,
                 tokenize=None):
        import sqlite3
        import threading
        from pathlib import Path

        db_path = Path(db_path)
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._con = sqlite3.connect(db_path, check_same_thread=False)
        self._con.executescript(_STORE_SCHEMA)
        self._con.commit()
        self._lock = threading.Lock()
        self._stats = stats
        self._retention_s = retention_days * 86400
        if tokenize is None:
            from .routing import tokenize
        self._tokenize = tokenize

    def record(self, outcome: RouteOutcome) -> None:
        import json
        import time

        from .merge import normalize_url

        terms = self._tokenize(outcome.query)
        now_ms = outcome.started_ms or int(time.time() * 1000)
        # Which of each indexer's merged results nobody else also returned.
        unique: dict[str, int] = {}
        for result in outcome.results:
            sources = result.get("sources") or []
            if len(sources) == 1:
                pk = sources[0].get("pubkey")
                unique[pk] = unique.get(pk, 0) + 1
        rows = []
        for n in outcome.nodes:
            dfs = {}
            if self._stats is not None:
                known = self._stats.terms(n.pubkey)
                dfs = {t: known[t][0] for t in terms if t in known}
            raw = outcome.raw.get(n.pubkey)
            urls = None
            if raw is not None:
                urls = json.dumps(
                    [{"url": r.get("url", ""),
                      "norm": normalize_url(r.get("url", "")),
                      "title": r.get("title", ""),
                      "snippet": r.get("snippet", "")} for r in raw],
                    separators=(",", ":"))
            rows.append((
                outcome.request_id, now_ms, n.pubkey, n.endpoint,
                n.transport, json.dumps(terms, separators=(",", ":")),
                round(n.coverage, 4),
                json.dumps(dfs, separators=(",", ":")),
                n.stage, n.dropped, n.error, n.latency_ms or None,
                n.paid_msat, n.returned, urls, n.survived,
                unique.get(n.pubkey, 0),
            ))
        with self._lock:
            self._con.executemany(
                "INSERT INTO outcomes (query_id, ts, pubkey, endpoint, "
                "transport, terms, predicted_coverage, term_dfs, stage, "
                "skip_reason, error, latency_ms, paid_msat, "
                "results_returned, result_urls, results_in_merged, "
                "unique_results) VALUES "
                "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)
            self._con.execute(
                "DELETE FROM outcomes WHERE ts < ?",
                (int(time.time() * 1000) - self._retention_s * 1000,))
            self._con.commit()

    def add_click(self, query_id: str, url_norm: str, *,
                  cap: float = 10.0) -> bool:
        """Credit a page interaction to the indexers that served the URL.

        Split equally across sources (crediting the first-ranked would
        double-count merge's own opinion). Aggregate only — no user
        identifier is ever recorded; ``cap`` bounds how much any one row
        can accumulate so replayed beacons cannot inflate the learner.
        """
        import json

        with self._lock:
            rows = self._con.execute(
                "SELECT id, result_urls, clicks FROM outcomes "
                "WHERE query_id = ? AND result_urls IS NOT NULL",
                (query_id,)).fetchall()
            matches = []
            for row_id, urls_json, clicks in rows:
                try:
                    urls = json.loads(urls_json)
                except ValueError:
                    continue
                if any(u.get("norm") == url_norm for u in urls):
                    matches.append((row_id, float(clicks or 0)))
            if not matches:
                return False
            share = 1.0 / len(matches)
            credited = False
            for row_id, clicks in matches:
                if clicks >= cap:
                    continue
                self._con.execute(
                    "UPDATE outcomes SET clicks = COALESCE(clicks, 0) + ? "
                    "WHERE id = ?", (share, row_id))
                credited = True
            self._con.commit()
        return credited

    def term_evidence(self, pubkey: str, *, window_days: int = 7,
                      min_failures: int = 2) -> dict:
        """Claimed-vs-demonstrated, per term, from recent delivery.

        A term is DELIVERED when some answered query containing it (which
        the indexer's snapshot claimed) saw this indexer contribute at
        least one merged result; FAILED when it was tried at least
        ``min_failures`` times without ever delivering. Everything else
        the indexer claims is simply untested. This is the display's
        layer-2 evidence and, later, the learner's usefulness signal.
        """
        import json
        import time

        since = int((time.time() - window_days * 86400) * 1000)
        delivered: set = set()
        tries: dict = {}
        answered = 0
        with self._lock:
            cur = self._con.execute(
                "SELECT term_dfs, results_in_merged FROM outcomes "
                "WHERE pubkey = ? AND stage = 'responded' AND ts >= ?",
                (pubkey, since))
            rows = cur.fetchall()
        for dfs_json, merged in rows:
            answered += 1
            claimed = [t for t, df in json.loads(dfs_json).items() if df > 0]
            for t in claimed:
                tries[t] = tries.get(t, 0) + 1
                if merged > 0:
                    delivered.add(t)
        failed = {t for t, n in tries.items()
                  if t not in delivered and n >= min_failures}
        return {"delivered": delivered, "failed": failed,
                "tried": set(tries), "answered": answered}

    def content_profile(self, pubkey: str, *, window_days: int = 90,
                        top: int = 8) -> dict:
        """What this indexer has actually DELIVERED, cumulatively, from the
        stored result URLs — observed content, not claimed vocabulary.

        Returns the observed-result total, how many distinct queries it has
        contributed to, the window's span in days, and its top result
        domains with counts. This is the unforgeable half of a corpus's
        character (an indexer can lie in /corpus, not in what it hands
        back) and the ReDDE foundation: a read-model over the store, read
        by naming and the routing uniqueness signal, written by no one.
        """
        import json
        import time
        from urllib.parse import urlsplit

        since = int((time.time() - window_days * 86400) * 1000)
        with self._lock:
            rows = self._con.execute(
                "SELECT query_id, result_urls FROM outcomes WHERE pubkey = ? "
                "AND stage = 'responded' AND result_urls IS NOT NULL "
                "AND ts >= ?", (pubkey, since)).fetchall()
            span = self._con.execute(
                "SELECT MIN(ts) FROM outcomes WHERE pubkey = ? AND ts >= ?",
                (pubkey, since)).fetchone()
        domains: dict[str, int] = {}
        total = 0
        queries = set()
        for query_id, urls_json in rows:
            try:
                urls = json.loads(urls_json)
            except ValueError:
                continue
            queries.add(query_id)
            for u in urls:
                total += 1
                host = (urlsplit(u.get("url", "")).hostname or "").lower()
                if host.startswith("www."):
                    host = host[4:]
                if host:
                    domains[host] = domains.get(host, 0) + 1
        ranked = sorted(domains.items(), key=lambda kv: (-kv[1], kv[0]))
        observed_since = span[0] if span and span[0] else None
        days = round((time.time() * 1000 - observed_since) / 86400000, 1) \
            if observed_since else 0.0
        return {
            "observed_results": total,
            "distinct_queries": len(queries),
            "observed_days": days,
            "top_domains": [{"domain": d, "count": c}
                            for d, c in ranked[:top]],
        }

    def unique_shares(self, *, window_days: int = 90) -> dict:
        """Per-pubkey lifetime-ish unique-result share: of everything this
        indexer got into merged output, the fraction no co-queried indexer
        also returned. A cumulative delivery signal the router can favor."""
        import time

        since = int((time.time() - window_days * 86400) * 1000)
        with self._lock:
            rows = self._con.execute(
                "SELECT pubkey, SUM(results_in_merged), SUM(unique_results) "
                "FROM outcomes WHERE stage = 'responded' AND ts >= ? "
                "GROUP BY pubkey", (since,)).fetchall()
        out = {}
        for pubkey, merged, unique in rows:
            if merged:
                out[pubkey] = (unique or 0) / merged
        return out

    def last_success_times(self) -> dict:
        """Most recent answered-query timestamp per pubkey, in unix
        SECONDS — persisted evidence to seed reachability on restart so a
        card never reads 'not yet contacted' beside its own history."""
        with self._lock:
            rows = self._con.execute(
                "SELECT pubkey, MAX(ts) FROM outcomes "
                "WHERE stage = 'responded' GROUP BY pubkey").fetchall()
        return {pubkey: ts / 1000.0 for pubkey, ts in rows if ts}

    def rows(self, **where):
        """Thin read helper for tests and read-models: dict rows, newest
        first, filtered by exact match on any column."""
        clause = " AND ".join(f"{k} = ?" for k in where) or "1=1"
        with self._lock:
            cur = self._con.execute(
                f"SELECT * FROM outcomes WHERE {clause} ORDER BY id DESC",
                tuple(where.values()))
            names = [d[0] for d in cur.description]
            return [dict(zip(names, row)) for row in cur.fetchall()]


_STORE_SCHEMA = """
CREATE TABLE IF NOT EXISTS outcomes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    query_id TEXT NOT NULL,
    ts INTEGER NOT NULL,              -- unix ms, the route's start
    pubkey TEXT NOT NULL,
    endpoint TEXT NOT NULL,
    transport TEXT NOT NULL,          -- 'clearnet' | 'tor'
    terms TEXT NOT NULL,              -- JSON array, as tokenized for routing
    predicted_coverage REAL NOT NULL,
    term_dfs TEXT NOT NULL,           -- JSON {term: df} from the snapshot used
    stage TEXT NOT NULL,              -- considered|routed|paid|responded|failed
    skip_reason TEXT,
    error TEXT,
    latency_ms INTEGER,
    paid_msat INTEGER NOT NULL,
    results_returned INTEGER NOT NULL,
    result_urls TEXT,                 -- JSON [{url,norm,title,snippet}]
    results_in_merged INTEGER NOT NULL,
    unique_results INTEGER NOT NULL,
    clicks INTEGER                    -- NULL until a client reports one
);
CREATE INDEX IF NOT EXISTS idx_outcomes_pubkey_ts ON outcomes(pubkey, ts);
CREATE INDEX IF NOT EXISTS idx_outcomes_query ON outcomes(query_id);
"""
