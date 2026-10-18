"""The broker's HTTP face, exercised over real sockets.

A live BrokerHTTPServer on an ephemeral port, with a fake paid indexer wired
behind it, driven by httpx: gossip in (/announce, /peers), refusals as
protocol Errors, and the customer surface — /client/quote, /client/peers,
and the paid /client/search door: 402 challenge with a signed payment
challenge body, mock payment, byte-identical replay, attributed results,
and the outcome record behind the visibility flags.
"""

import json

import httpx
import pytest

from broker.client import IndexerClient, LoopbackTransport
from broker.config import BrokerConfig
from broker.identity import Identity
from broker.messages import new_request_id, peer_list_request, query_request
from broker.node import BrokerNode
from broker.payment import (
    L402Receiver,
    MockInvoiceMinter,
    MockPayProvider,
    NullPayProvider,
)
from broker.peers import PeerStore
from broker.server import BrokerHTTPServer
from broker.wire import accept
from helpers import FakeIndexer


def _rig(tmp_path, *, price_msat=0, receive=None, client=None,
         pay_provider=None, indexer_cls=FakeIndexer, minter=None):
    indexer = indexer_cls(
        Identity.generate(), price_msat=price_msat,
        db_path=tmp_path / "idx.db",
        results=[{"id": "d1", "url": "https://example.org/a",
                  "title": "Alpha", "snippet": "s"}])
    peers = PeerStore()
    assert peers.add(indexer.announcement("http://indexer.test"))
    transport = LoopbackTransport({"http://indexer.test": indexer.handle})
    cfg = BrokerConfig(listen_host="127.0.0.1", listen_port=0,
                       data_dir=tmp_path, receive=receive or {},
                       client=client or {})
    receiver = None
    if receive:
        receiver = L402Receiver(b"entry-secret", tmp_path / "entry.db",
                                minter or MockInvoiceMinter())
    node = BrokerNode(Identity.generate(), peers,
                      IndexerClient(pay_provider or NullPayProvider(),
                                    transport),
                      receiver=receiver)
    server = BrokerHTTPServer(node, cfg)
    server.start_background()
    return {"base": f"http://127.0.0.1:{server.port}", "node": node,
            "indexer": indexer, "server": server, "tmp": tmp_path}


@pytest.fixture
def rig(tmp_path):
    """A running broker HTTP server with one free in-process indexer."""
    r = _rig(tmp_path)
    yield r
    r["server"].shutdown()


def _post(base, path, message, headers=None):
    return httpx.post(base + path, content=json.dumps(message).encode(),
                      headers={"Content-Type": "application/json",
                               **(headers or {})}, timeout=10)


def _envelope(query="q", n=5, **extra):
    return {"query": query, "request_id": new_request_id(), "n": n, **extra}


def test_free_search_over_http(rig):
    envelope = _envelope()
    r = _post(rig["base"], "/client/search", envelope)
    assert r.status_code == 200
    data = r.json()
    assert data["request_id"] == envelope["request_id"]
    [result] = data["results"]
    assert result["url"] == "https://example.org/a"
    # Attribution is always on: the customer may know who served them.
    assert result["sources"] == [
        {"pubkey": rig["indexer"].identity.pubkey, "rank": 1}]
    assert "outcome" not in data          # visibility flags default OFF


def test_prices_default_to_zero_unless_set(tmp_path):
    """Nothing is charged unless a price is written in the config: a
    price-list entry without a price, and quoted mode with no margin over
    free indexers, both serve without a 402 — and with nothing to charge,
    no receiver is built at all."""
    from broker.config import build_receiver

    unpriced = BrokerConfig(data_dir=tmp_path,
                            receive={"pricing": [{"type": "per_query"}]})
    assert unpriced.entry_price("per_query") == 0
    assert unpriced.entry_price("per_result") is None   # not offered
    assert not unpriced.entry_charges
    assert build_receiver(unpriced) is None
    quoted = BrokerConfig(data_dir=tmp_path,
                          receive={"pricing_mode": "quoted"})
    assert quoted.margin_flat_msat == quoted.margin_percent == 0
    assert quoted.with_margin(0) == 0

    for receive in ({"pricing": [{"type": "per_query"}]},
                    {"pricing_mode": "quoted"}):
        r = _rig(tmp_path / receive.get("pricing_mode", "static"),
                 receive=receive)
        try:
            resp = _post(r["base"], "/client/search", _envelope())
            assert resp.status_code == 200, receive
            assert resp.json()["charged_msat"] == 0
        finally:
            r["server"].shutdown()


