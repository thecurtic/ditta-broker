"""Indexer naming: the two-call pipeline (describe+name, then assess) and
its deterministic rules.

A scripted fake inference endpoint keyed by the STAGE tag in the system
prompt (1 = describe+name -> {name, description}, 2 = assess ->
{assessment}) exercises the validators that are the real defence, the
deterministic keep-vs-rename reconcile, the retired-name rules, the
experience gate, and the recovery paths (model change, operator reset).
Nothing here needs a real model.
"""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from broker.identity import Identity
from broker.naming import (InferenceClient, NamingService, brand_tokens,
                           has_banned_word, leading_brand_violation,
                           name_shape_ok, normalize_candidate, numbers_check,
                           registrable_label)
from broker.peers import PeerStore
from broker.stats import StatsCache


class FakeLLM:
    """Replies by the STAGE:n tag in the system prompt, from a script."""

    def __init__(self):
        self.script = {}
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(
                    int(self.headers.get("Content-Length", 0))))
                system = body["messages"][0]["content"]
                stage = next((n for n in "12" if f"STAGE:{n}" in system), "1")
                reply = outer.script.get(stage, "{}")
                out = json.dumps({"choices": [
                    {"message": {"content": reply}}]}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)

        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self._httpd.serve_forever,
                         daemon=True).start()
        self.url = f"http://127.0.0.1:{self._httpd.server_address[1]}/v1"

    def shutdown(self):
        self._httpd.shutdown()


@pytest.fixture
def llm():
    f = FakeLLM()
    yield f
    f.shutdown()


class _FakeLearner:
    def __init__(self, profile=None):
        self._p = profile

    def profile(self, pubkey):
        return self._p


def _announcement(_id, docs, terms_count, langs):
    ident = Identity.generate()
    return ident, ident.signed({
        "pubkey": ident.pubkey, "protocol_version": 0,
        "endpoint": "https://idx.example", "timestamp": 1,
        "roles": ["indexer"],
        "pricing": [{"type": "per_query", "price": 0}],
        "corpus": {"document_count": docs, "term_count": terms_count,
                   "updated": 1, "languages": langs,
                   "statistics_versions": [0]},
    }, "announcement")


def _service(tmp_path, llm, *, learner=None, store=None, model="test-model",
             config=None):
    peers = PeerStore()
    stats = StatsCache()
    svc = NamingService(
        tmp_path / "naming.db", peers, stats, node=None,
        client=InferenceClient(llm.url, model),
        config={"min_queries_for_assessment": 10, **(config or {})},
        learner=learner, store=store)
    return svc, peers, stats


# -- deterministic validators ----------------------------------------------

def test_numbers_check_allows_list_counts_and_magnitude():
    facts = {"languages": ["en"] * 15, "document_count": 105_000_000}
    assert numbers_check("105 million documents in 15 languages", facts)
    assert not numbers_check("in 9 languages", facts)


def test_name_shape_rules():
    assert name_shape_ok("The Polyglot Crawler")
    assert name_shape_ok("Web & Media Hub")
    assert name_shape_ok("Café Crawler")
    assert not name_shape_ok("Trusted Security Index")
    assert not name_shape_ok("Index")
    assert not name_shape_ok("node a1b2c3d4e5")
    assert not name_shape_ok("A B C D E F")
    assert has_banned_word("the Verified crawler")


def test_normalize_candidate_peels_noise():
    assert normalize_candidate('**"The Quiet Archive".**') == \
        "The Quiet Archive"
    assert normalize_candidate("Name: Zap Chronicle") == "Zap Chronicle"


# -- the pipeline -----------------------------------------------------------

def test_describe_and_name_happy_path(tmp_path, llm):
    learner = _FakeLearner({"queries": 40, "latency_p50_ms": 300,
                            "success_rate": 0.9})
    svc, peers, stats = _service(tmp_path, llm, learner=learner)
    ident, ann = _announcement("x", 3400, 14000, ["en"])
    peers.add(ann)
    stats.put(ident.pubkey, {"attacker": (900, 1), "cwe": (800, 1)},
              updated=1)
    llm.script = {
        "1": '{"name": "The Exploit Librarian", "description": "Covers '
             'cybersecurity disclosures and exploit write-ups."}',
        "2": '{"assessment": "Our go-to for security questions."}',
    }
    assert svc.run_once(ann)
    view = svc.view(ident.pubkey)
    assert view["name"] == "The Exploit Librarian"
    assert view["description"].startswith("Covers")
    assert view["assessment"] == "Our go-to for security questions."
    assert view["status"] == "ok"
    assert [h["name"] for h in view["history"]] == ["The Exploit Librarian"]


def test_assessment_gated_until_experience(tmp_path, llm):
    svc, peers, stats = _service(tmp_path, llm,
                                 learner=_FakeLearner({"queries": 3}))
    ident, ann = _announcement("y", 500, 900, ["en"])
    peers.add(ann)
    llm.script = {
        "1": '{"name": "Tiny Niche Reader", "description": "Covers x."}',
        "2": '{"assessment": "SHOULD NOT BE CALLED"}',
    }
    svc.run_once(ann)
    v = svc.view(ident.pubkey)
    assert v["name"] == "Tiny Niche Reader"
    assert v["assessment"] is None       # no experience yet


