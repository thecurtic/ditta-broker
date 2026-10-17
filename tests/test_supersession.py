"""Behavioural checks for the peer store's supersession rules."""

import time

from broker.identity import Identity
from broker.peers import PeerStore


def _announce(identity, ts, endpoint="https://a.test", price=0):
    return identity.signed({
        "pubkey": identity.pubkey,
        "protocol_version": 0,
        "endpoint": endpoint,
        "timestamp": ts,
        "roles": ["indexer"],
        "pricing": [{"type": "per_query", "price": price}],
        "corpus": {"document_count": 1, "term_count": 1, "updated": ts,
                   "languages": ["en"], "statistics_versions": [0]},
    }, "announcement")


def test_greater_timestamp_supersedes():
    ident = Identity.generate()
    store = PeerStore()
    now = int(time.time())
    assert store.add(_announce(ident, now, "https://old.test"))
    assert store.add(_announce(ident, now + 1, "https://new.test"))
    assert len(store) == 1
    assert store.get(ident.pubkey)["endpoint"] == "https://new.test"


def test_older_timestamp_ignored():
    ident = Identity.generate()
    store = PeerStore()
    now = int(time.time())
    assert store.add(_announce(ident, now + 10, "https://new.test"))
    assert not store.add(_announce(ident, now, "https://old.test"))
    assert store.get(ident.pubkey)["endpoint"] == "https://new.test"


def test_future_dated_ignored():
    ident = Identity.generate()
    store = PeerStore(future_skew=3600)
    now = int(time.time())
    assert not store.add(_announce(ident, now + 7200), now=now)
    assert len(store) == 0


def test_invalid_signature_rejected():
    ident = Identity.generate()
    store = PeerStore()
    ann = _announce(ident, int(time.time()))
    ann["endpoint"] = "https://tampered.test"  # invalidates the signature
    assert not store.add(ann)
    assert len(store) == 0
