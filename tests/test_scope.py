"""Indexer selection (search-and-selection.md): the searcher may narrow WHO
is asked, never how the broker routes, ranks or merges within that set.

Each test names the acceptance criterion (§10) or rule (§7, §8) it checks.
Broker-level tests drive /client/* directly, as an agent would; the
webclient tests prove the page's proxy carries the same scope through.
"""

import base64
import gzip
import json

import httpx
import pytest

from broker.client import IndexerClient, LoopbackTransport
from broker.config import BrokerConfig
from broker.identity import Identity
from broker.messages import new_request_id
from broker.node import BrokerNode
from broker.payment import L402Receiver, MockInvoiceMinter, MockPayProvider
from broker.payment.pay import NullPayProvider
from broker.peers import PeerStore
from broker.reachability import Reachability
from broker.server import BrokerHTTPServer
from helpers import FakeIndexer
from test_webclient import _stack


class _Counted:
    """Wraps an indexer handler and counts the /query calls it answered."""

    def __init__(self, indexer, fail=False):
        self.indexer = indexer
        self.fail = fail
        self.queries = 0

    def handle(self, path, body, headers):
        if path == "/query":
            self.queries += 1
            if self.fail:
                return 500, {}, b"boom"
        return self.indexer.handle(path, body, headers)


def _indexer(tmp_path, name, price=0):
    return FakeIndexer(
        Identity.generate(), price_msat=price,
        db_path=tmp_path / f"{name}.db",
        results=[{"id": name, "url": f"https://{name}.example/",
                  "title": name, "snippet": "s"}])


def _rig(tmp_path, spec, *, receive=None, client=None, reachability=None,
         pay_provider=None, fanout=3, minter=None):
    """``spec`` is {endpoint: (price, fail)}; returns base URL, the counted
    handlers by endpoint, their pubkeys by endpoint, and the server."""
    peers = PeerStore()
    counted, pubkeys = {}, {}
    for i, (endpoint, (price, fail)) in enumerate(spec.items()):
        idx = _indexer(tmp_path, f"i{i}", price)
        assert peers.add(idx.announcement(endpoint))
        counted[endpoint] = _Counted(idx, fail)
        pubkeys[endpoint] = idx.identity.pubkey
    transport = LoopbackTransport(
        {ep: c.handle for ep, c in counted.items()})
    cfg = BrokerConfig(listen_host="127.0.0.1", listen_port=0,
                       data_dir=tmp_path, receive=receive or {},
                       client=client or {})
    receiver = (L402Receiver(b"entry-secret", tmp_path / "entry.db",
                             minter or MockInvoiceMinter())
                if receive else None)
    node = BrokerNode(Identity.generate(), peers,
                      IndexerClient(pay_provider or NullPayProvider(),
                                    transport),
                      receiver=receiver, fanout=fanout,
                      reachability=reachability)
    server = BrokerHTTPServer(node, cfg)
    server.start_background()
    return {"base": f"http://127.0.0.1:{server.port}", "counted": counted,
            "pk": pubkeys, "server": server}


@pytest.fixture
def three(tmp_path):
    r = _rig(tmp_path, {"http://a.test": (0, False),
                        "http://b.test": (0, False),
                        "http://c.test": (0, False)})
    yield r
    r["server"].shutdown()


def _search(base, **knobs):
    body = {"query": "q", "request_id": new_request_id(), "n": 5, **knobs}
    return httpx.post(base + "/client/search", json=body, timeout=10)


def _quote(base, **params):
    return httpx.get(base + "/client/quote",
                     params={"query": "q", "n": 5, **params},
                     timeout=10).json()


def _calls(r):
    return {ep: c.queries for ep, c in r["counted"].items()}


# -- §10.1 / §10.2: the full network by default; a scope is honoured exactly

def test_no_selection_queries_the_full_network(three):
    data = _search(three["base"]).json()
    assert _calls(three) == {ep: 1 for ep in three["counted"]}
    assert data["scope"]["selected"] == []
    assert sorted(data["scope"]["queried"]) == sorted(three["pk"].values())
    assert data["scope"]["skipped"] == [] and data["scope"]["failed"] == []
    # An empty list means the same thing as no list.
    assert len(_search(three["base"], pubkeys=[]).json()
               ["scope"]["queried"]) == 3


