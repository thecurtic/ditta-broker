"""A demo network for screenshots and visual review (not a test module).

Starts a broker and the web client over loopback, wired to four in-process
indexers with names, descriptions, vocabulary and mixed health (one Tor,
one unreachable), with the outcome record exposed so the route map draws.
``python tests/demo_stack.py [--paid]`` prints the page URL and serves
until interrupted; ``start(paid=...)`` does the same for a script.
"""

import sys
import time
from pathlib import Path
from tempfile import mkdtemp

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from broker.client import IndexerClient, LoopbackTransport  # noqa: E402
from broker.config import BrokerConfig, build_receiver  # noqa: E402
from broker.gossip import Gossip  # noqa: E402
from broker.identity import Identity  # noqa: E402
from broker.node import BrokerNode  # noqa: E402
from broker.payment import MockPayProvider  # noqa: E402
from broker.payment.pay import NullPayProvider  # noqa: E402
from broker.peers import PeerStore  # noqa: E402
from broker.reachability import Reachability  # noqa: E402
from broker.server import BrokerHTTPServer  # noqa: E402
from broker.stats import StatsCache  # noqa: E402
from broker.webclient import WebClientConfig, WebClientServer  # noqa: E402
from helpers import FakeIndexer  # noqa: E402

NOW = int(time.time())

INDEXERS = [
    # endpoint, name, covers, docs, price, terms, health:
    # True | False (unreachable) | "slow" (answers in 3.5s) | "flaky"
    # (reachable, but every query fails)
    ("https://lightning-dev.example.net", "Lightning Notes",
     "Covers Lightning Network specifications, BOLT proposals and "
     "implementation mailing lists.", 18_400, 1000,
     {"lightning": (9000, NOW - 3600), "channel": (7000, NOW - 7200),
      "htlc": (2400, NOW - 3600), "jamming": (310, NOW - 86400),
      "splicing": (520, NOW - 3600), "bolt": (4100, NOW - 86400)}, True),
    ("https://webcrawl-a.example.org", "Open Web Crawl",
     "Covers a general crawl of the public web in many languages.",
     102_400_000, 0,
     {"lightning": (80000, NOW - 600), "weather": (900000, NOW - 600),
      "recipe": (700000, NOW - 600), "channel": (300000, NOW - 600)}, True),
    ("http://nostrarchive7x2qbcd.onion", "Nostr Relay Archive",
     "Covers long-form notes and relay announcements from public Nostr "
     "relays.", 2_150_000, 0,
     {"relay": (120000, NOW - 1800), "zap": (88000, NOW - 1800),
      "lightning": (40000, NOW - 1800)}, True),
    ("https://slow-archive.example.com", "Slow Mail Archive",
     "Covers developer mailing-list archives back to 2015.", 640_000, 0,
     {"lightning": (21000, NOW - 7200), "channel": (15000, NOW - 7200),
      "jamming": (800, NOW - 7200)}, "slow"),
    ("https://flaky-mirror.example.io", "Flaky Mirror",
     "Covers a partial mirror of technical blogs and newsletters.",
     310_000, 0,
     {"lightning": (9000, NOW - 3600), "channel": (6000, NOW - 3600)},
     "flaky"),
    ("https://papers.example.edu", "Distributed Systems Papers",
     "Covers peer-reviewed papers on consensus, gossip protocols and "
     "routing.", 41_000, 0,
     {"gossip": (3000, NOW - 86400 * 9), "consensus": (5200, NOW - 86400 * 9),
      "routing": (4400, NOW - 86400 * 9)}, False),
]


def _results(name, n=4):
    slug = name.lower().replace(" ", "-")
    return [{"id": f"{slug}-{i}",
             "url": f"https://{slug}.example/doc/{i}",
             "title": f"{name}: lightning channel jamming, part {i + 1}",
             "snippet": "Channel jamming ties up HTLC slots or liquidity "
                        "without paying fees. This note surveys upfront "
                        "fees, reputation and the trade-offs between them "
                        "for routing nodes on the network."}
            for i in range(n)]


def _behave(handle, health):
    def wrapped(path, body, headers):
        if path == "/query" and health == "slow":
            time.sleep(3.5)
        if path == "/query" and health == "flaky":
            return 500, {}, b"upstream timeout"
        return handle(path, body, headers)
    return wrapped


class _Naming:
    """Stands in for the naming store: names and Covers lines by pubkey."""

    def __init__(self, by_pk):
        self.by_pk = by_pk

    def view(self, pubkey):
        name, covers = self.by_pk[pubkey]
        return {"name": name, "description": covers, "assessment": None,
                "named_at": NOW - 86400 * 3, "described_at": NOW - 86400 * 3,
                "assessed_at": None, "status": "ok",
                "evidence_base": {"reported_documents": 18400,
                                  "observed_results": 212,
                                  "queries_routed": 37},
                "last_error": None,
                "history": [{"name": name, "from": NOW - 86400 * 3,
                             "to": None},
                            {"name": name + " (draft)",
                             "from": NOW - 86400 * 26,
                             "to": NOW - 86400 * 3}]}


def start(paid=False):
    tmp = Path(mkdtemp(prefix="demo-stack-"))
    peers, stats = PeerStore(), StatsCache()
    receive = ({"pricing_mode": "quoted", "margin_flat_msat": 500,
                "minter": "mock", "site_secret": "demo-site-secret"}
               if paid else {})
    cfg = BrokerConfig(listen_host="127.0.0.1", listen_port=0, data_dir=tmp,
                       receive=receive,
                       client={"expose_outcome": True,
                               "expose_raw_sets": True})
    handlers, names = {}, {}
    reach = Reachability(startup_grace_seconds=0)
    built = []
    for i, (ep, name, covers, docs, price, terms, healthy) in \
            enumerate(INDEXERS):
        idx = FakeIndexer(Identity.generate(),
                          price_msat=price if paid else 0,
                          db_path=tmp / f"idx{i}.db",
                          results=_results(name), document_count=docs,
                          terms=terms)
        handlers[ep] = _behave(idx.handle, healthy)
        names[idx.identity.pubkey] = (name, covers)
        built.append((ep, idx, healthy))
    transport = LoopbackTransport(handlers)
    gossip = Gossip(Identity.generate(), peers, transport, cfg, stats=stats)
    for ep, idx, healthy in built:
        ann = idx.announcement(ep)
        peers.add(ann)
        gossip.pull_stats(ann)
        if healthy is not False:
            reach.note_success(idx.identity.pubkey)
        else:
            reach.note_failure(idx.identity.pubkey,
                               "connection refused (port 443)")
    node = BrokerNode(Identity.generate(), peers,
                      IndexerClient(MockPayProvider() if paid
                                    else NullPayProvider(), transport),
                      receiver=build_receiver(cfg), stats=stats,
                      reachability=reach)
    broker = BrokerHTTPServer(node, cfg, naming=_Naming(names))
    broker.start_background()
    web = WebClientServer(WebClientConfig(
        listen_host="127.0.0.1", listen_port=0,
        broker_url=f"http://127.0.0.1:{broker.port}",
        title="Ditta search",
        site_secret=receive.get("site_secret", "")))
    web.start_background()
    return f"http://127.0.0.1:{web.port}", (web, broker)


if __name__ == "__main__":
    url, _servers = start(paid="--paid" in sys.argv)
    print(url, flush=True)
    while True:
        time.sleep(3600)