def test_banned_name_held_with_description_kept(tmp_path, llm):
    svc, peers, stats = _service(tmp_path, llm)
    ident, ann = _announcement("z", 500, 900, ["en"])
    peers.add(ann)
    llm.script = {"1": '{"name": "Verified Secure Node", '
                       '"description": "Covers things."}'}
    svc.run_once(ann)
    v = svc.view(ident.pubkey)
    assert v["name"] is None             # banned words -> rejected
    assert v["description"] == "Covers things."   # ...description kept
    assert "did not pass the rules" in v["last_error"]


def test_held_when_no_json(tmp_path, llm):
    svc, peers, stats = _service(tmp_path, llm)
    ident, ann = _announcement("h", 500, 900, ["en"])
    peers.add(ann)
    llm.script = {"1": "I cannot help with that request."}
    svc.run_once(ann)
    v = svc.view(ident.pubkey)
    assert v["status"] == "held"
    assert "model said:" in v["last_error"] \
        and "cannot help" in v["last_error"]


def test_held_when_inference_unreachable(tmp_path):
    peers = PeerStore()
    svc = NamingService(tmp_path / "naming.db", peers, StatsCache(),
                        node=None,
                        client=InferenceClient("http://127.0.0.1:1/v1",
                                               "m", timeout=1))
    ident, ann = _announcement("d", 500, 900, ["en"])
    peers.add(ann)
    assert not svc.run_once(ann)
    assert svc.view(ident.pubkey)["status"] == "held"


def test_absent_without_a_client():
    svc = NamingService("/tmp/x-naming-none.db", PeerStore(), StatsCache(),
                        node=None, client=None)
    _i, ann = _announcement("e", 1, 1, ["en"])
    assert not svc.run_once(ann)


def test_retired_name_not_reused_by_another_pubkey(tmp_path, llm):
    svc, peers, stats = _service(tmp_path, llm)
    id_a, ann_a = _announcement("a", 500, 900, ["en"])
    id_b, ann_b = _announcement("b", 600, 900, ["en"])
    peers.add(ann_a)
    peers.add(ann_b)
    llm.script = {"1": '{"name": "The Wandering Index", '
                       '"description": "Covers a."}'}
    svc.run_once(ann_a)
    assert svc.view(id_a.pubkey)["name"] == "The Wandering Index"
    svc.run_once(ann_b)   # offered the same taken name
    assert svc.view(id_b.pubkey)["name"] != "The Wandering Index"


def test_evidence_base_recorded(tmp_path, llm):
    import time as _t

    from broker.outcomes import NodeOutcome, OutcomeStore, RouteOutcome

    store = OutcomeStore(tmp_path / "outcomes.db")
    svc, peers, stats = _service(tmp_path, llm,
                                 learner=_FakeLearner({"queries": 12}),
                                 store=store)
    ident, ann = _announcement("ev", 46000, 90000, ["en", "de"])
    peers.add(ann)
    stats.put(ident.pubkey, {"nostr": (9000, 1)}, updated=1)
    for qid in ("q1", "q2"):
        o = RouteOutcome(query="zap", pricing_type="per_query",
                         request_id=qid, started_ms=int(_t.time() * 1000))
        n = NodeOutcome(ident.pubkey, "http://x", 0, stage="responded",
                        returned=1, survived=1)
        n.ok = True
        o.nodes.append(n)
        o.raw[ident.pubkey] = [{"id": "i", "url": "https://njump.net/a",
                                "title": "t", "snippet": "s"}]
        o.results = [{"url": "https://njump.net/a",
                      "sources": [{"pubkey": ident.pubkey, "rank": 1}]}]
        store.record(o)
    llm.script = {"1": '{"name": "The Zap Gazette", "description": '
                       '"Covers nostr and bitcoin, from njump.net."}',
                  "2": '{"assessment": "A lively feed."}'}
    svc.run_once(ann)
    eb = svc.view(ident.pubkey)["evidence_base"]
    assert eb["reported_documents"] == 46000
    assert eb["observed_results"] == 2
    assert eb["queries_routed"] == 12


def test_stable_corpus_does_not_rerun(tmp_path, llm):
    learner = _FakeLearner({"queries": 40})
    svc, peers, stats = _service(tmp_path, llm, learner=learner)
    ident, ann = _announcement("s", 1_000_000, 50_000, ["en"])
    peers.add(ann)
    stats.put(ident.pubkey, {"bitcoin": (9000, 1)}, updated=1)
    llm.script = {"1": '{"name": "The Steady Ledger", '
                       '"description": "Covers bitcoin."}',
                  "2": '{"assessment": "Solid."}'}
    calls = {"n": 0}
    real = svc.client.complete
    svc.client.complete = lambda *a, **k: (calls.__setitem__(
        "n", calls["n"] + 1), real(*a, **k))[1]
    assert svc.run_once(ann)
    first = calls["n"]
    # live telemetry churns, corpus grows slightly: no re-run
    learner._p = {"queries": 51, "latency_p50_ms": 1234}
    stats.put(ident.pubkey, {"bitcoin": (9050, 2)}, updated=2)
    ann2 = ident.signed({**{k: v for k, v in ann.items() if k != "sig"},
                         "timestamp": 2,
                         "corpus": {**ann["corpus"],
                                    "document_count": 1_000_900}},
                        "announcement")
    assert not svc.run_once(ann2)
    assert calls["n"] == first