def test_outcome_and_raw_sets_behind_the_flags(tmp_path):
    r = _rig(tmp_path, client={"expose_outcome": True,
                               "expose_raw_sets": True})
    try:
        resp = _post(r["base"], "/client/search",
                     _envelope("alpha beta")).json()
        outcome = resp["outcome"]
        assert outcome["query"] == "alpha beta"
        assert outcome["ask"]["n"] == 5
        assert outcome["cost_msat"] == 0
        [node] = outcome["indexers"]
        assert node["pubkey"] == r["indexer"].identity.pubkey
        assert node["stage"] == "responded"
        assert node["result_count"] == 1 and node["survived"] == 1
        assert node["transport"] == "clearnet"
        # raw is the set exactly as received, before merge touched it.
        assert outcome["raw"][node["pubkey"]][0]["id"] == "d1"
        assert outcome["finished"] >= outcome["started"] > 0
    finally:
        r["server"].shutdown()


def test_client_quote_and_unsatisfiable_cap(tmp_path):
    r = _rig(tmp_path, receive={"pricing": [{"type": "per_query",
                                             "price": 5000}],
                                "minter": "mock"})
    try:
        q = httpx.get(r["base"] + "/client/quote",
                      params={"query": "q", "n": 5}, timeout=10).json()
        assert q["quote_msat"] == 5000
        assert q["ask"]["n"] == 5
        assert q["indexers_considered"] == 1
        capped = httpx.get(r["base"] + "/client/quote",
                           params={"query": "q", "n": 5, "max_msat": 100},
                           timeout=10).json()
        assert capped["error"] == "unsatisfiable"
        assert "5000" in capped["reason"]
        # The same cap on a real search refuses BEFORE minting an invoice.
        search = _post(r["base"], "/client/search",
                       _envelope(max_msat=100))
        assert search.status_code == 400
        assert search.json()["error"] == "unsatisfiable"
    finally:
        r["server"].shutdown()


def test_client_peers_carries_identities(rig):
    data = httpx.get(rig["base"] + "/client/peers", timeout=10).json()
    assert data["count"] == 1
    [x] = data["indexers"]
    assert x["pubkey"] == rig["indexer"].identity.pubkey
    assert x["endpoint"] == "http://indexer.test"
    assert x["transport"] == "clearnet"
    assert x["corpus"]["document_count"] == 1000
    assert x["pricing"] == [{"type": "per_query", "price": 0}]


def test_get_root_describes_the_services(rig):
    """An agent that lands on the broker with nothing but the hostname can
    learn everything: the client door, the L402 loop, quote and peers."""
    r = httpx.get(rig["base"] + "/", timeout=10)
    assert r.status_code == 200
    doc = r.json()
    assert doc["protocol"] == "ditta/v0"
    assert doc["pubkey"] == rig["node"].identity.pubkey
    assert "/client/search" in doc["client_paths"]
    assert "L402" in doc["client_paths"]["/client/search"]
    assert "/client/search" in doc["protocol_paths"]["/query"]
    assert "GET /" in httpx.get(rig["base"] + "/nope",
                                timeout=10).json()["hint"]


def test_protocol_query_refused_on_a_pure_broker(rig):
    """On the wire a broker sends queries, it does not answer them: a node
    not declaring `indexer` answers /query with `rejected` (Transport)."""
    request = query_request("q", 5, "per_query", 0)
    r = _post(rig["base"], "/query", request)
    err, _ = accept(r.content, "error")
    assert err["code"] == "rejected"
    assert err["request_id"] == request["request_id"]


