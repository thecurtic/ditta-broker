"""The visibility front end: the manual L402 flow plus the receipt, over
real HTTP.

web client -> broker -> indexers, wired over loopback: quote while typing
(knobs included), search -> 402 with QR and signed challenge, settlement
seen on the broker's own node, byte-identical replay with the preimage
slot empty (the paste is the fallback), attributed results, and the outcome
record (card states, ask-vs-did, raw sets) behind the broker's visibility
flags.
"""

import base64
import hashlib
import json

import httpx
import pytest

from broker.client import IndexerClient, LoopbackTransport
from broker.config import BrokerConfig, build_receiver
from broker.gossip import Gossip
from broker.identity import Identity
from broker.node import BrokerNode
from broker.payment import L402Receiver, MockInvoiceMinter, MockPayProvider
from broker.payment.pay import NullPayProvider
from broker.peers import PeerStore
from broker.server import BrokerHTTPServer
from broker.stats import StatsCache
from broker.webclient import WebClientConfig, WebClientServer, sanitize_results
from helpers import FakeIndexer

HOSTILE_RESULTS = [
    {"id": "ok", "url": "https://example.org/fine",
     "title": "A fine result", "snippet": "harmless text",
     "sources": [{"pubkey": "aa" * 32, "rank": 1}]},
    {"id": "xss", "url": "javascript:alert(1)",
     "title": "<script>alert('title')</script>",
     "snippet": "<img src=x onerror=alert(2)>",
     "sources": [{"pubkey": "<script>", "rank": 1},
                 {"pubkey": "bb" * 32, "rank": "2"}]},
]


def test_sanitize_results_strips_unfollowable_urls_and_bad_sources():
    out = sanitize_results(HOSTILE_RESULTS)
    assert out[0]["url"] == "https://example.org/fine"
    assert out[0]["sources"] == [{"pubkey": "aa" * 32, "rank": 1}]
    assert out[1]["url"] is None
    assert out[1]["title"] == "<script>alert('title')</script>"
    # A non-hex pubkey never reaches the page (it becomes an element key).
    assert out[1]["sources"] == [{"pubkey": "bb" * 32, "rank": 0}]


def _stack(tmp_path, *, indexers, receive, pay_provider, client=None,
           minter=None):
    """web client -> broker -> indexers, wired over loopback HTTP. Pass a
    ``minter`` to play the Lightning network (``mark_settled``)."""
    peers = PeerStore()
    cfg = BrokerConfig(listen_host="127.0.0.1", listen_port=0,
                       data_dir=tmp_path, receive=receive,
                       client=client or {})
    stats = StatsCache()
    transport = LoopbackTransport(
        {ep: idx.handle for ep, idx in indexers.items()})
    gossip = Gossip(Identity.generate(), peers, transport, cfg, stats=stats)
    for ep, idx in indexers.items():
        announcement = idx.announcement(ep)
        assert peers.add(announcement)
        assert gossip.pull_stats(announcement)
    receiver = build_receiver(cfg)
    if minter is not None and receiver is not None:
        receiver = L402Receiver(b"entry-secret", tmp_path / "entry.db",
                                minter)
    node = BrokerNode(Identity.generate(), peers,
                      IndexerClient(pay_provider, transport),
                      receiver=receiver, stats=stats)
    broker_srv = BrokerHTTPServer(node, cfg)
    broker_srv.start_background()
    web_srv = WebClientServer(WebClientConfig(
        listen_host="127.0.0.1", listen_port=0,
        broker_url=f"http://127.0.0.1:{broker_srv.port}",
        site_secret=str(receive.get("site_secret") or "")))
    web_srv.start_background()
    return f"http://127.0.0.1:{web_srv.port}", web_srv, broker_srv


def _one_indexer(tmp_path, price=1000):
    return {"http://indexer.test": FakeIndexer(
        Identity.generate(), price_msat=price, db_path=tmp_path / "idx.db",
        results=[{"id": "d1", "url": "https://example.org/a",
                  "title": "Alpha", "snippet": "s"},
                 {"id": "d2", "url": "https://example.org/b",
                  "title": "Beta", "snippet": "s"}],
        terms={"jamming": (200, 1), "mitigation": (120, 1)})}