def test_a_scope_of_two_queries_exactly_those_two(three):
    a, b = three["pk"]["http://a.test"], three["pk"]["http://b.test"]
    data = _search(three["base"], pubkeys=[a, b]).json()
    assert _calls(three) == {"http://a.test": 1, "http://b.test": 1,
                             "http://c.test": 0}
    assert data["scope"]["selected"] == [a, b]
    assert sorted(data["scope"]["queried"]) == sorted([a, b])
    assert data["scope"]["skipped"] == []
    assert {s["pubkey"] for r in data["results"] for s in r["sources"]} \
        == {a, b}


def test_duplicate_pubkeys_collapse(three):
    a = three["pk"]["http://a.test"]
    data = _search(three["base"], pubkeys=[a, a]).json()
    assert data["scope"]["selected"] == [a]
    assert three["counted"]["http://a.test"].queries == 1


def test_out_of_scope_indexers_recorded_as_such(tmp_path):
    r = _rig(tmp_path, {"http://a.test": (0, False),
                        "http://b.test": (0, False)},
             client={"expose_outcome": True})
    try:
        a = r["pk"]["http://a.test"]
        out = _search(r["base"], pubkeys=[a]).json()["outcome"]
        dropped = {x["pubkey"]: x.get("dropped") for x in out["indexers"]}
        assert dropped == {a: None, r["pk"]["http://b.test"]: "out_of_scope"}
    finally:
        r["server"].shutdown()


# -- §10.5 / §7: unknown pubkeys are named, never dropped -------------------

def test_unknown_pubkey_errors_naming_it_and_asks_no_one(three):
    a = three["pk"]["http://a.test"]
    stranger = "ab" * 32
    r = _search(three["base"], pubkeys=[a, stranger])
    assert r.status_code == 400
    assert r.json()["error"] == "unknown_pubkey"
    assert r.json()["pubkeys"] == [stranger]
    assert sum(_calls(three).values()) == 0
    q = _quote(three["base"], pubkeys=f"{a},{stranger}")
    assert q["error"] == "unknown_pubkey" and q["pubkeys"] == [stranger]


@pytest.mark.parametrize("bad", ["ab" * 32, ["AB" * 32], ["xyz"], [1],
                                 ["ab" * 32] * 65])
def test_malformed_pubkeys_are_a_bad_request(three, bad):
    r = _search(three["base"], pubkeys=bad)
    assert r.status_code == 400
    assert r.json()["error"] == "bad_request"


# -- §10.6 / §4.6: Tor exclusion and reachability never widen ---------------

@pytest.fixture
def with_onion(tmp_path):
    r = _rig(tmp_path, {"http://a.test": (0, False),
                        "http://torexample23456.onion": (0, False)})
    yield r
    r["server"].shutdown()


def test_scope_emptied_by_tor_exclusion_errors_and_never_widens(with_onion):
    onion = with_onion["pk"]["http://torexample23456.onion"]
    r = _search(with_onion["base"], pubkeys=[onion], exclude_tor=True)
    assert r.status_code == 400
    body = r.json()
    assert body["error"] == "empty_scope"
    assert body["skipped"] == [{"pubkey": onion, "reason": "excluded"}]
    assert sum(_calls(with_onion).values()) == 0      # not widened
    q = _quote(with_onion["base"], pubkeys=onion, exclude_tor="1")
    assert q["error"] == "empty_scope"
    assert q["skipped"] == [{"pubkey": onion, "reason": "excluded"}]


def test_tor_exclusion_names_the_selected_indexers_it_removes(with_onion):
    a = with_onion["pk"]["http://a.test"]
    onion = with_onion["pk"]["http://torexample23456.onion"]
    q = _quote(with_onion["base"], pubkeys=f"{a},{onion}", exclude_tor="1")
    assert q["scope"]["queried"] == [a]
    assert q["scope"]["skipped"] == [{"pubkey": onion,
                                      "reason": "excluded"}]
    data = _search(with_onion["base"], pubkeys=[a, onion],
                   exclude_tor=True).json()
    assert data["scope"]["queried"] == [a]
    assert data["scope"]["skipped"] == [{"pubkey": onion,
                                         "reason": "excluded"}]


