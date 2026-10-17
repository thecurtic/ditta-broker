"""Reachability: "searchable" means recently answered, not announced.

Design note: the module reads gossip contact events and routed-query
outcomes, publishes status/allowed/health_rank, and is deletable —
with reachability=None the node routes exactly as before (every other
test file runs that way, which is the proof).
"""

import time

from broker.gossip import Gossip
from broker.client import IndexerClient, LoopbackTransport
from broker.config import BrokerConfig
from broker.identity import Identity
from broker.payment.pay import NullPayProvider
from broker.peers import PeerStore
from broker.reachability import Reachability
from broker.stats import StatsCache
from helpers import FakeIndexer, broker_with


def test_status_lifecycle():
    r = Reachability(window_seconds=1800, degraded_horizon_seconds=21600,
                     startup_grace_seconds=0)
    pk = "aa" * 32
    assert r.status(pk)["status"] == "never"
    assert not r.allowed(pk)              # grace over: never is not routable

    r.note_success(pk)
    assert r.status(pk)["status"] == "reachable"
    assert r.allowed(pk)

    r.note_failure(pk, "boom")            # failed after a recent success
    s = r.status(pk)
    assert s["status"] == "degraded"
    assert s["last_error"] == "boom"
    assert r.allowed(pk)

    r._contacts[pk]["last_ok"] = time.time() - 7200   # window lapsed
    assert r.status(pk)["status"] == "degraded"       # within the horizon
    r._contacts[pk]["last_ok"] = time.time() - 30 * 86400
    assert r.status(pk)["status"] == "unreachable"
    assert not r.allowed(pk)

    fresh = Reachability(startup_grace_seconds=120)
    assert fresh.allowed("bb" * 32)       # a fresh restart routes normally


def _indexer(tmp_path, name):
    return FakeIndexer(
        Identity.generate(), price_msat=0, db_path=tmp_path / f"{name}.db",
        results=[{"id": name, "url": f"https://{name}.test/",
                  "title": name, "snippet": "s"}])


def test_routing_skips_the_unreached_and_records_why(tmp_path):
    reach = Reachability(startup_grace_seconds=0)
    indexer = _indexer(tmp_path, "one")
    broker = broker_with({"http://one.test": indexer}, NullPayProvider(),
                         reachability=reach)
    outcome = broker.route_query("q", max_results=5,
                                 pricing_type="per_query", max_price=0)
    assert outcome.results == []          # never contacted: never selected
    [node] = outcome.nodes
    assert node.stage == "considered"
    assert node.dropped == "never_contacted"

    reach.note_success(indexer.identity.pubkey)
    outcome = broker.route_query("q", max_results=5,
                                 pricing_type="per_query", max_price=0)
    assert [x["url"] for x in outcome.results] == ["https://one.test/"]
    # ...and the answered query itself became fresh contact evidence.
    assert reach.status(indexer.identity.pubkey)["status"] == "reachable"


def test_degraded_is_picked_only_when_nothing_better(tmp_path):
    reach = Reachability(startup_grace_seconds=0)
    shaky = _indexer(tmp_path, "shaky")
    steady = _indexer(tmp_path, "steady")
    reach.note_success(shaky.identity.pubkey)
    reach._contacts[shaky.identity.pubkey]["last_ok"] = time.time() - 7200
    reach.note_success(steady.identity.pubkey)
    broker = broker_with(
        {"http://shaky.test": shaky, "http://steady.test": steady},
        NullPayProvider(), reachability=reach, fanout=1,
        rank=lambda query, idx, stats: sorted(
            idx, key=lambda a: a["endpoint"]))   # shaky ranks first
    outcome = broker.route_query("q", max_results=5,
                                 pricing_type="per_query", max_price=0)
    # The fanout-1 cut lands on the healthy node despite shaky's rank.
    assert [x["url"] for x in outcome.results] == ["https://steady.test/"]
    assert outcome.errors == {}


def test_gossip_contact_failures_become_unreachable(tmp_path):
    reach = Reachability(startup_grace_seconds=0)
    indexer = _indexer(tmp_path, "down")
    peers = PeerStore()
    assert peers.add(indexer.announcement("http://down.test"))

    def exploding(path, body, headers):
        raise OSError("connection refused")

    cfg = BrokerConfig(data_dir=tmp_path)
    gossip = Gossip(Identity.generate(), peers,
                    LoopbackTransport({"http://down.test": exploding}),
                    cfg, stats=StatsCache(), reachability=reach)
    gossip.cycle()
    s = reach.status(indexer.identity.pubkey)
    assert s["status"] == "unreachable"
    assert "connection refused" in s["last_error"]
    assert not reach.allowed(indexer.identity.pubkey)

    # The node recovers; the next cycle's successful pull restores it.
    gossip.transport = LoopbackTransport({"http://down.test": indexer.handle})
    gossip.cycle()
    assert reach.status(indexer.identity.pubkey)["status"] == "reachable"


def test_peer_view_carries_the_status(tmp_path):
    reach = Reachability(startup_grace_seconds=0)
    indexer = _indexer(tmp_path, "one")
    broker = broker_with({"http://one.test": indexer}, NullPayProvider(),
                         reachability=reach)
    reach.note_failure(indexer.identity.pubkey, "timed out")
    [x] = broker.peer_view()["indexers"]
    assert x["reach"]["status"] == "unreachable"
    assert x["reach"]["last_error"] == "timed out"
    # Without the module, the field is simply absent-null — old behavior.
    plain = broker_with({"http://one.test": _indexer(tmp_path, "two")},
                        NullPayProvider())
    assert plain.peer_view()["indexers"][0]["reach"] is None


def test_seed_from_persisted_evidence_closes_the_restart_gap(tmp_path):
    """A restart inherits 'recently reached' from the stats cache and the
    outcome store, so a card never reads 'not yet contacted' beside a
    week of delivery history."""
    import time as _time

    from broker.outcomes import NodeOutcome, OutcomeStore, RouteOutcome
    from broker.stats import StatsCache

    pk_stats = "aa" * 32
    pk_outcome = "bb" * 32

    stats = StatsCache(db_path=tmp_path / "stats.db")
    stats.put(pk_stats, {"zap": (10, 1)}, updated=1)

    store = OutcomeStore(tmp_path / "outcomes.db")
    outcome = RouteOutcome(query="q", pricing_type="per_query",
                           started_ms=int(_time.time() * 1000))
    n = NodeOutcome(pk_outcome, "http://x", 0, stage="responded",
                    returned=1, survived=1)
    n.ok = True
    outcome.nodes.append(n)
    store.record(outcome)

    # Fresh reachability (a restart): both are "never" until seeded.
    reach = Reachability(startup_grace_seconds=0)
    assert reach.status(pk_stats)["status"] == "never"
    for pubkey, ts in store.last_success_times().items():
        reach.seed(pubkey, ts)
    reach.seed(pk_stats, stats.fetched_at(pk_stats))

    assert reach.status(pk_stats)["status"] == "reachable"
    assert reach.status(pk_outcome)["status"] == "reachable"
    # A live success since boot is never overwritten backwards by a seed.
    reach.note_success(pk_stats)
    before = reach.status(pk_stats)["last_ok"]
    reach.seed(pk_stats, 1.0)
    assert reach.status(pk_stats)["last_ok"] == before
