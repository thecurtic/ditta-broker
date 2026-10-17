"""The outcome store: the outcome log's first persistent reader.

Design note (what it reads, what it writes, why): it subscribes to the
node's OutcomeLog and writes one SQLite row per indexer per query —
identity, transport, tokenized terms, predicted coverage with the term
document-frequencies the prediction used, stage/skip reason, delivery
facts (latency, paid, returned), raw result URLs (normalized, for overlap
measurement), and the derived signals (merge survival, uniqueness) that
belong to this experimental broker's learner, not to a reference broker.
The learner, reachability status, and front page will read these rows and
nothing else. A store failure never fails a query (the log's rule), and
rows age out under a configurable retention.
"""

import json
import time

from broker.identity import Identity
from broker.outcomes import OutcomeStore
from broker.payment.pay import NullPayProvider
from broker.stats import StatsCache
from helpers import FakeIndexer, broker_with


def _searched_broker(tmp_path, store):
    indexer = FakeIndexer(
        Identity.generate(), price_msat=0, db_path=tmp_path / "idx.db",
        results=[{"id": "d1", "url": "https://example.org/A/",
                  "title": "Alpha", "snippet": "s"}])
    stats = StatsCache()
    stats.put(indexer.identity.pubkey, {"jamming": (200, 1)}, updated=1)
    broker = broker_with({"http://indexer.test": indexer},
                         NullPayProvider(), stats=stats)
    broker.outcome_log.subscribe(store.record)
    return broker, indexer


def test_one_row_per_indexer_per_query_with_the_doc_fields(tmp_path):
    stats = StatsCache()
    store = OutcomeStore(tmp_path / "outcomes.db", stats=stats)
    broker, indexer = _searched_broker(tmp_path, store)
    store._stats = broker.stats  # the deployment wires the live cache

    outcome = broker.route_query("Channel JAMMING!", max_results=5,
                                 pricing_type="per_query", max_price=0,
                                 request_id="q-1")
    assert outcome.results

    [row] = store.rows(query_id="q-1")
    assert row["pubkey"] == indexer.identity.pubkey
    assert row["transport"] == "clearnet"
    assert json.loads(row["terms"]) == ["channel", "jamming"]
    assert json.loads(row["term_dfs"]) == {"jamming": 200}
    assert row["predicted_coverage"] == 0.5
    assert row["stage"] == "responded"
    assert row["paid_msat"] == 0
    assert row["results_returned"] == 1
    assert row["results_in_merged"] == 1
    assert row["unique_results"] == 1     # nobody else returned the URL
    assert row["clicks"] is None          # filled in later by the client
    [url] = json.loads(row["result_urls"])
    assert url["url"] == "https://example.org/A/"
    assert url["norm"] == "https://example.org/A"  # trailing slash stripped
    assert row["ts"] > 0


def test_store_failure_never_fails_a_query(tmp_path):
    store = OutcomeStore(tmp_path / "outcomes.db")
    broker, _ = _searched_broker(tmp_path, store)
    store.record = None  # make the subscriber blow up on call
    broker.outcome_log.subscribe(lambda o: store.record(o))  # raises
    outcome = broker.route_query("q", max_results=5,
                                 pricing_type="per_query", max_price=0)
    assert outcome.results  # the route served anyway


def test_retention_prunes_old_rows(tmp_path):
    store = OutcomeStore(tmp_path / "outcomes.db", retention_days=7)
    broker, _ = _searched_broker(tmp_path, store)
    broker.route_query("old", max_results=5, pricing_type="per_query",
                       max_price=0, request_id="old-1")
    # Age the row past retention, then write a fresh one.
    with store._lock:
        store._con.execute("UPDATE outcomes SET ts = ?",
                           (int((time.time() - 8 * 86400) * 1000),))
        store._con.commit()
    broker.route_query("new", max_results=5, pricing_type="per_query",
                       max_price=0, request_id="new-1")
    assert store.rows(query_id="old-1") == []
    assert len(store.rows(query_id="new-1")) == 1


def test_content_profile_and_unique_shares(tmp_path):
    from broker.outcomes import NodeOutcome, OutcomeStore, RouteOutcome

    store = OutcomeStore(tmp_path / "outcomes.db")
    pk = "ab" * 32
    # Two queries; this indexer returned nostr-flavored URLs, some unique.
    for qid, urls, merged, unique in [
        ("q1", ["https://njump.net/a", "https://www.primal.net/b"], 2, 2),
        ("q2", ["https://njump.net/c", "https://nostr.band/d"], 1, 0),
    ]:
        o = RouteOutcome(query="zap", pricing_type="per_query",
                         request_id=qid,
                         started_ms=int(time.time() * 1000))
        n = NodeOutcome(pk, "http://x", 0, stage="responded",
                        returned=len(urls), survived=merged)
        n.ok = True
        o.nodes.append(n)
        o.raw[pk] = [{"id": "i", "url": u, "title": "t", "snippet": "s"}
                     for u in urls]
        srcs = [[{"pubkey": pk, "rank": 1}]] * len(urls)
        if qid == "q2":
            # q2's merged result is shared with another indexer: not unique.
            srcs[0] = [{"pubkey": pk, "rank": 1},
                       {"pubkey": "cd" * 32, "rank": 1}]
        o.results = [{"url": u, "sources": srcs[i]}
                     for i, u in enumerate(urls[:merged])]
        store.record(o)

    prof = store.content_profile(pk)
    assert prof["observed_results"] == 4
    assert prof["distinct_queries"] == 2
    domains = {d["domain"]: d["count"] for d in prof["top_domains"]}
    assert domains["njump.net"] == 2        # www. stripped, counted
    assert "primal.net" in domains
    shares = store.unique_shares()
    assert shares[pk] == 2 / 3              # 2 unique (q1) of 3 merged
