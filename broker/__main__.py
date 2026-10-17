"""Run the broker and its front ends.

    python -m broker [config.yaml]        the broker, plus every front end
                                          the config switches on
    python -m broker web [config.yaml]    only the web client
    python -m broker mcp [config.yaml]    only the MCP server

Free mode by default: no config file means listen on 127.0.0.1:8410, charge
nothing, pay nothing, and route only to free indexers. Everything else — the
announced endpoint, seed peers, entry pricing (receive), the pay backend and
the front ends — comes from the YAML config (see broker/config.py).

The front ends (``webclient:`` and ``mcp:``, each off unless ``enabled:
true``) are clients of the broker's /client API, not parts of it: even when
the broker starts them in its own process they reach it over HTTP only, so
running one on another host is the same code with a different
``broker_url``.
"""

from __future__ import annotations

import functools
import sys
import threading
import time

from . import merge, routing
from .client import HttpxTransport, IndexerClient
from .learner import Learner
from .naming import InferenceClient, NamingService
from .config import build_pay_provider, build_receiver, load_config
from .gossip import Gossip
from .identity import Identity
from .node import BrokerNode
from .outcomes import OutcomeStore
from .peers import PeerStore
from .reachability import Reachability
from .server import BrokerHTTPServer
from .stats import StatsCache


FRONT_ENDS = ("web", "mcp")


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    mode = "broker"
    if argv and argv[0] in FRONT_ENDS:
        mode, argv = argv[0], argv[1:]
    cfg = load_config(argv[0] if argv else None)
    if mode != "broker":
        server = start_front_end(cfg, mode)
        if server is None:
            section = "webclient" if mode == "web" else "mcp"
            print(f"the {mode} front end is off: set {section}.enabled: "
                  f"true in the config to run it", file=sys.stderr)
            return 2
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            server.shutdown()
        return 0
    return run_broker(cfg)


def broker_url_for(cfg) -> str:
    """Where a front end started beside the broker reaches it."""
    host = cfg.listen_host
    if host in ("", "0.0.0.0", "::"):
        host = "127.0.0.1"
    return f"http://{host}:{cfg.listen_port}"


def start_front_end(cfg, which: str):
    """Start one front end in the background if the config switches it on;
    returns its server, or None when it is off. Each speaks to the broker
    over HTTP only; ``broker_url`` defaults to the broker's own address."""
    if which == "web" and cfg.web_enabled:
        from .webclient import WebClientServer, config_from

        web_cfg = config_from(cfg.webclient, broker_url_for(cfg))
        server = WebClientServer(web_cfg)
        server.start_background()
        print(f"web client on {web_cfg.listen_host}:{server.port} -> "
              f"broker {web_cfg.broker_url}", flush=True)
        return server
    if which == "mcp" and cfg.mcp_enabled:
        from .mcp import MCPServer, config_from

        mcp_cfg = config_from(cfg.mcp, broker_url_for(cfg))
        server = MCPServer(mcp_cfg)
        server.start_background()
        print(f"MCP server on {mcp_cfg.listen_host}:{server.port}/mcp -> "
              f"broker {mcp_cfg.broker_url}", flush=True)
        return server
    return None


