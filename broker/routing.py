"""Routing: query + corpus statistics (+ reputation) → ranked list of nodes.

Contract: takes a query and corpus statistics and returns a ranked list of
nodes to ask, best first. Merit only — routing NEVER reads a price. Prices
belong to economics, which sets the cutoff on this ranking; if a price ever
influences the order here, the seam has leaked (the direction-probe test in
tests/test_modules.py mutates prices and asserts this ranking is unchanged).

The baseline rank consults the cached per-term statistics discovery has
fetched from each indexer's /corpus, so it is genuinely query-dependent: an
indexer covering more of the query's terms outranks one covering fewer, more
documents under the covered terms breaks the tie, corpus size breaks that,
and pubkey keeps it deterministic. A query containing a term nobody covers
therefore selects — and prices — differently from one everyone covers, which
is exactly what a quote shown before payment is supposed to reflect.

Tokenization here is the broker's own guess (lowercase alphanumeric runs);
terms are opaque strings whose semantics belong to the indexer that reported
them, so a miss may be vocabulary mismatch rather than absence. That is the
baseline being honest, not clever. A real router replaces this function and
keeps its judgment private — smarter term matching, freshness weighting,
reputation projected from the outcome log, query understanding as a private
input to ranking (never a rewrite of what the indexer sees).
"""

from __future__ import annotations

import re

_TOKEN_RE = re.compile(r"[a-z0-9]+")


def tokenize(query: str) -> list[str]:
    """The baseline's term guess: lowercase alphanumeric runs, deduplicated."""
    seen = []
    for token in _TOKEN_RE.findall(query.lower()):
        if token not in seen:
            seen.append(token)
    return seen


def rank(query: str, indexers: list[dict], stats=None) -> list[dict]:
    """The baseline merit rank. Best first; never reads a price.

    ``indexers`` are verified current announcements from discovery. ``stats``
    is discovery's StatsCache (or anything with ``.terms(pubkey)``); with no
    statistics cached the rank degrades to corpus size, query-independent.
    """
    terms = tokenize(query)

    def merit(announcement: dict):
        pubkey = announcement["pubkey"]
        term_stats = stats.terms(pubkey) if stats is not None else {}
        covered = [t for t in terms if t in term_stats]
        weight = sum(term_stats[t][0] for t in covered)
        return (
            -len(covered),                                        # coverage
            -weight,                                              # depth
            -announcement.get("corpus", {}).get("document_count", 0),
            pubkey,
        )

    return sorted(indexers, key=merit)
