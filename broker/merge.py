"""Merge: N ranked result lists in, one ordered list out. Owns dedupe.

Contract: takes result sets and returns one ordered list. Merge never sees
a price — or it's pay-for-ranking. It now RETAINS origin: every merged
entry carries ``sources`` (which indexers returned it, and at what rank in
their own list), because a paid customer may know who served them. The
pubkeys are bookkeeping only — nothing about the ordering reads them.

The protocol carries no scores, only order, so the baseline fuses by
position with Reciprocal Rank Fusion. Deduplication is by URL after
trivial normalisation (scheme and host lowercased, trailing slash
stripped, fragment dropped) — deliberately no deeper, since per-result
attribution is ambiguous under duplicates and the point is to show that
ambiguity, not paper over it. Sophistication ranges from this to
fetch-every-URL-and-rerank (a GPU); the contract is the same at every
level.
"""

from __future__ import annotations

from urllib.parse import urlsplit, urlunsplit


def normalize_url(url: str) -> str:
    """The trivial normalization dedupe keys on; also what the outcome
    store records for overlap measurement."""
    return _dedupe_key(url)


def _dedupe_key(url: str) -> str:
    try:
        parts = urlsplit(url)
    except ValueError:
        return url
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(),
                       parts.path.rstrip("/"), parts.query, ""))


def merge_results(per_indexer: list[tuple[str, list[dict]]], *,
                  limit: int, k: int = 60) -> list[dict]:
    """Merge result sets into one ranked list via Reciprocal Rank Fusion.

    ``per_indexer`` is a list of (pubkey, results) in each indexer's own
    order. Each merged entry's display fields come from the winning source
    — the one that ranked the URL best, ties broken by arrival order — and
    its ``sources`` lists every indexer that returned the URL with that
    indexer's own 1-based rank, winner first. RRF accumulates over all of
    them; ``rank`` is the 1-based merged position.
    """
    scored: dict[str, dict] = {}
    for arrival, (pubkey, results) in enumerate(per_indexer):
        for position, result in enumerate(results):
            url = result.get("url")
            if url is None:
                continue
            key = _dedupe_key(url)
            entry = scored.get(key)
            if entry is None:
                entry = {"result": result, "score": 0.0, "sources": [],
                         "best": (position, arrival)}
                scored[key] = entry
            entry["score"] += 1.0 / (k + position)
            entry["sources"].append(
                {"pubkey": pubkey, "rank": position + 1,
                 "_order": (position, arrival)})
            if (position, arrival) < entry["best"]:
                entry["best"] = (position, arrival)
                entry["result"] = result
    ranked = sorted(scored.values(), key=lambda e: e["score"], reverse=True)
    merged = []
    for i, entry in enumerate(ranked[:limit]):
        sources = sorted(entry["sources"], key=lambda s: s["_order"])
        merged.append({
            **{key: entry["result"].get(key, "")
               for key in ("id", "url", "title", "snippet")},
            "rank": i + 1,
            "sources": [{"pubkey": s["pubkey"], "rank": s["rank"]}
                        for s in sources],
        })
    return merged


def merge_evidence(per_indexer: list[tuple[str, list[dict]]], *,
                   limit: int, query: str = "", affinities: dict | None = None,
                   k: int = 60, divider_ratio: float = 0.2) -> list[dict]:
    """Affinity-weighted evidence merge: rank by what the result itself
    shows, who served it, and who agrees — not by position alone.

    Three multiplied signals, each explainable on the receipt:

      agreement   the RRF sum over sources (cross-indexer corroboration),
      local       query-term presence in the result's own title/snippet/
                  URL, rarity-weighted — a result whose surface shows no
                  query term is aggressively demoted whatever its rank,
      affinity    how much routing believed the result's best source FOR
                  THIS QUERY (the same log-scores selection used).

    Returns EVERY fetched unique result, ranked hard, each entry carrying
    ``confidence`` (score relative to the top result) and ``weak`` — the
    divider: below it the broker does not think the result answers the
    query. The page shows the tail; the broker never hides what was paid
    for. ``limit`` caps the tail at 10x the asked-for count.
    """
    import math

    from .routing import tokenize
    from .stats import _GENERAL_DF, _GENERAL_FLOOR

    terms = tokenize(query)
    weights = {t: -math.log(_GENERAL_DF.get(t, _GENERAL_FLOOR))
               for t in terms}
    total_weight = sum(weights.values())

    def local_evidence(result: dict) -> float:
        if not total_weight:
            return 1.0  # an empty query judges nothing
        text = " ".join((result.get("title", ""), result.get("snippet", ""),
                         result.get("url", ""))).lower()
        return sum(w for t, w in weights.items() if t in text) / total_weight

    max_aff = max(affinities.values()) if affinities else 0.0

    def source_trust(pubkey: str) -> float:
        if not affinities or pubkey not in affinities:
            return 1.0
        return math.exp(affinities[pubkey] - max_aff)  # (0, 1], best = 1

    scored: dict[str, dict] = {}
    for arrival, (pubkey, results) in enumerate(per_indexer):
        for position, result in enumerate(results):
            url = result.get("url")
            if url is None:
                continue
            key = _dedupe_key(url)
            entry = scored.get(key)
            if entry is None:
                entry = {"result": result, "rrf": 0.0, "sources": [],
                         "best": (position, arrival), "trust": 0.0}
                scored[key] = entry
            entry["rrf"] += 1.0 / (k + position)
            entry["trust"] = max(entry["trust"], source_trust(pubkey))
            entry["sources"].append(
                {"pubkey": pubkey, "rank": position + 1,
                 "_order": (position, arrival)})
            if (position, arrival) < entry["best"]:
                entry["best"] = (position, arrival)
                entry["result"] = result

    for entry in scored.values():
        entry["local"] = local_evidence(entry["result"])
        entry["score"] = entry["rrf"] * (0.2 + 0.8 * entry["local"]) \
            * (0.3 + 0.7 * entry["trust"])
    ranked = sorted(scored.values(), key=lambda e: -e["score"])
    top = ranked[0]["score"] if ranked else 1.0
    merged = []
    for i, entry in enumerate(ranked[:limit * 10]):
        sources = sorted(entry["sources"], key=lambda s: s["_order"])
        confidence = entry["score"] / top if top > 0 else 0.0
        merged.append({
            **{key: entry["result"].get(key, "")
               for key in ("id", "url", "title", "snippet")},
            "rank": i + 1,
            "sources": [{"pubkey": s["pubkey"], "rank": s["rank"]}
                        for s in sources],
            "confidence": round(confidence, 3),
            "weak": bool(entry["local"] == 0.0
                         or confidence < divider_ratio),
        })
    return merged