def test_announce_and_peers_round_trip(rig):
    newcomer = FakeIndexer(Identity.generate(), 0, rig["tmp"] / "n.db",
                           results=[])
    announcement = newcomer.announcement("http://newcomer.test")
    r = _post(rig["base"], "/announce", announcement)
    assert r.status_code == 200 and r.content == b""

    asker = Identity.generate()
    request = asker.signed(peer_list_request(asker.pubkey),
                           "peer_list_request")
    r = _post(rig["base"], "/peers", request)
    assert r.status_code == 200
    response, _ = accept(r.content, "peer_list_response")
    endpoints = {a["endpoint"] for a in response["peers"]}
    assert "http://newcomer.test" in endpoints


def test_malformed_body_answered_with_wire_error(rig):
    r = httpx.post(rig["base"] + "/query", content=b'{"query": 1.5}',
                   timeout=10)
    assert r.status_code == 400
    err, _ = accept(r.content, "error")
    assert err["code"] == "malformed"


def test_unsigned_peer_request_is_malformed(rig):
    r = _post(rig["base"], "/peers", peer_list_request("ab" * 32))
    err, _ = accept(r.content, "error")
    assert err["code"] == "malformed"  # missing mandatory signature


def test_corpus_refused_by_broker(rig):
    asker = Identity.generate()
    request = asker.signed({
        "pubkey": asker.pubkey, "request_id": new_request_id(),
        "statistics_version": 0}, "corpus_statistics_request")
    r = _post(rig["base"], "/corpus", request)
    err, _ = accept(r.content, "error")
    assert err["code"] == "rejected"
    assert err["request_id"] == request["request_id"]


def test_paid_entrance_full_l402_loop(tmp_path):
    """The broker CHARGES at the door: 402, pay, retry, results."""
    r = _rig(tmp_path, receive={"pricing": [{"type": "per_query",
                                             "price": 1500}],
                                "minter": "mock"})
    base = r["base"]
    try:
        envelope = _envelope()
        body = json.dumps(envelope).encode()

        first = httpx.post(base + "/client/search", content=body, timeout=10)
        assert first.status_code == 402
        challenge = first.headers["WWW-Authenticate"]
        assert challenge.startswith('L402 macaroon="')

        # The 402 body is plain /client JSON that duplicates the header
        # and answers OUR request; no protocol message at this door.
        challenge_body = first.json()
        assert challenge_body["error"] == "payment_required"
        assert challenge_body["request_id"] == envelope["request_id"]

        # Settle the mock invoice like a paying customer would.
        import re
        macaroon, invoice = re.search(
            r'macaroon="([^"]+)", invoice="([^"]+)"', challenge).groups()
        assert challenge_body["macaroon"] == macaroon
        assert challenge_body["invoice"] == invoice
        preimage = MockPayProvider().pay(invoice, 2000)

        retry = httpx.post(
            base + "/client/search", content=body,
            headers={"Authorization": f"L402 {macaroon}:{preimage}"},
            timeout=10)
        assert retry.status_code == 200
        assert retry.json()["results"][0]["url"] == "https://example.org/a"

        # The credential was consumed by SERVICE: presenting it again, even
        # with the identical bytes, gets a fresh challenge.
        assert httpx.post(
            base + "/client/search", content=body,
            headers={"Authorization": f"L402 {macaroon}:{preimage}"},
            timeout=10).status_code == 402
        again = httpx.post(base + "/client/search", content=body, timeout=10)
        assert again.status_code == 402
    finally:
        r["server"].shutdown()


class _Flaky:
    """An indexer that can be switched off: dialing it fails below HTTP."""

    def __init__(self, *args, **kwargs):
        self.inner = FakeIndexer(*args, **kwargs)
        self.identity = self.inner.identity
        self.up = False

    def announcement(self, endpoint):
        return self.inner.announcement(endpoint)

    def handle(self, path, body, headers):
        if not self.up:
            raise OSError("[Errno 111] Connection refused")
        return self.inner.handle(path, body, headers)


