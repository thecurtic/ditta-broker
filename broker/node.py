"""The broker: the pipeline caller, with payment wrapping the entrance.

The pipeline is three stages, each behind a one-sentence contract, injectable
so the swappability test can replace any one with a deliberately stupid
alternative and prove nothing else degrades:

    routing.rank      query + corpus statistics → ranked list of nodes
    economics.allocate  that list + prices + ceiling → who to ask, how many each
    merge.merge_results result sets → one ordered list

Payment is not a stage. Taking payment wraps the entrance (a broker with no
receiver is simply free — the query path never calls a stub), and making
payment executes inside each ask, spending exactly what economics authorized.
Discovery feeds the pipeline from outside it, and every route emits one
outcome record of what observably happened.
"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from . import economics, merge, messages, routing
from . import stats as stats_module
from .outcomes import NodeOutcome, OutcomeLog, RouteOutcome
from .client import IndexerClient, IndexerError
from .identity import Identity
from .payment import L402Receiver
from .peers import PeerStore
from .stats import StatsCache

_NO_CEILING = 9007199254740991  # quote wants the full allocation, uncapped


def _is_onion(announcement: dict) -> bool:
    from urllib.parse import urlsplit

    host = urlsplit(announcement.get("endpoint", "")).hostname or ""
    return host.endswith(".onion")


@dataclass(frozen=True)
class Quote:
    """Routing evaluated early: what a query would spend downstream, now."""

    downstream_msat: int
    indexer_count: int
    stats_age_seconds: int | None
    asked: tuple = ()          # pubkeys the route would ask, merit order
    skipped: tuple = ()        # (pubkey, reason) for selected-but-not-asked


class BrokerNode:
    def __init__(self, identity: Identity, peers: PeerStore,
                 client: IndexerClient, receiver: L402Receiver | None = None,
                 fanout: int = 3, *, stats: StatsCache | None = None,
                 rank=routing.rank, allocate=economics.allocate,
                 merge=merge.merge_results,
                 outcome_log: OutcomeLog | None = None,
                 reachability=None, learner=None,
                 routing_policy: dict | None = None):
        self.identity = identity
        self.peers = peers
        self.client = client
        self.receiver = receiver
        self.fanout = fanout
        self.stats = stats
        self.rank = rank
        self.allocate = allocate
        self.merge = merge
        self.outcome_log = outcome_log or OutcomeLog()
        # Optional, deletable (see reachability.py): when present, routing
        # selects only nodes it has recently reached, prefers healthy over
        # degraded, and the outcome records why anyone was left out.
        self.reachability = reachability
        if reachability is not None:
            self.outcome_log.subscribe(reachability.observe)
        self.learner = learner
        self.routing_policy = dict(routing_policy or {})
        import inspect

        try:
            merge_params = inspect.signature(merge).parameters
            self._merge_rich = all(k in merge_params
                                   for k in ("query", "affinities"))
        except (TypeError, ValueError):
            self._merge_rich = False

    # -- the pipeline ------------------------------------------------------

    def _candidates(self, exclude_onion: bool,
                    scope=None) -> list[dict]:
        """The indexers this request may consider.

        ``scope`` is the searcher's selection (a set of pubkeys, or None
        for the full network). It narrows the candidate set and nothing
        else: within it the broker alone ranks, allocates and merges — a
        searcher constrains WHO may be asked, never the order or weight
        (search-and-selection.md, principle 2). Onion exclusion is a
        per-request client preference (a latency choice) and reachability
        is an observed fact (never routed to a node that has not recently
        answered) — all three applied BEFORE rank so routing and quoting
        see the same world; none is a judgment the price-blind rank makes.
        """
        indexers = self.peers.indexers()
        if scope:
            indexers = [a for a in indexers if a["pubkey"] in scope]
        if exclude_onion:
            indexers = [a for a in indexers if not _is_onion(a)]
        if self.reachability is not None:
            indexers = [a for a in indexers
                        if self.reachability.allowed(a["pubkey"])]
        return indexers

    def _not_candidate_reason(self, ann: dict, exclude_onion: bool,
                              scope=None) -> str:
        if scope and ann["pubkey"] not in scope:
            return "out_of_scope"
        if exclude_onion and _is_onion(ann):
            return "excluded"
        state = self.reachability.status(ann["pubkey"])["status"] \
            if self.reachability is not None else "reachable"
        return {"never": "never_contacted"}.get(state, state)

    def _select(self, query: str, max_results: int, pricing_type: str,
                ceiling: int, exclude_onion: bool, scope=None):
        """One selection for quote and route alike: rank (scored or
        plain), health ordering, threshold allocation, and seeded
        exploration — deterministic within the hour so the quote a
        customer saw and the route their payment buys agree."""
        raw = self.rank(query, self._candidates(exclude_onion, scope),
                        self.stats)
        scores = None
        if raw and isinstance(raw[0], tuple):
            scores = {ann["pubkey"]: sc for sc, ann in raw}
            ranked = [ann for _sc, ann in raw]
        else:
            ranked = list(raw)
        ranked = self._order_by_health(ranked)
        skip_odds = float(self.routing_policy.get("skip_odds", 10.0))
        try:
            asks = self.allocate(
                ranked, pricing_type, ceiling, max_results,
                can_pay=self.client.can_pay, fanout=self.fanout,
                scores=scores, skip_odds=skip_odds)
        except TypeError:   # an injected allocate with the plain contract
            asks = self.allocate(
                ranked, pricing_type, ceiling, max_results,
                can_pay=self.client.can_pay, fanout=self.fanout)
        asks = self._maybe_explore(query, ranked, asks, pricing_type,
                                   max_results, ceiling)
        return ranked, scores, asks

    def _maybe_explore(self, query, ranked, asks, pricing_type,
                       max_results, ceiling):
        """Thompson keeps exploration inside the score; this floor is the
        guarantee under it — occasionally one extra reachable candidate is
        asked so nobody starves. Seeded per (query, hour): exploration can
        never make a quote and its invoice disagree."""
        p = float(self.routing_policy.get("exploration", 0.0))
        if p <= 0 or self.learner is None or not ranked:
            return asks
        rng = self.learner.rng_for(query, salt="explore")
        if rng.random() >= p:
            return asks
        chosen = {a.pubkey for a in asks}
        extras = []
        for ann in ranked:
            if ann["pubkey"] in chosen:
                continue
            cost = economics.query_cost(ann, pricing_type, max_results)
            if cost is None or cost > ceiling:
                continue
            if cost > 0 and not self.client.can_pay:
                continue
            extras.append((ann, cost))
        if not extras:
            return asks
        ann, cost = rng.choice(extras)
        return asks + [economics.Ask(ann, max_results, cost)]

    def _order_by_health(self, ranked: list[dict]) -> list[dict]:
        """Healthy nodes above degraded ones, merit order preserved within
        each — so a degraded node is selected only when nothing better
        covers the terms (the fanout cut falls on it last)."""
        if self.reachability is None:
            return ranked
        return sorted(ranked, key=lambda a:
                      self.reachability.health_rank(a["pubkey"]))

    def _coverage(self, query: str, pubkey: str) -> float:
        """Fraction of the query's terms this indexer's cached stats cover."""
        terms = routing.tokenize(query)
        if not terms or self.stats is None:
            return 0.0
        known = self.stats.terms(pubkey)
        return len([t for t in terms if t in known]) / len(terms)

    def _low_score_or(self, ann: dict, scores: dict | None,
                      pricing_type: str, max_results: int,
                      ceiling: int) -> str:
        if scores:
            import math

            best = max(scores.values())
            odds = float(self.routing_policy.get("skip_odds", 10.0))
            if scores.get(ann["pubkey"], best) < best - math.log(odds):
                return "low_score"
        return self._dropped_reason(ann, pricing_type, max_results, ceiling)

    def _dropped_reason(self, ann: dict, pricing_type: str, max_results: int,
                        ceiling: int) -> str:
        cost = economics.query_cost(ann, pricing_type, max_results)
        if cost is None:
            return "no_pricing"
        if cost > ceiling:
            return "budget"
        if cost > 0 and not self.client.can_pay:
            return "pay_disabled"
        return "fanout"

    def _skip_reason(self, ann: dict, considered_pks: set, scores,
                     pricing_type: str, max_results: int, ceiling: int,
                     exclude_onion: bool, scope) -> str:
        """Why an indexer was not asked: left out before rank (scope, Tor,
        reachability) or dropped after it (score, price, fanout)."""
        if ann["pubkey"] not in considered_pks:
            return self._not_candidate_reason(ann, exclude_onion, scope)
        return self._low_score_or(ann, scores, pricing_type, max_results,
                                  ceiling)

    def route_query(self, query: str, max_results: int, pricing_type: str,
                    max_price: int, *, exclude_onion: bool = False,
                    request_id: str = "", ask_echo: dict | None = None,
                    quote_msat: int = 0,
                    charged_msat: int = 0, scope=None) -> RouteOutcome:
        """Rank, allocate, ask-and-pay, merge — and log the outcome.

        ``max_price`` is the downstream spend ceiling. Each ask is signed
        under the broker's identity; a refusal by one indexer never fails
        the whole route. The outcome records every indexer CONSIDERED —
        including those routing evaluated and dropped, with the reason —
        plus the raw per-indexer result sets before merge touched them.
        ``scope`` narrows the candidate set (see ``_candidates``); indexers
        outside it are recorded as dropped "out_of_scope".
        """
        scope = frozenset(scope) if scope else None
        started_ms = int(time.time() * 1000)
        ranked, scores, asks = self._select(
            query, max_results, pricing_type, max_price, exclude_onion,
            scope)

        outcome = RouteOutcome(
            query=query, pricing_type=pricing_type, request_id=request_id,
            ask=dict(ask_echo or {}), quote_msat=quote_msat,
            charged_msat=charged_msat, started_ms=started_ms)

        def ask_one(ask, node: NodeOutcome):
            """One indexer's ask, run concurrently: the route's wall time is
            the slowest indexer's, never the sum — a Tor circuit beside a
            LAN indexer must not stack their timeouts."""
            query_msg = self.identity.signed(
                messages.query_request(
                    query, ask.max_results, pricing_type, ask.cost_msat,
                    pubkey=self.identity.pubkey),
                "query_request")
            receipt: dict = {}
            started = time.monotonic()
            try:
                result_set = self.client.query(
                    ask.endpoint, query_msg, pricing_type, ask.cost_msat,
                    indexer_pubkey=ask.pubkey, receipt=receipt)
            except IndexerError as e:
                node.latency_ms = int((time.monotonic() - started) * 1000)
                node.error = str(e)
                node.stage = "failed"
                node.paid_msat = receipt.get("paid_msat", 0)
                node.payment_hash = receipt.get("payment_hash")
                return None
            node.latency_ms = int((time.monotonic() - started) * 1000)
            node.ok = True
            node.stage = "responded"
            node.returned = len(result_set["results"])
            node.paid_msat = ask.cost_msat
            node.payment_hash = receipt.get("payment_hash")
            return result_set["results"]

        selected = {a.pubkey for a in asks}
        nodes = []
        for ask in asks:
            node = NodeOutcome(
                ask.pubkey, ask.endpoint, ask.cost_msat, stage="routed",
                transport="tor" if _is_onion(ask.announcement) else "clearnet",
                price_msat=economics.price_for(
                    ask.announcement, pricing_type) or 0,
                coverage=self._coverage(query, ask.pubkey))
            outcome.nodes.append(node)
            nodes.append(node)
        considered_pks = {a["pubkey"] for a in ranked}
        left_out = [a for a in self.peers.indexers()
                    if a["pubkey"] not in considered_pks]
        for ann in list(ranked) + left_out:
            if ann["pubkey"] in selected:
                continue
            outcome.nodes.append(NodeOutcome(
                ann["pubkey"], ann.get("endpoint", ""),
                cost_msat=economics.query_cost(
                    ann, pricing_type, max_results) or 0,
                stage="considered",
                transport="tor" if _is_onion(ann) else "clearnet",
                price_msat=economics.price_for(ann, pricing_type) or 0,
                coverage=self._coverage(query, ann["pubkey"]),
                dropped=self._skip_reason(
                    ann, considered_pks, scores, pricing_type, max_results,
                    max_price, exclude_onion, scope)))

        per_indexer: list[tuple[str, list[dict]]] = []
        if asks:
            with ThreadPoolExecutor(max_workers=len(asks)) as pool:
                answers = list(pool.map(ask_one, asks, nodes))
            per_indexer = [(ask.pubkey, results)
                           for ask, results in zip(asks, answers)
                           if results is not None]

        # raw is populated before merge runs and never mutated by it.
        outcome.raw = {pubkey: results for pubkey, results in per_indexer}
        if self._merge_rich:
            outcome.results = self.merge(per_indexer, limit=max_results,
                                         query=query, affinities=scores)
        else:
            outcome.results = self.merge(per_indexer, limit=max_results)
        kept = {r.get("url") for r in outcome.results}
        for node, (_pubkey, results) in zip(
                [n for n in outcome.nodes if n.ok], per_indexer):
            node.survived = sum(1 for r in results if r.get("url") in kept)

        outcome.finished_ms = int(time.time() * 1000)
        self.outcome_log.emit(outcome)
        return outcome

    def quote(self, query: str, max_results: int, pricing_type: str, *,
              exclude_onion: bool = False,
              budget_msat: int | None = None, scope=None) -> Quote:
        """What routing would spend on this query against the current cache.

        The same rank and allocation as a real route — this is the promise
        half of pricing, computed from cached announcements and statistics
        only. No indexer is contacted. ``budget_msat`` caps the downstream
        spend (the customer's max_msat translated past the margin): the
        allocation drops the lowest-merit indexers until it fits, exactly
        as the paid route will. The settlement may differ: that is what
        the stats age is for.

        With a ``scope`` the quote also says, for each selected indexer
        the route would not ask, why — so a client can name what its
        selection lost before anything is spent.
        """
        scope = frozenset(scope) if scope else None
        ceiling = _NO_CEILING if budget_msat is None else budget_msat
        ranked, scores, asks = self._select(
            query, max_results, pricing_type, ceiling, exclude_onion, scope)
        skipped = ()
        if scope:
            asked = {a.pubkey for a in asks}
            considered = {a["pubkey"] for a in ranked}
            skipped = tuple(
                (ann["pubkey"], self._skip_reason(
                    ann, considered, scores, pricing_type, max_results,
                    ceiling, exclude_onion, scope))
                for ann in self.peers.indexers()
                if ann["pubkey"] in scope and ann["pubkey"] not in asked)
        return Quote(
            downstream_msat=sum(a.cost_msat for a in asks),
            indexer_count=len(asks),
            stats_age_seconds=(self.stats.age_seconds()
                               if self.stats is not None else None),
            asked=tuple(a.pubkey for a in asks),
            skipped=skipped)

    def peer_view(self, cards: dict | None = None) -> dict:
        """The card view: every indexer with its identity, corpus, pricing,
        broker-selected chips, a composed PROFILE (what the broker thinks
        this index covers, every clause traceable to a measurement), and a
        vocabulary table (top terms by blended lift). Identities are on
        the cards by decision — see DECISIONS.md.
        """
        cards = cards or {}
        min_len = int(cards.get("min_token_length",
                                stats_module.MIN_TOKEN_LENGTH))
        window = int(cards.get("fresh_window_seconds",
                               stats_module.FRESH_WINDOW_SECONDS))
        indexers = self.peers.indexers()
        snapshots = {}
        for a in indexers:
            corpus = a.get("corpus") or {}
            terms = (self.stats.terms(a["pubkey"])
                     if self.stats is not None else {})
            snapshots[a["pubkey"]] = (corpus.get("document_count", 0), terms)

        out = []
        for a in indexers:
            pubkey = a["pubkey"]
            corpus = a.get("corpus") or {}
            doc_count, terms = snapshots[pubkey]
            # Cards score against the fixed general-English prior alone:
            # every indexer's vocabulary displays on its own terms, and a
            # mirror pair simply shows the same chips (by decision — a
            # network-relative baseline made mirrors cancel each other's
            # description; see DECISIONS.md). The mirror relationship is
            # still stated by the overlap clause.
            network = None
            chips = stats_module.distinctive_terms(
                doc_count, terms, network=network, min_token_length=min_len)
            recent = stats_module.recent_terms(
                doc_count, terms, exclude=set(chips), network=network,
                fresh_window_seconds=window, min_token_length=min_len)
            out.append({
                "pubkey": pubkey,
                "endpoint": a.get("endpoint", ""),
                "transport": "tor" if _is_onion(a) else "clearnet",
                "corpus": {
                    "document_count": doc_count,
                    "term_count": corpus.get("term_count", 0),
                    "updated": corpus.get("updated", 0),
                    "languages": list(corpus.get("languages") or []),
                },
                "pricing": [dict(opt) for opt in a.get("pricing", [])],
                "chips": chips,
                "recent": recent,
                "profile": self._profile(pubkey, doc_count, terms, chips,
                                         snapshots, corpus),
                "vocabulary": self._vocabulary(doc_count, terms, network,
                                               min_len),
                "last_ingest": stats_module.last_ingest(terms),
                "last_seen": a.get("timestamp", 0),
                "stats_age_s": (self.stats.age_for(pubkey)
                                if self.stats is not None else None),
                "reach": (self.reachability.status(pubkey)
                          if self.reachability is not None else None),
                "learned": (self.learner.profile(pubkey)
                            if self.learner is not None else None),
            })
        out.sort(key=lambda e: -e["corpus"]["document_count"])
        return {"indexers": out, "count": len(out)}

    @staticmethod
    def _profile(pubkey: str, doc_count: int, terms: dict,
                 chips: list, snapshots: dict, corpus: dict) -> list[dict]:
        """What the broker thinks this index covers — composed clauses,
        each carrying the measurement it rests on. Inference from REPORTED
        statistics; the client labels it unverified until delivery
        evidence (the outcome store) says otherwise."""
        clauses = []
        if doc_count >= 1_000_000:
            kind = "a broad web-scale corpus"
        elif doc_count and doc_count <= 20_000:
            kind = "a small, focused corpus"
        elif doc_count:
            kind = "a mid-size corpus"
        else:
            kind = "an empty or unreported corpus"
        clauses.append({"text": kind,
                        "detail": f"{doc_count:,} documents announced"})
        languages = list(corpus.get("languages") or [])
        if len(languages) >= 4:
            clauses.append({
                "text": f"multilingual ({len(languages)} languages)",
                "detail": "announced languages: " + ", ".join(languages)})
        if not terms:
            clauses.append({
                "text": "term statistics not fetched yet",
                "detail": "gossip pulls /corpus on its next cycle; "
                          "everything below fills in then"})
            return clauses
        share = stats_module.boilerplate_share(terms)
        if terms and share >= 0.25:
            clauses.append({
                "text": "raw web-page vocabulary",
                "detail": f"{share:.0%} of its reported terms are common "
                          "English or web chrome — the signature of an "
                          "unfiltered page crawl"})
        elif terms and share <= 0.10:
            clauses.append({
                "text": "clean, curated vocabulary",
                "detail": f"only {share:.0%} of its reported terms are "
                          "common English or web chrome"})
        if chips:
            clauses.append({
                "text": "strongest distinctive coverage: "
                        + ", ".join(chips[:3]),
                "detail": "the terms with the highest lift over both the "
                          "rest of the network and general English"})
        else:
            clauses.append({
                "text": "no vocabulary that stands out from the rest of "
                        "the network",
                "detail": "every reported term is about as common "
                          "elsewhere — consistent with a mirror or a "
                          "very general crawl"})
        best, best_pk = 0.0, None
        for other_pk, (_count, other_terms) in snapshots.items():
            if other_pk == pubkey:
                continue
            j = stats_module.vocabulary_overlap(terms, other_terms)
            if j > best:
                best, best_pk = j, other_pk
        if best_pk is not None and best >= 0.5:
            short = best_pk[:8] + "\u2026" + best_pk[-4:]
            clauses.append({
                "text": f"vocabulary largely overlaps {short}",
                "detail": f"Jaccard overlap {best:.2f} between reported "
                          "top-term sets — possibly the same corpus"})
        elif best_pk is not None and best < 0.15 and terms:
            clauses.append({
                "text": "vocabulary unlike every other indexer",
                "detail": f"highest top-term overlap with any peer is "
                          f"only {best:.2f} (Jaccard)"})
        return clauses

    @staticmethod
    def _vocabulary(doc_count: int, terms: dict, network: dict,
                    min_len: int, limit: int = 20) -> list[dict]:
        """The stats themselves, made legible: top terms by blended lift
        with their document share — the raw layer under the profile."""
        scored = []
        eligible = stats_module._eligible(doc_count, terms,
                                          min_token_length=min_len)
        for t, (dc, _newest) in eligible.items():
            lift = stats_module.term_lift(t, dc, doc_count, network)
            if lift < stats_module.MIN_LIFT:
                continue
            scored.append({"term": t, "df": dc,
                           "pct": round(100 * dc / doc_count, 2)
                           if doc_count else 0.0,
                           "lift": round(lift, 2)})
        scored.sort(key=lambda e: (-e["lift"], -e["df"], e["term"]))
        return scored[:limit]

    # -- entry pricing (payment wraps the entrance) ------------------------

    def entry_redeemable(self, auth_header: str, pricing_type: str,
                         body_sha256: str | None = None,
                         pubkey: str | None = None,
                         request_id: str | None = None,
                         witnessed: bool = False) -> bool:
        """Whether an incoming client query is paid for at the door.

        Checks only — nothing is consumed here. The credential is spent by
        ``entry_consume`` once the route actually served, so a payer never
        loses a credential to a route that produced nothing. With no
        receiver the broker charges nothing and everything is redeemable;
        an unredeemable query is answered with ``entry_challenge`` (a 402),
        never a protocol Error.
        """
        if self.receiver is None:
            return True
        return self.receiver.redeemable(
            auth_header, pricing_type, body_sha256,
            pubkey=pubkey, request_id=request_id, witnessed=witnessed)

    def entry_consume(self, auth_header: str,
                      request_id: str | None = None,
                      witnessed: bool = False) -> None:
        if self.receiver is not None:
            self.receiver.consume(auth_header, request_id,
                                  witnessed=witnessed)

    def entry_challenge(self, amount_msat: int, pricing_type: str,
                        quantity: int = 1, body_sha256: str | None = None,
                        request_id: str = "",
                        bound_pubkey: str | None = None):
        """Mint the door's 402: (headers, body).

        The door is the /client product API, not a protocol path, so the
        body is plain JSON rather than DITTA's signed payment_challenge:
        everything a client needs to pay and redeem, with nothing to
        decode or verify. The L402 ``WWW-Authenticate`` header carries the
        same macaroon and invoice for HTTP clients that speak L402.
        """
        if self.receiver is None:
            raise RuntimeError("broker has no receiver; entry is free")
        ch = self.receiver.challenge(amount_msat, pricing_type, quantity,
                                     body_sha256, bound_pubkey)
        body = {"error": "payment_required",
                "request_id": request_id,
                "invoice": ch["invoice"],
                "macaroon": ch["macaroon"],
                "payment_hash": ch["payment_hash"],
                "amount_msat": ch["amount_msat"],
                "invoice_expires": ch["invoice_expires"],
                "credential_expires": ch["expiry"]}
        return {"WWW-Authenticate": ch["header"]}, body