@pytest.fixture
def paid_rig(tmp_path):
    """Quoted pricing: indexer charges 1000, broker adds 500 flat margin;
    all visibility flags on, as the demo deployment runs. Yields the page's
    base URL and the minter that plays the Lightning network."""
    minter = MockInvoiceMinter()
    base, web_srv, broker_srv = _stack(
        tmp_path, indexers=_one_indexer(tmp_path),
        receive={"pricing_mode": "quoted", "margin_flat_msat": 500,
                 "minter": "mock", "site_secret": "shared-with-the-site"},
        client={"expose_outcome": True, "expose_raw_sets": True},
        pay_provider=MockPayProvider(), minter=minter)
    yield base, minter
    web_srv.shutdown()
    broker_srv.shutdown()


@pytest.fixture
def paid_stack(paid_rig):
    return paid_rig[0]


@pytest.fixture
def free_stack(tmp_path):
    base, web_srv, broker_srv = _stack(
        tmp_path, indexers=_one_indexer(tmp_path, price=0), receive={},
        pay_provider=NullPayProvider())
    yield base
    web_srv.shutdown()
    broker_srv.shutdown()


def test_free_mode_skips_straight_to_results(free_stack):
    r = httpx.post(free_stack + "/api/search",
                   json={"q": "jamming", "n": 10, "quote_msat": 0},
                   timeout=10)
    data = r.json()
    assert "payment_required" not in data
    assert [x["url"] for x in data["results"]] == [
        "https://example.org/a", "https://example.org/b"]
    assert data["results"][0]["sources"][0]["rank"] == 1


def test_quote_is_query_dependent_with_stats_age(paid_stack):
    covered = httpx.get(paid_stack + "/api/quote",
                        params={"q": "jamming mitigation", "n": 10},
                        timeout=10).json()
    assert covered["quote_msat"] == 1500      # 1000 downstream + 500 margin
    assert covered["indexers_considered"] == 1
    assert isinstance(covered["stats_age_s"], int)


def test_peers_view_for_the_cards(free_stack):
    data = httpx.get(free_stack + "/api/peers", timeout=10).json()
    [x] = data["indexers"]
    assert len(x["pubkey"]) == 64
    assert x["endpoint"] == "http://indexer.test"
    assert x["transport"] == "clearnet"
    assert x["corpus"]["document_count"] == 1000
    # Stripped card: no chips/profile/learned in the payload; the
    # vocabulary (a detail tab) and reachability remain.
    assert "chips" not in x and "profile" not in x and "learned" not in x
    assert x["vocabulary"][0]["term"] == "jamming"
    assert x["vocabulary"][0]["pct"] == 20.0
    assert "reach" in x   # present (None when no reachability is wired)


def test_l402_flow_end_to_end_with_receipt(paid_rig):
    paid_stack, minter = paid_rig
    # Step 2: search -> 402 surfaced with invoice, QR, and the held bytes.
    r = httpx.post(paid_stack + "/api/search",
                   json={"q": "jamming mitigation", "n": 10,
                         "quote_msat": 1500},
                   timeout=10)
    challenge = r.json()
    assert challenge["payment_required"]
    assert challenge["amount_msat"] == 1500
    assert challenge["quote_msat"] == 1500
    assert challenge["qr"].startswith("data:image/svg+xml")
    assert len(challenge["payment_hash"]) == 64
    assert challenge["ask"]["n"] == 10          # the envelope, echoed
    request_bytes = base64.b64decode(challenge["request_b64"])
    assert json.loads(request_bytes)["query"] == "jamming mitigation"

    # Before payment the page sees no settlement, and asking to collect
    # does NOT replay: the invoice being paid must never be replaced.
    status = httpx.get(paid_stack + "/api/pending-status",
                       params={"hash": challenge["payment_hash"]},
                       timeout=10).json()
    assert status == {"settled": False, "ready": False}
    r = httpx.post(paid_stack + "/api/complete", timeout=10, json={
        "request_b64": challenge["request_b64"],
        "macaroon": challenge["macaroon"],
        "preimage": "", "quote_msat": 1500})
    assert r.json()["unsettled"] is True

    # A rebuilt request cannot ride the macaroon: the binding refuses it.
    tampered = json.loads(request_bytes)
    tampered["query"] = "something else"
    r = httpx.post(paid_stack + "/api/complete", timeout=10, json={
        "request_b64": base64.b64encode(
            json.dumps(tampered).encode()).decode(),
        "macaroon": challenge["macaroon"],
        "preimage": "", "quote_msat": 1500})
    assert "not issued for the pending request" in r.json()["error"]

    # Step 3: the wallet pays; the broker's own node sees it settle. The
    # preimage exists in the wallet, but nobody on the page needs it.
    minter.mark_settled(challenge["payment_hash"])
    status = httpx.get(paid_stack + "/api/pending-status",
                       params={"hash": challenge["payment_hash"]},
                       timeout=10).json()
    assert status["settled"] is True

    # Step 4: the original bytes replay byte-identically, the preimage slot
    # empty, and redeem on the node's word — with the receipt: attributed
    # results plus the outcome record.
    r = httpx.post(paid_stack + "/api/complete", timeout=10, json={
        "request_b64": challenge["request_b64"],
        "macaroon": challenge["macaroon"],
        "preimage": "", "quote_msat": 1500})
    data = r.json()
    assert [x["url"] for x in data["results"]] == [
        "https://example.org/a", "https://example.org/b"]
    outcome = data["outcome"]
    assert outcome["charged_msat"] == 1500
    assert outcome["cost_msat"] == 1000        # what went downstream
    [node] = outcome["indexers"]
    assert node["stage"] == "responded"
    assert node["paid_msat"] == 1000
    assert node["result_count"] == 2
    assert 0 < node["coverage"] <= 1
    assert len(outcome["raw"][node["pubkey"]]) == 2

    # The credential was consumed by service: completing again yields a
    # FRESH challenge.
    r = httpx.post(paid_stack + "/api/complete", timeout=10, json={
        "request_b64": challenge["request_b64"],
        "macaroon": challenge["macaroon"],
        "preimage": "", "quote_msat": 1500})
    again = r.json()
    assert again["payment_required"]
    assert again["invoice"] != challenge["invoice"]
    assert "did not redeem" in again["note"]


