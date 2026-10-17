"""Transport failures are routing facts, never crashes.

The routing contract — one indexer's failure never fails the route — died
in production on a raw httpx.ConnectError from an unreachable .onion
endpoint: the client's promise to raise IndexerError only covered protocol
refusals, so the exception escaped route_query and a PAID query became a
500. These tests pin the repaired contract at every layer: the client wraps
whatever the transport throws, the route survives a dead indexer, and onion
names are never resolved or dialed without a Tor proxy.
"""

import pytest

from broker.client import (HttpxTransport, IndexerClient, IndexerError,
                           Transport)
from broker.identity import Identity
from broker.payment import MockPayProvider
from broker.payment.pay import NullPayProvider
from helpers import FakeIndexer, broker_with


class _Exploding(Transport):
    def post(self, url, body, headers):
        raise ConnectionError("[Errno -2] Name or service not known")


def test_client_wraps_raw_transport_errors_as_indexer_error():
    client = IndexerClient(NullPayProvider(), _Exploding())
    with pytest.raises(IndexerError, match="unreachable"):
        client.query("http://gone.test", {"query": "q"}, "per_query", 0)


class _Unreachable:
    """Announces like a real indexer; dialing it fails below HTTP."""

    def __init__(self, inner):
        self._inner = inner

    def announcement(self, endpoint):
        return self._inner.announcement(endpoint)

    def handle(self, path, body, headers):
        raise OSError("[Errno -2] Name or service not known")


def test_unreachable_indexer_never_fails_the_route(tmp_path):
    live = FakeIndexer(Identity.generate(), price_msat=750,
                       db_path=tmp_path / "live.db",
                       results=[{"id": "a", "url": "https://a.test/1",
                                 "title": "A", "snippet": "s"}])
    dead = _Unreachable(FakeIndexer(Identity.generate(), price_msat=0,
                                    db_path=tmp_path / "dead.db", results=[]))
    broker = broker_with({"http://live.test": live, "http://dead.test": dead},
                         MockPayProvider(), fanout=99,
                         rank=lambda query, indexers, stats: list(indexers))
    outcome = broker.route_query("q", max_results=10,
                                 pricing_type="per_query", max_price=5000)
    assert [r["url"] for r in outcome.results] == ["https://a.test/1"]
    assert outcome.paid_msat == 750          # the dead node cost nothing
    [error] = outcome.errors.values()        # ...and was logged, not fatal
    assert "unreachable" in error


def test_onion_endpoint_refused_without_tor_never_dialed():
    with pytest.raises(IndexerError, match="no tor socks proxy"):
        HttpxTransport().post("http://exampleonion234567.onion/query",
                              b"{}", {})


def test_onion_endpoint_goes_to_the_socks_proxy_not_dns():
    import httpx

    # A proxy that refuses instantly: reaching it (instead of resolving the
    # onion name) is exactly the behavior under test.
    transport = HttpxTransport(onion_proxy="socks5://127.0.0.1:1",
                               onion_timeout=2)
    with pytest.raises(httpx.TransportError):
        transport.post("http://exampleonion234567.onion/query", b"{}", {})


class _Slow:
    """Wraps an indexer with a fixed pre-response delay."""

    def __init__(self, inner, delay):
        self._inner = inner
        self._delay = delay

    def announcement(self, endpoint):
        return self._inner.announcement(endpoint)

    def handle(self, path, body, headers):
        import time
        time.sleep(self._delay)
        return self._inner.handle(path, body, headers)


def test_fan_out_is_concurrent_wall_time_is_the_max_not_the_sum(tmp_path):
    import time

    indexers = {}
    for name in ("one", "two", "three"):
        inner = FakeIndexer(Identity.generate(), price_msat=0,
                            db_path=tmp_path / f"{name}.db",
                            results=[{"id": name,
                                      "url": f"https://{name}.test/",
                                      "title": name, "snippet": "s"}])
        indexers[f"http://{name}.test"] = _Slow(inner, 0.4)
    broker = broker_with(indexers, NullPayProvider(), fanout=99,
                         rank=lambda query, idx, stats: list(idx))
    started = time.monotonic()
    outcome = broker.route_query("q", max_results=10,
                                 pricing_type="per_query", max_price=0)
    elapsed = time.monotonic() - started
    assert len(outcome.results) == 3
    # Sequential would be >= 1.2s; concurrent is one delay plus overhead.
    assert elapsed < 0.9, f"fan-out looks sequential: {elapsed:.2f}s"
