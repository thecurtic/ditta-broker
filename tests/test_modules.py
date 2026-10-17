"""The swappability test, run for real.

The only proof that a seam is right: replace one module with a deliberately
stupid alternative — route to everyone, merge by concatenation — and check
that nothing else degrades. Plus the direction probe: degradation tests catch
missing outputs, but only a direction probe catches illicit reads (routing
consulting a price it should never see).
"""

import copy

from broker import economics, routing
from broker.outcomes import OutcomeLog
from broker.identity import Identity
from broker.payment import MockPayProvider
from helpers import FakeIndexer, broker_with


def _announcement(pubkey, document_count, price, pricing_type="per_query"):
    """A bare announcement-shaped dict; rank and allocate never verify sigs."""
    pricing = [{"type": pricing_type, "price": price}]
    if pricing_type == "per_batch":
        pricing[0]["query_count"] = 10
    return {
        "pubkey": pubkey, "protocol_version": 0,
        "endpoint": f"https://{pubkey[:8]}.test", "timestamp": 1,
        "roles": ["indexer"], "pricing": pricing,
        "corpus": {"document_count": document_count, "term_count": 1,
                   "updated": 1, "languages": ["en"],
                   "statistics_versions": [0]},
    }


# --- direction probe: routing is price-blind --------------------------------

def test_routing_rank_is_byte_identical_under_price_mutation():
    indexers = [
        _announcement("aa" * 32, document_count=100, price=1),
        _announcement("bb" * 32, document_count=9000, price=50_000),
        _announcement("cc" * 32, document_count=500, price=0),
    ]
    before = [a["pubkey"] for a in routing.rank("any query", indexers)]

    mutated = copy.deepcopy(indexers)
    for ann in mutated:
        ann["pricing"][0]["price"] = 999_999_999  # wildly different prices
    after = [a["pubkey"] for a in routing.rank("any query", mutated)]

    # If the order shifts, a price leaked into the merit rank.
    assert before == after
    assert before == ["bb" * 32, "cc" * 32, "aa" * 32]  # corpus size, desc


def test_routing_rank_consults_term_statistics():
    from broker.stats import StatsCache

    big = _announcement("aa" * 32, document_count=9000, price=0)
    small = _announcement("bb" * 32, document_count=100, price=0)
    stats = StatsCache()
    stats.put("aa" * 32, {"bitcoin": (5000, 1)}, updated=1)
    stats.put("bb" * 32, {"jamming": (40, 1), "mitigation": (25, 1)},
              updated=1)

    # The small indexer covers the query's terms; the big one does not.
    ranked = routing.rank("jamming mitigation", [big, small], stats)
    assert [a["pubkey"] for a in ranked] == ["bb" * 32, "aa" * 32]
    # A query neither covers falls back to corpus size.
    ranked = routing.rank("cooking recipes", [big, small], stats)
    assert [a["pubkey"] for a in ranked] == ["aa" * 32, "bb" * 32]
    # Price mutation still cannot move a term-aware rank (direction probe).
    small_pricey = copy.deepcopy(small)
    small_pricey["pricing"][0]["price"] = 999_999
    ranked = routing.rank("jamming mitigation", [big, small_pricey], stats)
    assert [a["pubkey"] for a in ranked] == ["bb" * 32, "aa" * 32]


# --- economics: cutoff without reordering -----------------------------------

def test_allocate_preserves_merit_order_even_when_cheaper_ranks_lower():
    ranked = [
        _announcement("aa" * 32, 9000, price=3000),  # best merit, pricier
        _announcement("bb" * 32, 100, price=10),     # worst merit, cheapest
    ]
    asks = economics.allocate(ranked, "per_query", ceiling_msat=5000,
                              max_results=5, can_pay=True)
    assert [a.pubkey for a in asks] == ["aa" * 32, "bb" * 32]