def test_pasted_preimage_is_the_fallback_when_the_node_saw_nothing(
        paid_stack):
    """A broker whose node never reports settlement (here: nobody marks
    the mock settled) still serves a person who pastes the preimage by
    hand — verified locally first, so a wrong one never replays."""
    r = httpx.post(paid_stack + "/api/search",
                   json={"q": "jamming mitigation", "n": 10,
                         "quote_msat": 1500}, timeout=10)
    challenge = r.json()
    preimage = MockPayProvider().pay(challenge["invoice"], 2000)
    assert hashlib.sha256(bytes.fromhex(preimage)).hexdigest() \
        == challenge["payment_hash"]

    r = httpx.post(paid_stack + "/api/complete", timeout=10, json={
        "request_b64": challenge["request_b64"],
        "macaroon": challenge["macaroon"],
        "preimage": "ab" * 32, "quote_msat": 1500})
    assert "payment hash" in r.json()["error"]
    r = httpx.post(paid_stack + "/api/complete", timeout=10, json={
        "request_b64": challenge["request_b64"],
        "macaroon": challenge["macaroon"],
        "preimage": "not hex", "quote_msat": 1500})
    assert "64 hex" in r.json()["error"]

    r = httpx.post(paid_stack + "/api/complete", timeout=10, json={
        "request_b64": challenge["request_b64"],
        "macaroon": challenge["macaroon"],
        "preimage": preimage, "quote_msat": 1500})
    assert [x["url"] for x in r.json()["results"]] == [
        "https://example.org/a", "https://example.org/b"]


def test_site_without_a_secret_says_so_instead_of_replaying(tmp_path):
    """No shared secret on either side: collecting on the broker's word is
    refused locally — the invoice is not replaced — and the paste works."""
    minter = MockInvoiceMinter()
    base, web_srv, broker_srv = _stack(
        tmp_path, indexers=_one_indexer(tmp_path),
        receive={"pricing_mode": "quoted", "margin_flat_msat": 500,
                 "minter": "mock"},
        pay_provider=MockPayProvider(), minter=minter)
    try:
        challenge = httpx.post(base + "/api/search", timeout=10, json={
            "q": "jamming", "n": 10, "quote_msat": 1500}).json()
        minter.mark_settled(challenge["payment_hash"])
        r = httpx.post(base + "/api/complete", timeout=10, json={
            "request_b64": challenge["request_b64"],
            "macaroon": challenge["macaroon"],
            "preimage": "", "quote_msat": 1500})
        assert "site_secret" in r.json()["error"]
        preimage = MockPayProvider().pay(challenge["invoice"], 2000)
        r = httpx.post(base + "/api/complete", timeout=10, json={
            "request_b64": challenge["request_b64"],
            "macaroon": challenge["macaroon"],
            "preimage": preimage, "quote_msat": 1500})
        assert len(r.json()["results"]) == 2
    finally:
        web_srv.shutdown()
        broker_srv.shutdown()