@pytest.fixture
def reach_rig(tmp_path):
    reach = Reachability(startup_grace_seconds=0)
    r = _rig(tmp_path, {"http://up.test": (0, False),
                        "http://never.test": (0, False),
                        "http://down.test": (0, False)},
             reachability=reach)
    reach.note_success(r["pk"]["http://up.test"])
    reach.note_failure(r["pk"]["http://down.test"], "connection refused")
    yield r
    r["server"].shutdown()


def test_unreachable_selection_runs_on_the_rest_and_says_so(reach_rig):
    up, never, down = (reach_rig["pk"][f"http://{n}.test"]
                       for n in ("up", "never", "down"))
    data = _search(reach_rig["base"], pubkeys=[up, never, down]).json()
    assert data["scope"]["queried"] == [up]
    assert sorted(data["scope"]["skipped"], key=lambda x: x["reason"]) == [
        {"pubkey": never, "reason": "never_contacted"},
        {"pubkey": down, "reason": "unreachable"}]


def test_all_selected_unreachable_blocks_the_search(reach_rig):
    down = reach_rig["pk"]["http://down.test"]
    r = _search(reach_rig["base"], pubkeys=[down])
    assert r.status_code == 400 and r.json()["error"] == "empty_scope"
    assert sum(_calls(reach_rig).values()) == 0


# -- §10.7 / §4.5: the estimate is scope-aware; the cap names indexers -----

@pytest.fixture
def paid(tmp_path):
    r = _rig(tmp_path, {"http://cheap.test": (1000, False),
                        "http://dear.test": (3000, False)},
             receive={"pricing_mode": "quoted", "margin_flat_msat": 500,
                      "minter": "mock"},
             pay_provider=MockPayProvider())
    yield r
    r["server"].shutdown()


def test_quote_is_the_estimate_over_the_selected_set(paid):
    cheap, dear = paid["pk"]["http://cheap.test"], paid["pk"]["http://dear.test"]
    assert _quote(paid["base"])["quote_msat"] == 4500
    assert _quote(paid["base"], pubkeys=cheap)["quote_msat"] == 1500
    q = _quote(paid["base"], pubkeys=dear)
    assert q["quote_msat"] == 3500
    assert q["indexers_considered"] == 1
    assert q["scope"] == {"selected": [dear], "queried": [dear],
                          "skipped": []}


def test_cap_that_prices_out_the_scope_names_the_indexers(paid):
    cheap, dear = paid["pk"]["http://cheap.test"], paid["pk"]["http://dear.test"]
    # 2000 msat leaves 1500 downstream: the dear indexer cannot be asked.
    q = _quote(paid["base"], pubkeys=dear, max_msat=2000)
    assert q["error"] == "empty_scope"
    assert q["skipped"] == [{"pubkey": dear, "reason": "budget"}]
    r = _search(paid["base"], pubkeys=[dear], max_msat=2000)
    assert r.status_code == 400 and r.json()["error"] == "empty_scope"
    # A cap that keeps part of the scope keeps that part, and says what
    # it lost — the broker's allocation, unchanged, within the scope.
    q = _quote(paid["base"], pubkeys=f"{cheap},{dear}", max_msat=2000)
    assert q["quote_msat"] == 1500
    assert q["scope"]["skipped"] == [{"pubkey": dear, "reason": "budget"}]


def test_paid_scoped_search_charges_and_reports_the_spend(paid):
    import re

    cheap = paid["pk"]["http://cheap.test"]
    body = json.dumps({"query": "q", "request_id": new_request_id(),
                       "n": 5, "pubkeys": [cheap]}).encode()
    first = httpx.post(paid["base"] + "/client/search", content=body,
                       timeout=10)
    assert first.status_code == 402
    macaroon, invoice = re.search(
        r'macaroon="([^"]+)", invoice="([^"]+)"',
        first.headers["WWW-Authenticate"]).groups()
    preimage = MockPayProvider().pay(invoice, 5000)
    done = httpx.post(paid["base"] + "/client/search", content=body,
                      headers={"Authorization": f"L402 {macaroon}:{preimage}"},
                      timeout=10).json()
    assert done["charged_msat"] == 1500
    assert done["scope"]["queried"] == [cheap]
    # cheap sees two /query hits (its own 402, then the paid retry);
    # the unselected indexer sees none.
    assert _calls(paid) == {"http://cheap.test": 2, "http://dear.test": 0}


