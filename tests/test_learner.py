"""The learner and the density routing it feeds — including THE test:
a nostr-flavored query, never saying "nostr", routes to the nostr indexer
alone, and the receipt says why everyone else was skipped.
"""

import functools
import random

from broker import merge
from broker.identity import Identity
from broker.learner import Learner
from broker.outcomes import NodeOutcome, OutcomeStore, RouteOutcome
from broker.payment.pay import NullPayProvider
from broker.stats import StatsCache
from helpers import FakeIndexer, broker_with


def _network(tmp_path):
    """Three corpora shaped like the live network: nostr, cybersec, and a
    web-scale generalist whose snapshot has neither niche's vocabulary."""
    nostr = FakeIndexer(
        Identity.generate(), price_msat=0, db_path=tmp_path / "nostr.db",
        document_count=19_000,
        results=[{"id": "n1", "url": "https://njump.example/note1",
                  "title": "Zap splits for relays", "snippet": "zap zap"}])
    cyber = FakeIndexer(
        Identity.generate(), price_msat=0, db_path=tmp_path / "cyber.db",
        document_count=3_351,
        results=[{"id": "c1", "url": "https://cve.example/a",
                  "title": "Attacker writeup", "snippet": "cwe"}])
    general = FakeIndexer(
        Identity.generate(), price_msat=0, db_path=tmp_path / "gen.db",
        document_count=102_000_000,
        results=[{"id": "g1", "url": "https://web.example/x",
                  "title": "A page", "snippet": "text"}])
    stats = StatsCache()
    stats.put(nostr.identity.pubkey,
              {"zap": (1500, 1), "relays": (900, 1), "nostr": (9000, 1)},
              updated=1)
    stats.put(cyber.identity.pubkey,
              {"attacker": (900, 1), "cwe": (800, 1)}, updated=1)
    stats.put(general.identity.pubkey,
              {"wikipedia": (40_000_000, 1)}, updated=1)
    return nostr, cyber, general, stats


def _learned_broker(tmp_path, store=None):
    nostr, cyber, general, stats = _network(tmp_path)
    store = store or OutcomeStore(tmp_path / "outcomes.db", stats=stats)
    learner = Learner(store)
    learner.rng_for = lambda q, salt="": random.Random(42)  # test determinism
    broker = broker_with(
        {"http://nostr.test": nostr, "http://cyber.test": cyber,
         "http://general.test": general},
        NullPayProvider(), stats=stats, rank=learner.scorer,
        learner=learner, routing_policy={"skip_odds": 10.0},
        merge=functools.partial(merge.merge_evidence, divider_ratio=0.2))
    broker.outcome_log.subscribe(store.record)
    return broker, (nostr, cyber, general), store, learner


def test_niche_query_routes_to_the_niche_indexer_alone(tmp_path):
    """The acceptance test: "zap relays" never says nostr, but the nostr
    corpus is ~4000x denser in those terms than anyone's smoothed floor —
    so it is asked ALONE, and the receipt says low_score for the rest."""
    broker, (nostr, cyber, general), _store, _l = _learned_broker(tmp_path)
    outcome = broker.route_query("zap relays", max_results=5,
                                 pricing_type="per_query", max_price=0)
    assert outcome.asked == [nostr.identity.pubkey]
    assert [r["url"] for r in outcome.results] == \
        ["https://njump.example/note1"]
    reasons = {n.pubkey: n.dropped for n in outcome.nodes
               if n.stage == "considered"}
    assert reasons == {cyber.identity.pubkey: "low_score",
                       general.identity.pubkey: "low_score"}


def test_broad_query_still_fans_out(tmp_path):
    broker, (nostr, cyber, general), _s, _l = _learned_broker(tmp_path)
    # A term in nobody's snapshot: densities equalize at the smoothed
    # prior, nothing clears the 10x odds gap, everyone is asked.
    outcome = broker.route_query("pottery", max_results=5,
                                 pricing_type="per_query", max_price=0)
    assert len(outcome.asked) == 3


def test_quote_and_route_agree_within_the_hour(tmp_path):
    broker, _idx, _s, _l = _learned_broker(tmp_path)
    first = broker.quote("zap relays", 5, "per_query")
    second = broker.quote("zap relays", 5, "per_query")
    assert first.indexer_count == second.indexer_count == 1