def test_credential_survives_a_route_that_produced_nothing(tmp_path):
    """Consume on service, period: a paid credential presented while every
    indexer is down is NOT spent — the payer retries later and gets the
    results already paid for."""
    r = _rig(tmp_path, indexer_cls=_Flaky,
             receive={"pricing": [{"type": "per_query", "price": 1500}],
                      "minter": "mock"})
    base = r["base"]
    try:
        body = json.dumps(_envelope()).encode()
        first = httpx.post(base + "/client/search", content=body, timeout=10)
        assert first.status_code == 402
        import re
        macaroon, invoice = re.search(
            r'macaroon="([^"]+)", invoice="([^"]+)"',
            first.headers["WWW-Authenticate"]).groups()
        preimage = MockPayProvider().pay(invoice, 2000)
        auth = {"Authorization": f"L402 {macaroon}:{preimage}"}

        # Indexer down: an empty result list goes out, flagged so the client
        # keeps its pending payment — and the credential is NOT consumed.
        resp = httpx.post(base + "/client/search", content=body,
                          headers=auth, timeout=10)
        assert resp.status_code == 200
        assert resp.headers["X-Credential-Retained"] == "1"
        assert resp.json()["results"] == []

        # The network recovers; the SAME credential now redeems for real.
        r["indexer"].up = True
        resp = httpx.post(base + "/client/search", content=body,
                          headers=auth, timeout=10)
        assert resp.status_code == 200
        assert "X-Credential-Retained" not in resp.headers
        assert resp.json()["results"][0]["url"] == "https://example.org/a"

        # ...and only now is it spent.
        assert httpx.post(base + "/client/search", content=body,
                          headers=auth, timeout=10).status_code == 402
    finally:
        r["server"].shutdown()


class _Counting:
    def __init__(self, *args, **kwargs):
        self.inner = FakeIndexer(*args, **kwargs)
        self.identity = self.inner.identity
        self.queries = 0

    def announcement(self, endpoint):
        return self.inner.announcement(endpoint)

    def handle(self, path, body, headers):
        if path == "/query":
            self.queries += 1
        return self.inner.handle(path, body, headers)


def test_status_asks_the_node_and_an_empty_preimage_slot_redeems(tmp_path):
    """With the head start off the prefetcher knows nothing; /client/status
    still answers from the node, and the byte-identical retry with the
    preimage slot empty redeems on that same witness — for the site, which
    carries the shared secret. An agent on the same door does not."""
    minter = MockInvoiceMinter()
    r = _rig(tmp_path, minter=minter,
             receive={"pricing": [{"type": "per_query", "price": 1500}],
                      "minter": "mock", "head_start": False,
                      "site_secret": "s3cret"})
    base = r["base"]
    try:
        envelope = _envelope()
        body = json.dumps(envelope).encode()
        first = httpx.post(base + "/client/search", content=body, timeout=10)
        assert first.status_code == 402
        challenge = first.json()
        from broker.payment.l402 import decode_claims
        h = decode_claims(challenge["macaroon"])["h"]
        agent = {"Authorization": f'L402 {challenge["macaroon"]}:'}
        bare = {**agent, "X-Site-Secret": "s3cret"}

        assert httpx.get(base + f"/client/status?hash={h}",
                         timeout=10).json() == {"settled": False,
                                                "ready": False}
        # Unpaid: the bare retry earns a fresh challenge, as any bad token.
        assert httpx.post(base + "/client/search", content=body,
                          headers=bare, timeout=10).status_code == 402

        minter.mark_settled(h)
        assert httpx.get(base + f"/client/status?hash={h}",
                         timeout=10).json() == {"settled": True,
                                                "ready": False}
        # Settled, but no site secret (an agent) or the wrong one: still a
        # fresh challenge — the agent API keeps L402's rule.
        assert httpx.post(base + "/client/search", content=body,
                          headers=agent, timeout=10).status_code == 402
        assert httpx.post(
            base + "/client/search", content=body,
            headers={**agent, "X-Site-Secret": "wrong"},
            timeout=10).status_code == 402
        resp = httpx.post(base + "/client/search", content=body,
                          headers=bare, timeout=10)
        assert resp.status_code == 200
        assert resp.json()["results"][0]["url"] == "https://example.org/a"
        # Consumed by that service.
        assert httpx.post(base + "/client/search", content=body,
                          headers=bare, timeout=10).status_code == 402
        # A malformed hash never reaches the node.
        assert httpx.get(base + "/client/status?hash=zz",
                         timeout=10).json()["settled"] is False
    finally:
        r["server"].shutdown()