def test_head_start_routes_on_the_scope_that_was_paid_for(tmp_path):
    """The broker routes on settlement, before the preimage arrives —
    that early route must carry the scope too."""
    import re
    import time

    minter = MockInvoiceMinter()
    r = _rig(tmp_path, {"http://cheap.test": (1000, False),
                        "http://dear.test": (3000, False)},
             receive={"pricing_mode": "quoted", "margin_flat_msat": 500,
                      "minter": "mock", "head_start_poll_seconds": 0.05},
             pay_provider=MockPayProvider(), minter=minter)
    try:
        cheap = r["pk"]["http://cheap.test"]
        body = json.dumps({"query": "q", "request_id": new_request_id(),
                           "n": 5, "pubkeys": [cheap]}).encode()
        first = httpx.post(r["base"] + "/client/search", content=body,
                           timeout=10)
        macaroon, invoice = re.search(
            r'macaroon="([^"]+)", invoice="([^"]+)"',
            first.headers["WWW-Authenticate"]).groups()
        from broker.payment.l402 import decode_claims
        payment_hash = decode_claims(macaroon)["h"]
        minter.mark_settled(payment_hash)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not httpx.get(
                r["base"] + f"/client/status?hash={payment_hash}",
                timeout=10).json()["ready"]:
            time.sleep(0.05)
        assert _calls(r) == {"http://cheap.test": 2, "http://dear.test": 0}
        preimage = MockPayProvider().pay(invoice, 5000)
        done = httpx.post(r["base"] + "/client/search", content=body,
                          headers={"Authorization":
                                   f"L402 {macaroon}:{preimage}"},
                          timeout=10).json()
        assert done["scope"]["queried"] == [cheap]
        assert _calls(r)["http://cheap.test"] == 2   # served from prefetch
    finally:
        r["server"].shutdown()


def test_free_search_reports_zero_spend(three):
    assert _search(three["base"]).json()["charged_msat"] == 0


# -- §6: a selected indexer that fails is named ----------------------------

def test_failed_indexer_in_scope_is_named(tmp_path):
    r = _rig(tmp_path, {"http://ok.test": (0, False),
                        "http://bad.test": (0, True)})
    try:
        ok, bad = r["pk"]["http://ok.test"], r["pk"]["http://bad.test"]
        data = _search(r["base"], pubkeys=[ok, bad]).json()
        assert sorted(data["scope"]["queried"]) == sorted([ok, bad])
        assert data["scope"]["failed"] == [bad]
        assert len(data["results"]) == 1
    finally:
        r["server"].shutdown()


def test_fanout_still_applies_within_a_scope(tmp_path):
    """Selection constrains the set; the broker still decides within it —
    a scope larger than the fanout is cut by the broker, and says so."""
    spec = {f"http://n{i}.test": (0, False) for i in range(3)}
    r = _rig(tmp_path, spec, fanout=2)
    try:
        data = _search(r["base"], pubkeys=list(r["pk"].values())).json()
        assert len(data["scope"]["queried"]) == 2
        assert [s["reason"] for s in data["scope"]["skipped"]] == ["fanout"]
    finally:
        r["server"].shutdown()


# -- §8: the network listing -----------------------------------------------

def test_listing_has_what_an_agent_needs_to_choose(reach_rig):
    view = httpx.get(reach_rig["base"] + "/client/peers", timeout=10).json()
    by_pk = {x["pubkey"]: x for x in view["indexers"]}
    up = by_pk[reach_rig["pk"]["http://up.test"]]
    down = by_pk[reach_rig["pk"]["http://down.test"]]
    for x in (up, down):
        for key in ("pubkey", "name", "covers", "corpus", "last_ingest",
                    "pricing", "transport", "reach", "searchable"):
            assert key in x, key
    assert up["searchable"] is True and "unsearchable_reason" not in up
    assert down["searchable"] is False
    assert down["unsearchable_reason"] == "unreachable"


def test_self_description_documents_scope(three):
    doc = httpx.get(three["base"] + "/", timeout=10).json()
    assert "pubkeys?" in doc["client_paths"]["/client/search"]
    assert "pubkeys" in doc["client_paths"]["/client/quote"]
    assert set(doc["scope"]["errors"]) == {"unknown_pubkey", "empty_scope",
                                           "unsatisfiable"}
    assert "never names" in doc["scope"]["identity"]