def test_spend_cap_maps_to_unsatisfiable(paid_stack):
    q = httpx.get(paid_stack + "/api/quote",
                  params={"q": "jamming", "n": 10, "max_msat": 200},
                  timeout=10).json()
    assert q["error"] == "unsatisfiable"
    r = httpx.post(paid_stack + "/api/search", timeout=10,
                   json={"q": "jamming", "n": 10, "max_msat": 200,
                         "quote_msat": 0})
    assert r.json().get("price_exceeded") is True


def test_onion_exclusion_reprices_and_reroutes(tmp_path):
    """The Tor toggle: excluding .onion indexers must change the quote, the
    invoice, and who actually answers — through the full paid flow."""
    indexers = {
        "http://clear.test": FakeIndexer(
            Identity.generate(), price_msat=1000,
            db_path=tmp_path / "clear.db",
            results=[{"id": "c", "url": "https://clear.example/a",
                      "title": "Clear", "snippet": "s"}],
            terms={"jamming": (200, 1)}),
        "http://exampleonion234567.onion": FakeIndexer(
            Identity.generate(), price_msat=1000,
            db_path=tmp_path / "onion.db",
            results=[{"id": "o", "url": "https://onion.example/b",
                      "title": "Onion", "snippet": "s"}],
            terms={"jamming": (200, 1)}),
    }
    minter = MockInvoiceMinter()
    base, web_srv, broker_srv = _stack(
        tmp_path, indexers=indexers,
        receive={"pricing_mode": "quoted", "margin_flat_msat": 500,
                 "minter": "mock", "site_secret": "shared-with-the-site"},
        client={"expose_outcome": True},
        pay_provider=MockPayProvider(), minter=minter)
    try:
        both = httpx.get(base + "/api/quote",
                         params={"q": "jamming", "n": 10},
                         timeout=10).json()
        assert both["indexers_considered"] == 2
        assert both["quote_msat"] == 2500      # 1000 + 1000 + 500 margin
        excl = httpx.get(base + "/api/quote",
                         params={"q": "jamming", "n": 10,
                                 "exclude_tor": "1"},
                         timeout=10).json()
        assert excl["indexers_considered"] == 1
        assert excl["quote_msat"] == 1500      # onion indexer priced out

        # The peers view names transports so the page can filter the cards.
        peers = httpx.get(base + "/api/peers", timeout=10).json()
        assert sorted(x["transport"] for x in peers["indexers"]) == \
            ["clearnet", "tor"]

        r = httpx.post(base + "/api/search", timeout=10,
                       json={"q": "jamming", "n": 10, "exclude_onion": True,
                             "quote_msat": 1500})
        challenge = r.json()
        assert challenge["amount_msat"] == 1500
        assert challenge["ask"]["exclude_tor"] is True
        minter.mark_settled(challenge["payment_hash"])
        r = httpx.post(base + "/api/complete", timeout=10, json={
            "request_b64": challenge["request_b64"],
            "macaroon": challenge["macaroon"],
            "preimage": "", "quote_msat": 1500})
        data = r.json()
        assert [x["url"] for x in data["results"]] == \
            ["https://clear.example/a"]        # the onion never asked
        stages = {x["transport"]: x["stage"]
                  for x in data["outcome"]["indexers"]}
        assert stages == {"clearnet": "responded", "tor": "considered"}
    finally:
        web_srv.shutdown()
        broker_srv.shutdown()


