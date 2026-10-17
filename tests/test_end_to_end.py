"""End-to-end: a broker routes a query to a paid indexer and pays it.

Wires a real broker (routing + economics + pay) to an in-process indexer
(receive/L402) over the loopback transport, exercising the whole loop with no
network and no Lightning node:

    query -> 402 challenge -> mock pay -> byte-identical retry -> authorize
          -> signed result set -> merge

The mock invoice and mock pay provider move no real money; they stand in for
the invoice-only receive credential and the spend credential that live on real
deployments.
"""

import json

from broker.identity import Identity
from broker.messages import new_request_id
from broker.payment import MockPayProvider
from broker.payment.pay import NullPayProvider
from broker.wire import accept
from helpers import FakeIndexer, broker_with


def test_paid_route_pays_and_returns_results(tmp_path):
    indexer = FakeIndexer(
        Identity.generate(), price_msat=2000,
        db_path=tmp_path / "idx.db",
        results=[{"id": "d1", "url": "https://example.org/a",
                  "title": "Alpha", "snippet": "first"},
                 {"id": "d2", "url": "https://example.org/b",
                  "title": "Beta", "snippet": "second"}])
    pay = MockPayProvider()
    broker = broker_with({"http://indexer.test": indexer}, pay)

    outcome = broker.route_query(
        "channel jamming", max_results=5,
        pricing_type="per_query", max_price=5000)

    assert [r["url"] for r in outcome.results] == [
        "https://example.org/a", "https://example.org/b"]
    assert outcome.paid_msat == 2000
    assert pay.total_spent_msat == 2000
    assert not outcome.errors

    # Merge retained origin: every merged entry names who served it and at
    # what rank in that indexer's own list.
    assert [r["rank"] for r in outcome.results] == [1, 2]
    for r in outcome.results:
        assert r["sources"][0]["pubkey"] == indexer.identity.pubkey
    assert outcome.raw[indexer.identity.pubkey][0]["id"] == "d1"


def test_pay_disabled_skips_paid_indexer(tmp_path):
    indexer = FakeIndexer(
        Identity.generate(), price_msat=2000,
        db_path=tmp_path / "idx.db",
        results=[{"id": "d1", "url": "https://example.org/a",
                  "title": "Alpha", "snippet": "first"}])
    broker = broker_with({"http://indexer.test": indexer}, NullPayProvider())

    outcome = broker.route_query(
        "channel jamming", max_results=5,
        pricing_type="per_query", max_price=5000)

    # No spend credential -> the paid indexer is never asked, nothing paid.
    assert outcome.results == []
    assert outcome.asked == []
    assert outcome.paid_msat == 0


def test_free_indexer_served_without_pay_provider(tmp_path):
    indexer = FakeIndexer(
        Identity.generate(), price_msat=0,
        db_path=tmp_path / "idx.db",
        results=[{"id": "d1", "url": "https://example.org/a",
                  "title": "Alpha", "snippet": "first"}])
    broker = broker_with({"http://indexer.test": indexer}, NullPayProvider())

    outcome = broker.route_query(
        "channel jamming", max_results=5,
        pricing_type="per_query", max_price=0)

    # A price-0 indexer needs no payment, so even a pay-disabled broker serves it.
    assert [r["url"] for r in outcome.results] == ["https://example.org/a"]
    assert outcome.paid_msat == 0


class _HeaderOnlyIndexer(FakeIndexer):
    """A pre-DITTA indexer: 402 with the L402 header but no signed body."""

    def handle(self, path, body, headers):
        status, h, resp = super().handle(path, body, headers)
        if status == 402:
            return status, h, b""
        return status, h, resp


class _ImpostorIndexer(FakeIndexer):
    """Signs its payment challenge with a key that is not its announced
    identity — the in-path substitution the Payment profile exists to stop."""

    def handle(self, path, body, headers):
        status, h, resp = super().handle(path, body, headers)
        if status == 402:
            impostor = Identity.generate()
            m = json.loads(resp)
            m.pop("sig")
            m["pubkey"] = impostor.pubkey
            return status, h, json.dumps(
                impostor.signed(m, "payment_challenge")).encode()
        return status, h, resp


def _refused_route(tmp_path, cls, name):
    indexer = cls(Identity.generate(), price_msat=2000,
                  db_path=tmp_path / f"{name}.db",
                  results=[{"id": "d", "url": "https://example.org/x",
                            "title": "T", "snippet": "s"}])
    pay = MockPayProvider()
    broker = broker_with({f"http://{name}.test": indexer}, pay)
    outcome = broker.route_query("q", max_results=5,
                                 pricing_type="per_query", max_price=5000)
    assert outcome.results == []
    assert outcome.paid_msat == 0          # the broker never paid
    assert pay.total_spent_msat == 0       # provider was never asked
    return list(outcome.errors.values())[0]


def test_402_without_signed_challenge_body_is_never_paid(tmp_path):
    error = _refused_route(tmp_path, _HeaderOnlyIndexer, "headeronly")
    assert "refusing to pay" in error


def test_402_signed_by_the_wrong_identity_is_never_paid(tmp_path):
    error = _refused_route(tmp_path, _ImpostorIndexer, "impostor")
    assert "different identity" in error