# -- the page's proxy carries the same scope -------------------------------

@pytest.fixture
def web(tmp_path):
    indexers = {"http://a.test": _indexer(tmp_path, "a"),
                "http://b.test": _indexer(tmp_path, "b")}
    base, web_srv, broker_srv = _stack(
        tmp_path, indexers=indexers, receive={},
        pay_provider=NullPayProvider())
    pks = {ep: i.identity.pubkey for ep, i in indexers.items()}
    yield base, pks
    web_srv.shutdown()
    broker_srv.shutdown()


def test_webclient_passes_scope_through(web):
    base, pks = web
    a = pks["http://a.test"]
    q = httpx.get(base + "/api/quote", params={"q": "q", "pubkeys": a},
                  timeout=10).json()
    assert q["scope"]["queried"] == [a]
    data = httpx.post(base + "/api/search", timeout=10,
                      json={"q": "q", "pubkeys": [a]}).json()
    assert data["scope"]["selected"] == [a]
    assert data["scope"]["queried"] == [a]
    assert data["charged_msat"] == 0
    assert [r["url"] for r in data["results"]] == ["https://a.example/"]


def test_webclient_surfaces_scope_errors(web):
    base, _pks = web
    stranger = "cd" * 32
    data = httpx.post(base + "/api/search", timeout=10,
                      json={"q": "q", "pubkeys": [stranger]}).json()
    assert data == {"error": "unknown_pubkey", "pubkeys": [stranger]}
    q = httpx.get(base + "/api/quote",
                  params={"q": "q", "pubkeys": stranger}, timeout=10).json()
    assert q == {"error": "unknown_pubkey", "pubkeys": [stranger]}
    # Malformed is refused at the page's own door, not dropped to "full".
    assert httpx.post(base + "/api/search", timeout=10,
                      json={"q": "q", "pubkeys": ["nope"]}).json() \
        == {"error": "malformed_scope"}
    assert httpx.get(base + "/api/quote",
                     params={"q": "q", "pubkeys": "nope"},
                     timeout=10).json() == {"error": "malformed_scope"}


def test_webclient_listing_fields(web):
    base, _pks = web
    x = httpx.get(base + "/api/peers", timeout=10).json()["indexers"][0]
    assert x["searchable"] is True
    assert "name" in x and "covers" in x


def test_scope_is_bound_into_the_paid_request_bytes(tmp_path):
    """The macaroon binds the envelope's exact bytes, so the scope a
    customer paid for is the scope the replay routes on."""
    indexers = {"http://a.test": _indexer(tmp_path, "a", 1000),
                "http://b.test": _indexer(tmp_path, "b", 1000)}
    base, web_srv, broker_srv = _stack(
        tmp_path, indexers=indexers,
        receive={"pricing_mode": "quoted", "margin_flat_msat": 500,
                 "minter": "mock"},
        pay_provider=MockPayProvider())
    try:
        a = indexers["http://a.test"].identity.pubkey
        ch = httpx.post(base + "/api/search", timeout=10,
                        json={"q": "q", "pubkeys": [a]}).json()
        assert ch["payment_required"] is True
        assert ch["amount_msat"] == 1500
        sent = json.loads(base64.b64decode(ch["request_b64"]))
        assert sent["pubkeys"] == [a]
    finally:
        web_srv.shutdown()
        broker_srv.shutdown()


# -- gzip -------------------------------------------------------------------

def test_page_is_gzipped_when_accepted(web):
    base, _pks = web
    raw = httpx.get(base + "/", headers={"Accept-Encoding": "identity"},
                    timeout=10)
    assert "Content-Encoding" not in raw.headers
    assert raw.headers["Vary"] == "Accept-Encoding"
    with httpx.Client() as c:
        req = c.build_request("GET", base + "/",
                              headers={"Accept-Encoding": "gzip"})
        r = c.send(req, stream=True)
        compressed = b"".join(r.iter_raw())   # the bytes on the wire
        r.close()
    assert r.headers["Content-Encoding"] == "gzip"
    # base64 fonts compress least; the page still more than a third smaller
    assert len(compressed) < len(raw.content) * 0.6
    assert gzip.decompress(compressed) == raw.content