def test_delivery_evidence_reorders_equal_densities(tmp_path):
    """Two indexers claim the same density for a term; the one whose
    claims keep failing to deliver sinks (calibration + posteriors)."""
    a = FakeIndexer(Identity.generate(), 0, tmp_path / "a.db",
                    document_count=10_000,
                    results=[{"id": "a", "url": "https://a.test/1",
                              "title": "zap", "snippet": "s"}])
    b = FakeIndexer(Identity.generate(), 0, tmp_path / "b.db",
                    document_count=10_000, results=[])
    stats = StatsCache()
    for idx in (a, b):
        stats.put(idx.identity.pubkey, {"zap": (800, 1)}, updated=1)
    store = OutcomeStore(tmp_path / "outcomes.db", stats=stats)
    # History: b answered queries containing "zap" four times, never
    # contributing a merged result; a delivered (with a click).
    for merged, pk in [(1, a.identity.pubkey)] + \
            [(0, b.identity.pubkey)] * 4:
        outcome = RouteOutcome(query="zap wallets", pricing_type="per_query",
                               request_id="r")
        outcome.nodes.append(NodeOutcome(
            pk, "http://x", 0, stage="responded", returned=1,
            survived=merged))
        node = outcome.nodes[0]
        node.ok = True
        store.record(outcome)
    learner = Learner(store)
    learner.rng_for = lambda q, salt="": random.Random(7)
    learner.refresh(force=True)
    scored = learner.scorer("zap", [a.announcement("http://a.test"),
                                    b.announcement("http://b.test")], stats)
    order = [ann["pubkey"] for _s, ann in scored]
    assert order[0] == a.identity.pubkey
    assert scored[0][0] > scored[1][0]


def test_profile_reads_like_the_card_line(tmp_path):
    store = OutcomeStore(tmp_path / "outcomes.db")
    pk = "ab" * 32
    for stage, latency in [("responded", 300), ("responded", 500),
                           ("failed", 0)]:
        outcome = RouteOutcome(query="q", pricing_type="per_query")
        n = NodeOutcome(pk, "http://x", 0, stage=stage,
                        latency_ms=latency, returned=1, survived=1)
        n.ok = stage == "responded"
        outcome.nodes.append(n)
        store.record(outcome)
    learner = Learner(store)
    learner.refresh(force=True)
    p = learner.profile(pk)
    assert p["queries"] == 3
    assert 0 < p["success_rate"] < 1
    assert p["latency_p50_ms"] in (300, 500)
    assert p["calibration"] in ("good", "fair", "poor")
    assert learner.profile("cd" * 32) is None   # no data, no claims


def test_click_credit_splits_across_sources(tmp_path):
    broker, (nostr, cyber, general), store, _l = _learned_broker(tmp_path)
    outcome = broker.route_query("pottery", max_results=5,
                                 pricing_type="per_query", max_price=0,
                                 request_id="q-click")
    assert len(outcome.asked) == 3
    assert store.add_click("q-click", "https://njump.example/note1")
    rows = store.rows(query_id="q-click", pubkey=nostr.identity.pubkey)
    assert rows[0]["clicks"] == 1.0            # sole source: full credit
    assert not store.add_click("q-click", "https://nowhere.example/")
    assert not store.add_click("unknown-query", "https://a.test/")


def test_evidence_merge_demotes_surface_irrelevance(tmp_path):
    """An indexer's confident #1 that shows no trace of the query sinks
    below a relevant #2 from elsewhere — and nothing is hidden: the weak
    tail is returned, flagged for the divider."""
    per_indexer = [
        ("aa" * 32, [
            {"id": "x", "url": "https://x.test/promo",
             "title": "Buy our product", "snippet": "great deals"},
            {"id": "y", "url": "https://x.test/jam",
             "title": "Channel jamming analysis", "snippet": "jamming"},
        ]),
        ("bb" * 32, [
            {"id": "z", "url": "https://y.test/jamming",
             "title": "Jamming mitigations", "snippet": "channel jamming"},
        ]),
    ]
    merged = merge.merge_evidence(per_indexer, limit=10,
                                  query="channel jamming")
    urls = [r["url"] for r in merged]
    assert urls[0] in ("https://y.test/jamming", "https://x.test/jam")
    assert urls[-1] == "https://x.test/promo"    # demoted, not hidden
    promo = merged[-1]
    assert promo["weak"] is True
    assert all("confidence" in r for r in merged)


def test_persistence_peers_and_stats_survive_restart(tmp_path):
    from broker.peers import PeerStore

    indexer = FakeIndexer(Identity.generate(), 0, tmp_path / "i.db",
                          results=[])
    first = PeerStore(db_path=tmp_path / "peers.db")
    assert first.add(indexer.announcement("http://indexer.test"))
    reloaded = PeerStore(db_path=tmp_path / "peers.db")
    assert [a["endpoint"] for a in reloaded.indexers()] == \
        ["http://indexer.test"]

    cache = StatsCache(db_path=tmp_path / "stats.db")
    cache.put("ab" * 32, {"zap": (10, 5)}, updated=99)
    warm = StatsCache(db_path=tmp_path / "stats.db")
    assert warm.terms("ab" * 32) == {"zap": (10, 5)}
    assert warm.age_for("ab" * 32) is not None
