"""Economics: who to ask and how many results from each, within a ceiling.

Contract: takes routing's merit-ranked node list, per-node prices, and a
ceiling, and returns who to ask and how many results from each.

The direction of coupling is the design (see the module notes): routing ranks
on merit and never sees a price; economics sets the cutoff and never reorders
merit. Value-for-money reasoning — trading rank position against price —
belongs here, because this is the only stage holding both. The baseline does
none of it: it walks the merit order, drops nodes that are unaffordable (or
unpayable), and cuts at the fanout. A smarter economics may reorder; the
baseline deliberately does not.

Allocation is the one genuinely new decision this module owns. The baseline
asks every selected node for the full result count — over-fetching aids
fusion, and under per_result pricing the cost of the allocation is exactly
what the broker commits to pay. A smarter allocation might taper counts down
the merit rank.
"""

from __future__ import annotations

from dataclasses import dataclass


def price_for(announcement: dict, pricing_type: str) -> int | None:
    for opt in announcement.get("pricing", []):
        if opt["type"] == pricing_type:
            return opt["price"]
    return None


def query_cost(announcement: dict, pricing_type: str,
               result_count: int) -> int | None:
    """Total cost of one query under a pricing type, or None if not offered.

    ``per_result`` is priced per the requested result count; the product is
    computed with Python's arbitrary-precision integers, so it cannot silently
    overflow a double (Security considerations).
    """
    price = price_for(announcement, pricing_type)
    if price is None:
        return None
    if pricing_type == "per_result":
        return price * result_count
    return price  # per_query, and per_batch's whole-batch price


@dataclass(frozen=True)
class Ask:
    """One authorized downstream query: whom, how many results, at what cost."""

    announcement: dict
    max_results: int
    cost_msat: int

    @property
    def pubkey(self) -> str:
        return self.announcement["pubkey"]

    @property
    def endpoint(self) -> str:
        return self.announcement["endpoint"]


def allocate(ranked: list[dict], pricing_type: str, ceiling_msat: int,
             max_results: int, *, can_pay: bool, fanout: int = 3,
             scores: dict | None = None,
             skip_odds: float | None = None) -> list[Ask]:
    """The baseline spend decision: merit order in, affordable asks out.

    ``ranked`` is routing's output, best first; its order is preserved.
    ``ceiling_msat`` is the per-node ceiling for this pricing type. A node is
    skipped when it does not offer the pricing type, exceeds the ceiling, or
    charges at all while the broker cannot pay (a pay-disabled broker still
    serves the free network).

    When routing supplied LOG-scores and a ``skip_odds`` threshold, a node
    whose score trails the best by more than log(skip_odds) is not asked at
    all — fanout is a ceiling, not a quota: a query that clearly belongs to
    one corpus goes to that corpus alone.
    """
    cutoff = None
    if scores and skip_odds and skip_odds > 1:
        import math

        in_scores = [scores[a["pubkey"]] for a in ranked
                     if a["pubkey"] in scores]
        if in_scores:
            cutoff = max(in_scores) - math.log(skip_odds)
    asks: list[Ask] = []
    for ann in ranked:
        if cutoff is not None                 and scores.get(ann["pubkey"], cutoff) < cutoff:
            continue
        cost = query_cost(ann, pricing_type, max_results)
        if cost is None or cost > ceiling_msat:
            continue
        if cost > 0 and not can_pay:
            continue
        asks.append(Ask(ann, max_results, cost))
        if len(asks) >= fanout:
            break
    return asks