def run_broker(cfg) -> int:

    identity = Identity.load_or_create(cfg.key_path)
    peers = PeerStore(future_skew=cfg.future_skew,
                      db_path=cfg.data_dir / "peers.db")
    stats = StatsCache(db_path=cfg.data_dir / "stats_cache.db")
    transport = HttpxTransport(timeout=cfg.request_timeout,
                               onion_proxy=cfg.tor_socks or None,
                               onion_timeout=cfg.tor_timeout)
    pay = build_pay_provider(cfg)
    receiver = build_receiver(cfg)
    client = IndexerClient(pay, transport)
    reach_cfg = cfg.reachability
    reachability = Reachability(
        window_seconds=int(reach_cfg.get("window_seconds", 1800)),
        degraded_horizon_seconds=int(
            reach_cfg.get("degraded_horizon_seconds", 21600)),
        startup_grace_seconds=int(
            reach_cfg.get("startup_grace_seconds", 120)))
    # The outcome store first: the learner is a projection over it.
    store = OutcomeStore(cfg.data_dir / "outcomes.db", stats=stats,
                         retention_days=cfg.outcomes.get(
                             "retention_days", 90))
    # Reachability is in-memory, but stats fetch-times and the outcome
    # store DID persist: inherit "recently reached" so a restart does not
    # read 'not yet contacted' next to a week of delivery history.
    for _pk, _ts in store.last_success_times().items():
        reachability.seed(_pk, _ts)
    for _ann in peers.indexers():
        _fetched = stats.fetched_at(_ann["pubkey"])
        if _fetched is not None:
            reachability.seed(_ann["pubkey"], _fetched)
    route_cfg = cfg.routing
    mode = str(route_cfg.get("mode", "learned")).lower()
    learner = None
    rank = routing.rank
    merge_fn = merge.merge_results
    if mode == "learned":
        learner = Learner(
            store,
            window_days=int(route_cfg.get("window_days", 7)),
            refresh_seconds=int(route_cfg.get("refresh_seconds", 300)),
            density_lambda=float(route_cfg.get("density_lambda", 0.8)))
        rank = learner.scorer
        merge_fn = functools.partial(
            merge.merge_evidence,
            divider_ratio=float(route_cfg.get("divider_ratio", 0.2)))
    node = BrokerNode(identity, peers, client, receiver=receiver,
                      fanout=cfg.fanout, stats=stats,
                      reachability=reachability, learner=learner,
                      routing_policy=route_cfg, rank=rank, merge=merge_fn)
    node.outcome_log.subscribe(store.record)
    # Indexer naming: local inference only, out of the query path, and
    # never a routing input. Configured entirely by naming: in the config
    # (or BROKER_NAMING_ENDPOINT / BROKER_NAMING_MODEL env vars); absent,
    # cards simply show the pubkey.
    import os as _os

    naming_cfg = cfg.naming
    naming_endpoint = (naming_cfg.get("endpoint")
                       or _os.environ.get("BROKER_NAMING_ENDPOINT", ""))
    naming_model = (naming_cfg.get("model")
                    or _os.environ.get("BROKER_NAMING_MODEL", ""))
    naming = None
    if naming_endpoint and naming_model:
        naming = NamingService(
            cfg.data_dir / "naming.db", peers, stats, node,
            client=InferenceClient(
                naming_endpoint, naming_model,
                api_key=(naming_cfg.get("api_key")
                         or _os.environ.get("BROKER_NAMING_API_KEY", "")),
                strip_reasoning=bool(naming_cfg.get("strip_reasoning")),
                extra_body=naming_cfg.get("extra_body") or {}),
            config=naming_cfg, learner=learner,
            reachability=reachability, store=store)
        naming.start_background()
    gossip = Gossip(identity, peers, transport, cfg, stats=stats,
                    reachability=reachability)

    server = BrokerHTTPServer(node, cfg, outcome_store=store,
                              naming=naming)
    server.start_background()
    print(f"broker {identity.pubkey[:16]}… listening on "
          f"{cfg.listen_host}:{cfg.listen_port}, announcing {cfg.endpoint}")
    print(f"entry: {'paid' if receiver else 'free'}; "
          f"pay: {'enabled' if pay.can_pay else 'disabled'}; "
          f"seeds: {list(cfg.seeds) or 'none'}", flush=True)

    def gossip_loop():
        while True:
            if cfg.seeds:
                report = gossip.cycle()
                known = len(peers.indexers())
                print(f"gossip: {report}; indexers known: {known}", flush=True)
            # An empty store means we are still bootstrapping — a seed may
            # simply not be up yet (a reboot starts everything at once), so
            # retry quickly until the network exists, then settle into the
            # configured cadence.
            if cfg.seeds and not peers.indexers():
                time.sleep(15)
            else:
                time.sleep(max(60, cfg.refresh_interval))

    threading.Thread(target=gossip_loop, daemon=True).start()

    front_ends = [s for s in (start_front_end(cfg, which)
                              for which in FRONT_ENDS) if s is not None]
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        for s in front_ends:
            s.shutdown()
        server.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
