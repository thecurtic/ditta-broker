"""Outbound gossip: announce this broker to peers, and pull peers from them.

Bootstrap is out of scope for the protocol; here it is the ``seeds`` list in
config. On each cycle the broker POSTs its own signed announcement to every
seed's ``/announce`` and asks every seed's ``/peers`` for the announcements
it holds, ingesting whatever verifies. From then on, discovery's supersession
rules keep the freshest announcement per pubkey.
"""

from __future__ import annotations

import json
import time

from . import messages
from .client import Transport
from .config import BrokerConfig
from .identity import Identity
from .peers import PeerStore
from .wire import ValidationError, accept, validate_peers


def build_announcement(identity: Identity, cfg: BrokerConfig) -> dict:
    """This broker's current signed announcement (role: broker, no corpus).

    The pricing array is empty by rule, not by accident: a node not
    declaring `indexer` MUST carry an empty pricing array (Announcement).
    What the broker charges its own clients at /entry is the broker-client
    interface, which the spec leaves out of scope — quoted entry pricing
    could not be honestly expressed as a static announcement price anyway.
    """
    return identity.signed({
        "pubkey": identity.pubkey,
        "protocol_version": 0,
        "endpoint": cfg.endpoint,
        "timestamp": int(time.time()),
        "roles": ["broker"],
        "pricing": [],
    }, "announcement")


class Gossip:
    def __init__(self, identity: Identity, peers: PeerStore,
                 transport: Transport, cfg: BrokerConfig, stats=None,
                 reachability=None):
        self.identity = identity
        self.peers = peers
        self.transport = transport
        self.cfg = cfg
        self.stats = stats  # a StatsCache, when the deployment wants quotes
        # Every /corpus pull doubles as the reachability probe: gossip is
        # the heartbeat "searchable" rests on (see reachability.py).
        self.reachability = reachability

    def announce_to(self, base_url: str) -> bool:
        """POST our announcement to a peer's /announce. True on HTTP 200."""
        announcement = build_announcement(self.identity, self.cfg)
        body = json.dumps(announcement, separators=(",", ":")).encode()
        status, _headers, _body = self.transport.post(
            base_url.rstrip("/") + "/announce", body,
            {"Content-Type": "application/json"})
        return status == 200

    def pull_peers(self, base_url: str) -> int:
        """Ask a peer for the announcements it holds; ingest what verifies.

        Returns how many announcements were accepted into the store.
        """
        request = self.identity.signed(
            messages.peer_list_request(
                self.identity.pubkey, max_peers=self.cfg.max_peers_limit),
            "peer_list_request")
        body = json.dumps(request, separators=(",", ":")).encode()
        status, _headers, resp = self.transport.post(
            base_url.rstrip("/") + "/peers", body,
            {"Content-Type": "application/json"})
        if status != 200:
            return 0
        try:
            response, _ = accept(resp, "peer_list_response")
        except ValidationError:
            return 0
        if response["request_id"] != request["request_id"]:
            return 0
        accepted, _discarded = validate_peers(response)
        added = 0
        for announcement in accepted:
            if self.peers.add(announcement):
                added += 1
        return added

    def pull_stats(self, announcement: dict) -> bool:
        """Fetch one indexer's per-term statistics into the cache.

        Signed request, signed response, and the response must come from the
        pubkey the announcement named — a corpus claim is the one message a
        recipient can check against nothing else, so the signature is all the
        provenance there is.
        """
        if self.stats is None:
            return False
        if 0 not in announcement.get("corpus", {}).get(
                "statistics_versions", []):
            return False
        request = self.identity.signed(
            messages.corpus_statistics_request(
                self.identity.pubkey, statistics_version=0,
                max_terms=self.cfg.stats_max_terms),
            "corpus_statistics_request")
        body = json.dumps(request, separators=(",", ":")).encode()
        status, _headers, resp = self.transport.post(
            announcement["endpoint"].rstrip("/") + "/corpus", body,
            {"Content-Type": "application/json"})
        if status != 200:
            return False
        try:
            response, _ = accept(resp, "corpus_statistics_response")
        except ValidationError:
            return False
        if response["pubkey"] != announcement["pubkey"] \
                or response["request_id"] != request["request_id"]:
            return False
        # The last occurrence of a repeated term governs (spec: Corpus
        # statistics response); dict insertion order does exactly that.
        terms = {t["term"]: (t["document_count"], t["newest"])
                 for t in response["terms"]}
        self.stats.put(announcement["pubkey"], terms, response["updated"])
        return True

    def cycle(self) -> dict:
        """One gossip round over every seed. Failures are per-seed, not fatal."""
        report = {}
        for seed in self.cfg.seeds:
            try:
                announced = self.announce_to(seed)
                pulled = self.pull_peers(seed)
                report[seed] = {"announced": announced, "pulled": pulled}
            except Exception as e:
                report[seed] = {"error": str(e)}
        refreshed = 0
        for announcement in self.peers.indexers():
            pubkey = announcement["pubkey"]
            try:
                if self.pull_stats(announcement):
                    refreshed += 1
                    if self.reachability is not None:
                        self.reachability.note_success(pubkey)
                elif self.reachability is not None:
                    self.reachability.note_failure(
                        pubkey, "corpus pull answered but did not verify")
            except Exception as e:
                # One unreachable indexer never spoils the round — but the
                # failure is a reachability fact worth keeping.
                if self.reachability is not None:
                    self.reachability.note_failure(pubkey, str(e))
        if refreshed:
            report["stats_refreshed"] = refreshed
        return report