def test_allocate_applies_ceiling_can_pay_and_fanout():
    ranked = [
        _announcement("aa" * 32, 9000, price=8000),   # over ceiling
        _announcement("bb" * 32, 8000, price=2000),   # affordable
        _announcement("cc" * 32, 7000, price=0),      # free
        _announcement("dd" * 32, 6000, price=100),    # affordable
        _announcement("ee" * 32, 5000, price=100),    # cut by fanout
    ]
    asks = economics.allocate(ranked, "per_query", ceiling_msat=5000,
                              max_results=5, can_pay=True, fanout=3)
    assert [a.pubkey for a in asks] == ["bb" * 32, "cc" * 32, "dd" * 32]

    # Pay-disabled: only the free node survives, merit order intact.
    asks = economics.allocate(ranked, "per_query", ceiling_msat=5000,
                              max_results=5, can_pay=False, fanout=3)
    assert [a.pubkey for a in asks] == ["cc" * 32]


def test_allocate_per_result_cost_is_price_times_count():
    ranked = [_announcement("aa" * 32, 100, price=250,
                            pricing_type="per_result")]
    asks = economics.allocate(ranked, "per_result", ceiling_msat=2000,
                              max_results=8, can_pay=True)
    assert len(asks) == 1 and asks[0].cost_msat == 2000  # exactly the ceiling
    asks = economics.allocate(ranked, "per_result", ceiling_msat=1999,
                              max_results=8, can_pay=True)
    assert asks == []  # one msat over


# --- swap tests: stupid replacements degrade nothing -------------------------

def _two_indexers(tmp_path):
    a = FakeIndexer(Identity.generate(), price_msat=1000,
                    db_path=tmp_path / "a.db", document_count=9000,
                    results=[{"id": "a1", "url": "https://a.test/1",
                              "title": "A1", "snippet": "s"},
                             {"id": "a2", "url": "https://both.test/x",
                              "title": "AX", "snippet": "s"}])
    b = FakeIndexer(Identity.generate(), price_msat=500,
                    db_path=tmp_path / "b.db", document_count=100,
                    results=[{"id": "b1", "url": "https://both.test/x",
                              "title": "BX", "snippet": "s"},
                             {"id": "b2", "url": "https://b.test/2",
                              "title": "B2", "snippet": "s"}])
    return {"http://a.test": a, "http://b.test": b}


def test_route_to_everyone_degrades_nothing(tmp_path):
    broker = broker_with(
        _two_indexers(tmp_path), MockPayProvider(),
        rank=lambda query, indexers, stats: list(indexers),  # no judgment
        fanout=99)
    outcome = broker.route_query("q", max_results=10,
                                 pricing_type="per_query", max_price=5000)
    assert len(outcome.asked) == 2
    assert not outcome.errors
    # Merge still dedupes the shared URL: 4 results in, 3 distinct out.
    assert len(outcome.results) == 3
    assert outcome.paid_msat == 1500


def test_merge_by_concatenation_degrades_nothing(tmp_path):
    broker = broker_with(
        _two_indexers(tmp_path), MockPayProvider(),
        merge=lambda per, limit: [r for _, rs in per for r in rs][:limit],
        fanout=99)
    outcome = broker.route_query("q", max_results=10,
                                 pricing_type="per_query", max_price=5000)
    # Objective delivery facts are untouched by a stupid merge...
    assert not outcome.errors
    assert outcome.paid_msat == 1500
    assert all(n.ok and n.returned == 2 for n in outcome.nodes)
    # ...while merge-opinion facts (no dedupe -> 4 results) reflect the swap.
    assert len(outcome.results) == 4


def test_outcome_log_records_and_survives_bad_subscribers(tmp_path):
    seen = []
    log = OutcomeLog()
    log.subscribe(lambda outcome: seen.append(outcome))
    log.subscribe(lambda outcome: 1 / 0)  # a reader failing is harmless

    broker = broker_with(_two_indexers(tmp_path), MockPayProvider(),
                         outcome_log=log, fanout=99)
    outcome = broker.route_query("q", max_results=10,
                                 pricing_type="per_query", max_price=5000)

    assert seen == [outcome]
    by_pubkey = {n.pubkey: n for n in outcome.nodes}
    for indexer_node in by_pubkey.values():
        assert indexer_node.ok
        assert indexer_node.returned == 2
        assert indexer_node.paid_msat in (500, 1000)
        # The shared URL survived under one indexer's copy; each node kept >= 1.
        assert 1 <= indexer_node.survived <= 2
    assert sum(n.survived for n in outcome.nodes) == 4  # 3 kept, 1 shared URL