def test_model_change_forces_a_fresh_name(tmp_path, llm):
    svc, peers, stats = _service(tmp_path, llm)
    ident, ann = _announcement("m", 500, 900, ["en"])
    peers.add(ann)
    llm.script = {"1": '{"name": "Analyze The Input", '
                       '"description": "Covers x."}'}
    svc.run_once(ann)
    assert svc.view(ident.pubkey)["name"] == "Analyze The Input"
    svc.client = InferenceClient(llm.url, "better-model-2507")
    llm.script["1"] = '{"name": "The Quiet Archive", ' \
                      '"description": "Covers y."}'
    svc.run_once(ann)
    v = svc.view(ident.pubkey)
    assert v["name"] == "The Quiet Archive"
    assert [h["name"] for h in v["history"]] == ["The Quiet Archive",
                                                 "Analyze The Input"]


def test_reset_names_flushes_and_regenerates(tmp_path, llm):
    svc, peers, stats = _service(tmp_path, llm)
    ident, ann = _announcement("rs", 500, 900, ["en"])
    peers.add(ann)
    llm.script = {"1": '{"name": "Deconstruct The Request", '
                       '"description": "Covers x."}'}
    svc.run_once(ann)
    assert svc.view(ident.pubkey)["name"] == "Deconstruct The Request"
    svc2 = NamingService(
        tmp_path / "naming.db", peers, stats, node=None,
        client=InferenceClient(llm.url, "test-model"),
        config={"reset_names": True})
    assert svc2.view(ident.pubkey)["name"] is None
    llm.script["1"] = '{"name": "The Still Archive", ' \
                      '"description": "Covers y."}'
    svc2.run_once(ann)
    assert svc2.view(ident.pubkey)["name"] == "The Still Archive"


def test_extra_body_is_sent(tmp_path):
    import json as _json

    seen = {}

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            body = _json.loads(self.rfile.read(
                int(self.headers.get("Content-Length", 0))))
            seen.update(body)
            out = _json.dumps({"choices": [
                {"message": {"content": "{}"}}]}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{httpd.server_address[1]}/v1"
    client = InferenceClient(url, "m", extra_body={
        "chat_template_kwargs": {"enable_thinking": False}})
    client.complete("s", "u", temperature=0.2, max_tokens=100)
    httpd.shutdown()
    assert seen["chat_template_kwargs"] == {"enable_thinking": False}


def test_leading_brand_rules():
    assert registrable_label("blossom.primal.net") == "primal"
    assert registrable_label("attack.mitre.org") == "mitre"
    b = brand_tokens(["blossom.primal.net", "njump.net", "nostr.band"])
    subj = {"nostr", "bitcoin"}
    assert leading_brand_violation("Primal Nostr Index", b, subj)
    assert not leading_brand_violation("Nostr Notebook", b, subj)
    assert not leading_brand_violation("The Relay Register", b, subj)
    assert not leading_brand_violation("Notes On Primal", b, subj)


def test_name_leading_with_a_result_brand_is_rejected(tmp_path, llm):
    """A domain's brand must not front the name as its owner."""
    import time as _t

    from broker.outcomes import NodeOutcome, OutcomeStore, RouteOutcome

    store = OutcomeStore(tmp_path / "outcomes.db")
    svc, peers, stats = _service(tmp_path, llm, store=store)
    ident, ann = _announcement("pb", 50000, 90000, ["en"])
    peers.add(ann)
    stats.put(ident.pubkey, {"nostr": (9000, 1), "bitcoin": (7000, 1)},
              updated=1)
    o = RouteOutcome(query="zap", pricing_type="per_query",
                     request_id="q1", started_ms=int(_t.time() * 1000))
    n = NodeOutcome(ident.pubkey, "http://x", 0, stage="responded",
                    returned=1, survived=1)
    n.ok = True
    o.nodes.append(n)
    o.raw[ident.pubkey] = [{"id": "i", "url": "https://blossom.primal.net/a",
                            "title": "t", "snippet": "s"}]
    o.results = [{"url": "https://blossom.primal.net/a",
                  "sources": [{"pubkey": ident.pubkey, "rank": 1}]}]
    store.record(o)
    llm.script = {"1": '{"name": "Primal Nostr Index", '
                       '"description": "Covers nostr notes."}'}
    svc.run_once(ann)
    v = svc.view(ident.pubkey)
    assert v["name"] is None                  # brand led the name -> rejected
    assert "brand" in v["last_error"]
    assert v["description"] == "Covers nostr notes."   # description kept
