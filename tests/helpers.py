"""Shared test fixtures: an in-process paid indexer and broker wiring."""

import json
import time

from broker.client import IndexerClient, LoopbackTransport
from broker.identity import Identity
from broker.node import BrokerNode
from broker.payment import L402Receiver, MockInvoiceMinter
from broker.peers import PeerStore
from broker.wire import accept


class FakeIndexer:
    """A minimal paid indexer: L402 at the door, a signed result set inside."""

    def __init__(self, identity, price_msat, db_path, results,
                 document_count=1000, terms=None):
        self.identity = identity
        self.price = price_msat
        self.results = results
        self.document_count = document_count
        # {term: (document_count, newest)} served from /corpus
        self.terms = terms or {}
        self.receiver = L402Receiver(
            secret=b"indexer-secret", db_path=db_path,
            minter=MockInvoiceMinter())

    def announcement(self, endpoint):
        return self.identity.signed({
            "pubkey": self.identity.pubkey,
            "protocol_version": 0,
            "endpoint": endpoint,
            "timestamp": int(time.time()),
            "roles": ["indexer"],
            "pricing": [{"type": "per_query", "price": self.price}],
            "corpus": {
                "document_count": self.document_count, "term_count": 5000,
                "updated": int(time.time()), "languages": ["en"],
                "statistics_versions": [0],
            },
        }, "announcement")

    def handle(self, path, body, headers):
        if path == "/corpus":
            message, _ = accept(body, "corpus_statistics_request")
            response = self.identity.signed({
                "pubkey": self.identity.pubkey,
                "request_id": message["request_id"],
                "statistics_version": 0,
                "updated": int(time.time()),
                "terms": [{"term": t, "document_count": dc, "newest": newest}
                          for t, (dc, newest) in self.terms.items()],
            }, "corpus_statistics_response")
            return 200, {"Content-Type": "application/json"}, \
                json.dumps(response).encode()
        assert path == "/query"
        message, _ = accept(body, "query_request")  # validates the query
        # A price-0 indexer runs in free mode: it serves without a challenge,
        # which is conformant. A priced one gates on L402, with the signed
        # payment challenge body the Payment profile requires of a 402.
        auth = headers.get("Authorization", "")
        if self.price > 0 and not self.receiver.authorize(auth, "per_query"):
            ch = self.receiver.challenge(self.price, "per_query", 1)
            challenge_body = self.identity.signed({
                "pubkey": self.identity.pubkey,
                "request_id": message["request_id"],
                "invoice": ch["invoice"],
                "macaroon": ch["macaroon"],
                "expiry": ch["expiry"],
            }, "payment_challenge")
            return 402, {"WWW-Authenticate": ch["header"]}, \
                json.dumps(challenge_body).encode()
        result_set = self.identity.signed({
            "pubkey": self.identity.pubkey,
            "request_id": message["request_id"],
            "results": self.results,
        }, "result_set")
        return 200, {"Content-Type": "application/json"}, \
            json.dumps(result_set).encode()


def broker_with(indexers: dict, pay_provider, **node_kwargs) -> BrokerNode:
    """A broker wired to in-process indexers, keyed by endpoint base URL."""
    peers = PeerStore()
    handlers = {}
    for endpoint, indexer in indexers.items():
        assert peers.add(indexer.announcement(endpoint))
        handlers[endpoint] = indexer.handle
    client = IndexerClient(pay_provider, LoopbackTransport(handlers))
    return BrokerNode(Identity.generate(), peers, client, **node_kwargs)