def test_without_a_site_secret_nothing_is_witnessed(tmp_path):
    """No receive.site_secret: an empty preimage slot never redeems, even
    with the right (empty) secret offered — the site pastes like anyone."""
    minter = MockInvoiceMinter()
    r = _rig(tmp_path, minter=minter,
             receive={"pricing": [{"type": "per_query", "price": 1500}],
                      "minter": "mock", "head_start": False})
    base = r["base"]
    try:
        body = json.dumps(_envelope()).encode()
        first = httpx.post(base + "/client/search", content=body, timeout=10)
        challenge = first.json()
        from broker.payment.l402 import decode_claims
        minter.mark_settled(decode_claims(challenge["macaroon"])["h"])
        for secret in ("", "anything"):
            resp = httpx.post(
                base + "/client/search", content=body,
                headers={"Authorization": f'L402 {challenge["macaroon"]}:',
                         "X-Site-Secret": secret}, timeout=10)
            assert resp.status_code == 402
        preimage = MockPayProvider().pay(challenge["invoice"], 2000)
        assert httpx.post(
            base + "/client/search", content=body,
            headers={"Authorization":
                     f'L402 {challenge["macaroon"]}:{preimage}'},
            timeout=10).status_code == 200
    finally:
        r["server"].shutdown()


def test_head_start_routes_on_settlement_before_the_preimage(tmp_path):
    """The broker sees its own invoice settle and routes while the payer is
    still fetching the preimage; redemption serves the prefetched outcome
    without asking the indexers a second time."""
    import time

    minter = MockInvoiceMinter()
    r = _rig(tmp_path, indexer_cls=_Counting, minter=minter,
             receive={"pricing": [{"type": "per_query", "price": 1500}],
                      "minter": "mock", "head_start_poll_seconds": 0.05},
             client={"expose_outcome": True})
    base = r["base"]
    counting = r["indexer"]
    try:
        envelope = _envelope()
        body = json.dumps(envelope).encode()
        first = httpx.post(base + "/client/search", content=body, timeout=10)
        assert first.status_code == 402
        challenge = first.json()
        from broker.payment.l402 import decode_claims
        claims = decode_claims(challenge["macaroon"])
        assert counting.queries == 0

        # The wallet pays; our node sees settlement. No preimage anywhere.
        minter.mark_settled(claims["h"])
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            status = httpx.get(
                base + f"/client/status?hash={claims['h']}",
                timeout=10).json()
            if status["ready"]:
                break
            time.sleep(0.05)
        assert status["ready"], "prefetch never completed"
        assert counting.queries == 1     # routed on settlement alone

        # Redemption serves the prefetched outcome: no second fan-out.
        preimage = MockPayProvider().pay(challenge["invoice"], 2000)
        resp = httpx.post(
            base + "/client/search", content=body,
            headers={"Authorization":
                     f'L402 {challenge["macaroon"]}:{preimage}'},
            timeout=10)
        assert resp.status_code == 200
        data = resp.json()
        assert data["request_id"] == envelope["request_id"]
        assert data["results"][0]["url"] == "https://example.org/a"
        assert data["outcome"]["charged_msat"] == 1500
        assert counting.queries == 1     # the work happened exactly once

        # And the credential was consumed by that service.
        assert httpx.post(
            base + "/client/search", content=body,
            headers={"Authorization":
                     f'L402 {challenge["macaroon"]}:{preimage}'},
            timeout=10).status_code == 402
    finally:
        r["server"].shutdown()