def test_page_served_with_qr_capable_csp(free_stack):
    r = httpx.get(free_stack + "/", timeout=10)
    csp = r.headers["Content-Security-Policy"]
    assert "default-src 'none'" in csp
    assert "img-src data:" in csp
    assert "font-src data:" in csp           # the inlined Plex faces
    assert "localStorage" in r.text          # the pending-request persistence
    assert "<title>Search</title>" in r.text
    # The visibility front end's fixtures.
    for marker in ('id="cards"', 'id="askdid"', 'id="drill"',
                   "/api/peers", "/api/pending-status", "applyOutcome",
                   'id="map"', ">Route map</button>", "drawMap",
                   'id="backup"', "Paid, but nothing happened?",
                   'finish("")',
                   "Exclude Tor indexers</label>",
                   # one tool row: Tor visible, Options and History disclose
                   'id="opt-btn"', 'id="hist-btn"', 'aria-controls="opt-panel"',
                   "kept only in this browser",
                   "The broker keeps no history of your searches",
                   # navigation survives: id-only URLs, deletable history
                   "history.pushState", '"popstate"', '"pagehide"',
                   'id="newsearch"', 'id="histclear"', "deleteReceipt",
                   "sessionStorage",
                   # the stripped card: name, full pubkey, endpoint+reach,
                   # description, evidence basis, two-tab detail pane
                   "cardname", "pkfull", "buildDetailPane", "buildVocabTable",
                   "Described from ",
                   # Phase 3 components: labeled stats, "from [name]",
                   # empty/error blocks with figures, no vocabulary bars
                   '"stats"', '"srcname"',
                   "showBlock", 'figure: "noresults"', 'figure: "nonetwork"',
                   'figure: "nobroker"', 'figure: "refused"',
                   "No indexers are reachable right now",
                   'id="netblock"',
                   # Phase 4 selection: card checkbox, chips, scope line,
                   # never-widen notices, scope carried through payment
                   " Search this", 'id="chips"', 'id="scopeline"',
                   "chipx", '"Remove "', '"Searching "',
                   "All selected indexers use Tor",
                   "Search the full network", "sent.pubkeys",
                   "scopeNoteFromQuote", "Some are slow to answer"):
        assert marker in r.text, marker
    assert "loadNewestPending" not in r.text  # reloads stay on the homepage
    assert '"?q=' not in r.text                # queries never enter the URL
    # the fluff the stripped card dropped
    for gone in ('"reads as: "', '"learned: "', 'class="chips"',
                 "Broker's take", '"vbar"', "pkColor",
                 "something went wrong", '" terms"'):
        assert gone not in r.text, gone


def test_hostile_results_still_sanitized_through_paid_flow(tmp_path):
    minter = MockInvoiceMinter()
    base, web_srv, broker_srv = _stack(
        tmp_path, indexers=_one_indexer(tmp_path, price=0),
        receive={"pricing_mode": "quoted", "margin_flat_msat": 700,
                 "minter": "mock", "site_secret": "shared-with-the-site"},
        pay_provider=NullPayProvider(), minter=minter)
    try:
        r = httpx.post(base + "/api/search", timeout=10,
                       json={"q": "jamming", "n": 10, "quote_msat": 700})
        challenge = r.json()
        assert challenge["amount_msat"] == 700   # margin alone; indexer free
        minter.mark_settled(challenge["payment_hash"])
        r = httpx.post(base + "/api/complete", timeout=10, json={
            "request_b64": challenge["request_b64"],
            "macaroon": challenge["macaroon"],
            "preimage": "", "quote_msat": 700})
        data = r.json()
        assert "results" in data
        assert "outcome" not in data       # flags off: receipt withheld
    finally:
        web_srv.shutdown()
        broker_srv.shutdown()


def test_page_assembled_from_real_files():
    """Option A: one served page built at startup from broker/web/ — Plex
    inlined as data: woff2, our own figures ahead of the page script, the
    tokens on :root, and nothing drawn by anyone else."""
    import re

    from broker.webclient import build_page

    page = build_page("A & B").decode()
    assert "<title>A &amp; B</title>" in page
    assert len(re.findall(
        r"@font-face\{font-family:'IBM Plex (Sans|Mono)'.*?"
        r"src:url\(data:font/woff2;base64,", page)) == 4
    # Our figures, and none of anyone else's.
    assert "const Figures = " in page
    assert page.index("const Figures = ") < page.index('"use strict";\nconst $')
    for name in ("fanout", "noresults", "nonetwork", "nobroker", "refused"):
        assert f"FIG.{name} = " in page, name
    assert "@@" not in page                    # every slot filled
    # Tokens and the three-guard theme switch.
    for token in ("--plate: #ffffff", "--plate: #141417",
                  ':root:not([data-theme="light"])',
                  ':root[data-theme="dark"]', "--stroke: 0.9px"):
        assert token in page, token
    # Indexers are told apart by name, never colour.
    assert "pkColor" not in page and "border-left: 3px" not in page
