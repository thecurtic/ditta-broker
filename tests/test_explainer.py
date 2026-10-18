"""The /protocol explainer (explainer-page.md): its sections, its figures'
text alternatives, its links, and the read-only live data behind it."""

import json
import re

import httpx

from broker.webclient import build_page, build_protocol_page
from test_scope import _rig, _indexer  # noqa: F401  (rig helpers)
from test_webclient import _stack, free_stack  # noqa: F401  (fixture)

SECTIONS = ("how-ditta-works", "the-problem", "two-roles",
            "how-a-query-flows", "what-holds-it-together",
            "what-it-leaves-alone", "this-brokers-view", "build-against-it")


def test_sections_present_in_order_with_anchors():
    page = build_protocol_page("Search").decode()
    found = re.findall(r'<section id="([a-z-]+)"', page)
    assert tuple(found) == SECTIONS
    assert "<title>How DITTA works</title>" in page
    assert "Version 0 is a draft" in page and "unstable" in page


def test_every_figure_has_a_text_alternative():
    page = build_protocol_page("Search").decode()
    figs = re.findall(r'<div class="figure[^"]*" data-figure="([a-z0-9]+)"'
                      r'[^>]*?aria-label="([^"]+)"', page, re.S)
    assert len(figs) == 8
    assert all(len(label) > 20 for _name, label in figs)
    named = [name for name, _ in figs]
    assert named == ["fanout", "announce", "step1", "step2", "step3",
                     "step4", "step5", "step6"]
    # every figure is ours: drawn by figures.js, nothing vendored
    for name in named:
        assert f"FIG.{name} = " in page, name


def test_money_is_lightning_and_sats():
    """Payments are drawn as a lightning bolt with an amount in sats, and
    invoices as cards with the amount and a QR; no punch cards."""
    from pathlib import Path

    js = (Path(__file__).parents[1] / "broker/web/figures.js").read_text()
    assert "const BOLT = " in js and '" sats"' in js and "function qr(" in js
    assert "punch" not in js


def test_no_normative_words_and_no_reference_claim():
    page = build_protocol_page("Search").decode()
    text = re.sub(r"<script.*?</script>", "", page, flags=re.S)
    text = re.sub(r"<style.*?</style>", "", text, flags=re.S)
    for word in ("MUST", "SHOULD", " MAY ", "revolutionary"):
        assert word not in text, word
    assert "independent implementation" in text
    assert "It is not the reference" in text


def test_links_point_at_the_ditta_protocol_org():
    page = build_protocol_page("Search").decode()
    for url in ("https://github.com/ditta-protocol/ditta-spec",
                "https://github.com/ditta-protocol/ditta-wire-py",
                "https://github.com/ditta-protocol/ditta-spec/tree/main/"
                "vectors"):
        assert f'href="{url}' in page, url


def test_home_links_to_the_explainer():
    page = build_page("Search").decode()
    assert page.count('href="/protocol"') == 2   # by the figure, and the footer


def test_broker_serves_its_announcements_read_only(tmp_path):
    r = _rig(tmp_path, {"http://a.test": (0, False)})
    try:
        data = httpx.get(r["base"] + "/client/announcements",
                         timeout=10).json()
        assert data["broker"]["endpoint"]
        assert len(data["broker"]["pubkey"]) == 64
        [a] = data["announcements"]
        assert a["pubkey"] == r["pk"]["http://a.test"]
        assert len(a["sig"]) == 128          # exactly as signed
    finally:
        r["server"].shutdown()


def test_page_proxy_serves_protocol_with_verified_announcements(free_stack):  # noqa: F811
    r = httpx.get(free_stack + "/protocol", timeout=10)
    assert r.status_code == 200
    assert "How DITTA works" in r.text
    assert "font-src data:" in r.headers["Content-Security-Policy"]
    data = httpx.get(free_stack + "/api/announcements", timeout=10).json()
    [a] = data["announcements"]
    from broker.wire import validate
    validate(a, "announcement")              # still verifies, unchanged


def test_proxy_passes_announcements_through_as_data(tmp_path):
    """Verifying announcements is the broker's job (PeerStore refuses a
    bad one on arrival); the web client speaks no DITTA and only shows
    what /client/announcements returns, dropping what is not even shaped
    like one and any endpoint a browser should not follow."""
    from broker.webclient import WebClient, WebClientConfig

    good = _indexer(tmp_path, "g").announcement("https://g.test")
    api = WebClient(WebClientConfig())
    api._get_broker = lambda path: (200, {}, json.dumps({
        "broker": {"pubkey": "ab" * 32, "endpoint": "javascript:alert(1)"},
        "announcements": [good, "junk", {"pubkey": "nope"}]}).encode())
    out = api.announcements()
    assert out["announcements"] == [good]
    assert out["broker"] == {"pubkey": "ab" * 32, "endpoint": None}


def test_copy_matches_the_spec_on_strictness_and_state():
    """Strict: every message rejects a wrong type or bad signature; only
    operational ones reject unknown fields, announcements tolerate them at
    top level only (Extensibility). State: a spent mark, or under a batch
    the request ids served, kept until expiry (Payment profile, State)."""
    page = build_protocol_page("Search").decode()
    text = re.sub(r"\s+", " ", page)
    assert "Every message, the announcement included, is rejected" in text
    assert "because some of them move money" in text
    assert "unknown fields at their top level only" in text
    assert "These are the messages that move money" not in text
    assert "under a batch credential, the request identifiers" in text
    assert "dropped when the credential expires" in text


def test_client_api_phrase_is_the_link():
    page = build_protocol_page("Search").decode()
    assert '<span id="agent-api-link">this broker\'s client API</span>' in page
    assert 'span.replaceWith(a)' in page