def test_peer_cards_profile_and_claimed_vs_demonstrated(tmp_path):
    """The card says what the broker THINKS the index covers (clauses
    traceable to measurements), and — once the outcome store has delivery
    data — which claimed terms were actually demonstrated."""
    from broker.outcomes import OutcomeStore
    from broker.stats import StatsCache

    indexer = FakeIndexer(
        Identity.generate(), price_msat=0, db_path=tmp_path / "idx.db",
        results=[{"id": "d1", "url": "https://example.org/a",
                  "title": "Alpha", "snippet": "s"}],
        terms={"jamming": (200, 1), "mitigation": (120, 1)})
    peers = PeerStore()
    assert peers.add(indexer.announcement("http://indexer.test"))
    stats = StatsCache()
    stats.put(indexer.identity.pubkey,
              {"jamming": (200, 1), "mitigation": (120, 1)}, updated=1)
    node = BrokerNode(
        Identity.generate(), peers,
        IndexerClient(NullPayProvider(),
                      LoopbackTransport({"http://indexer.test":
                                         indexer.handle})),
        stats=stats)
    store = OutcomeStore(tmp_path / "outcomes.db", stats=stats)
    node.outcome_log.subscribe(store.record)
    cfg = BrokerConfig(listen_host="127.0.0.1", listen_port=0,
                       data_dir=tmp_path)
    server = BrokerHTTPServer(node, cfg, outcome_store=store)
    server.start_background()
    base = f"http://127.0.0.1:{server.port}"
    try:
        [x] = httpx.get(base + "/client/peers",
                        timeout=10).json()["indexers"]
        texts = [c["text"] for c in x["profile"]]
        assert any("small, focused corpus" in t for t in texts)
        assert any("strongest distinctive coverage: jamming" in t
                   for t in texts)
        assert all(c["detail"] for c in x["profile"])  # every clause cites
        assert x["vocabulary"][0]["term"] == "jamming"
        assert x["evidence"] == {"demonstrated": 0, "tried": 0,
                                 "answered": 0}
        assert "evidence" not in x["vocabulary"][0]    # untested so far

        # One answered query later, the claim about "jamming" is
        # DEMONSTRATED — the card's layer-2 upgrade.
        r = _post(base, "/client/search", _envelope("jamming attacks"))
        assert r.status_code == 200
        [x] = httpx.get(base + "/client/peers",
                        timeout=10).json()["indexers"]
        assert x["evidence"]["demonstrated"] == 1
        assert x["evidence"]["tried"] == 1
        assert x["evidence"]["answered"] == 1
        jam = next(v for v in x["vocabulary"] if v["term"] == "jamming")
        assert jam["evidence"] == "delivered"
        mit = next(v for v in x["vocabulary"] if v["term"] == "mitigation")
        assert "evidence" not in mit                   # never routed a query
    finally:
        server.shutdown()


def test_beacon_credits_clicks_through_the_store(tmp_path):
    from broker.outcomes import OutcomeStore

    store = OutcomeStore(tmp_path / "outcomes.db")
    r = _rig(tmp_path)
    r["node"].outcome_log.subscribe(store.record)
    r["server"].outcomes = store
    try:
        envelope = _envelope("alpha")
        assert _post(r["base"], "/client/search",
                     envelope).status_code == 200
        beacon = _post(r["base"], "/client/beacon", {
            "request_id": envelope["request_id"],
            "url": "https://example.org/a"})
        assert beacon.json()["ok"] is True
        rows = store.rows(query_id=envelope["request_id"])
        assert rows[0]["clicks"] == 1.0
        # Unknown URLs and queries are quietly not credited.
        assert _post(r["base"], "/client/beacon", {
            "request_id": envelope["request_id"],
            "url": "https://nowhere.example/"}).json()["ok"] is False
    finally:
        r["server"].shutdown()
