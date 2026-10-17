"""The front ends: clients of /client, off by default, and the MCP server.

The web client and the MCP server ship in the broker package but are not
part of the broker: they import only the client library (and the
standalone wire library) and reach the broker over HTTP. Both are off
unless the config enables them. The MCP server is exercised end to end
over real sockets: free search, the paid search -> invoice -> redeem loop,
and its protocol edges.
"""

import ast
import hashlib
import json
import time
from pathlib import Path

import httpx
import pytest

from broker.__main__ import main, start_front_end
from broker.client import IndexerClient, LoopbackTransport
from broker.config import BrokerConfig, build_receiver
from broker.gossip import Gossip
from broker.identity import Identity
from broker.mcp import MCPConfig, MCPServer
from broker.node import BrokerNode
from broker.payment import L402Receiver, MockInvoiceMinter, MockPayProvider
from broker.peers import PeerStore
from broker.server import BrokerHTTPServer
from broker.stats import StatsCache
from helpers import FakeIndexer

PKG = Path(__file__).resolve().parents[1] / "broker"


# --- the boundary ------------------------------------------------------------

@pytest.mark.parametrize("module", ["webclient.py", "mcp.py", "clientlib.py"])
def test_front_ends_import_no_broker_internals(module):
    """A front end may import the client library, the wire library, and
    the package version — nothing else of the broker's."""
    allowed = {"clientlib", "wire", "__main__"}
    tree = ast.parse((PKG / module).read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.level == 0:
                mod = node.module or ""
                assert not mod.startswith("broker") or \
                    mod.split(".")[1] in allowed, (module, mod)
                continue
            if node.module is None:          # from . import x
                assert [a.name for a in node.names] == ["__version__"], \
                    (module, [a.name for a in node.names])
                continue
            assert node.module.split(".")[0] in allowed, \
                (module, node.module)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                assert not alias.name.startswith("broker"), \
                    (module, alias.name)


def test_front_ends_are_off_by_default(tmp_path):
    cfg = BrokerConfig(data_dir=tmp_path)
    assert not cfg.web_enabled and not cfg.mcp_enabled
    assert cfg.mcp_url is None
    assert start_front_end(cfg, "web") is None
    assert start_front_end(cfg, "mcp") is None
    empty = tmp_path / "broker.yaml"
    empty.write_text("{}\n")
    assert main(["mcp", str(empty)]) == 2
    assert main(["web", str(empty)]) == 2


def test_broker_starts_enabled_front_ends_against_itself(tmp_path):
    """Enabled, each front end comes up on its own port and reaches the
    broker over HTTP at the broker's own address by default."""
    broker = _broker(tmp_path, receive={})
    port = broker["server"].port
    cfg = BrokerConfig(
        listen_host="0.0.0.0", listen_port=port, data_dir=tmp_path,
        webclient={"enabled": True, "listen_port": 0},
        mcp={"enabled": True, "listen_port": 0})
    started = [start_front_end(cfg, w) for w in ("web", "mcp")]
    try:
        web, mcp = started
        assert web.cfg.broker_url == f"http://127.0.0.1:{port}"
        assert mcp.cfg.broker_url == f"http://127.0.0.1:{port}"
        page = httpx.get(f"http://127.0.0.1:{web.port}/", timeout=10)
        assert page.status_code == 200
        reply = _rpc(f"http://127.0.0.1:{mcp.port}/mcp", "tools/call",
                     {"name": "search", "arguments": {"query": "jamming"}})
        assert reply["result"]["structuredContent"]["status"] == "ok"
    finally:
        for s in started:
            s.shutdown()
        broker["server"].shutdown()


def test_get_root_names_the_mcp_url_only_when_enabled(tmp_path):
    off = _broker(tmp_path / "off", receive={})
    on = _broker(tmp_path / "on", receive={}, mcp={
        "enabled": True, "public_url": "https://search.example/mcp"})
    try:
        assert "mcp" not in httpx.get(off["base"] + "/", timeout=10).json()
        described = httpx.get(on["base"] + "/", timeout=10).json()
        assert described["mcp"]["url"] == "https://search.example/mcp"
        assert described["mcp"]["transport"] == "streamable-http"
        assert set(described["mcp"]["tools"]) == {
            "search", "redeem", "quote", "list_indexers"}
    finally:
        off["server"].shutdown()
        on["server"].shutdown()


def test_paid_402_states_hash_amount_and_settlement_window(tmp_path):
    """The /client 402 tells any client what it needs without decoding
    the invoice or the broker's token."""
    broker = _broker(tmp_path, receive=_PAID)
    try:
        before = int(time.time())
        r = httpx.post(broker["base"] + "/client/search", timeout=10,
                       content=json.dumps({"query": "jamming",
                                           "request_id": "r1"}).encode())
        assert r.status_code == 402
        body = r.json()
        assert len(r.headers["X-Payment-Hash"]) == 64
        assert r.headers["X-Invoice-Amount-Msat"] == "1500"
        expires = int(r.headers["X-Invoice-Expires"])
        assert before + 3600 <= expires <= int(time.time()) + 3600
        assert body["expiry"] > expires     # the credential outlives it
    finally:
        broker["server"].shutdown()


# --- the MCP server ----------------------------------------------------------

def test_mcp_handshake_and_tool_list(mcp_free):
    url, _broker = mcp_free
    init = _rpc(url, "initialize", {"protocolVersion": "2025-06-18",
                                    "capabilities": {},
                                    "clientInfo": {"name": "t",
                                                   "version": "0"}})
    result = init["result"]
    assert result["protocolVersion"] == "2025-06-18"
    assert result["serverInfo"]["name"] == "ditta-broker"
    assert "tools" in result["capabilities"]
    # An unknown version is answered with ours; the client decides.
    other = _rpc(url, "initialize", {"protocolVersion": "1999-01-01"})
    assert other["result"]["protocolVersion"] == "2025-11-25"
    # Notifications get 202 and no body.
    r = httpx.post(url, json={"jsonrpc": "2.0",
                              "method": "notifications/initialized"},
                   timeout=10)
    assert r.status_code == 202 and r.content == b""
    tools = _rpc(url, "tools/list")["result"]["tools"]
    assert [t["name"] for t in tools] == [
        "search", "redeem", "quote", "list_indexers"]
    assert all(t["inputSchema"]["type"] == "object" for t in tools)
    assert _rpc(url, "ping")["result"] == {}
    assert _rpc(url, "resources/list")["error"]["code"] == -32601


def test_mcp_free_search_quote_and_indexers(mcp_free):
    url, broker = mcp_free
    reply = _call(url, "search", {"query": "jamming mitigation", "n": 5})
    data = reply["structuredContent"]
    assert reply["isError"] is False
    assert data["status"] == "ok" and data["charged_msat"] == 0
    assert [r["url"] for r in data["results"]] == [
        "https://example.org/a", "https://example.org/b"]
    pubkey = broker["indexer"].identity.pubkey
    assert data["results"][0]["sources"] == [pubkey]
    assert data["queried"] == [pubkey]
    text = reply["content"][0]["text"]
    assert "third-party content" in text and "example.org/a" in text

    quote = _call(url, "quote", {"query": "jamming"})["structuredContent"]
    assert quote["quote_msat"] == 0 and quote["would_query"] == [pubkey]

    listed = _call(url, "list_indexers", {})["structuredContent"]
    [entry] = listed["indexers"]
    assert entry["pubkey"] == pubkey and entry["documents"] == 1000

    scoped = _call(url, "search", {"query": "jamming",
                                   "indexers": ["ab" * 32]})
    assert scoped["isError"] is True
    assert "unknown_pubkey" in scoped["content"][0]["text"]


def test_mcp_paid_search_invoice_then_redeem(mcp_paid):
    url, _broker = mcp_paid
    before = int(time.time())
    reply = _call(url, "search", {"query": "jamming mitigation"})
    assert reply["isError"] is False
    owed = reply["structuredContent"]
    assert owed["status"] == "payment_required"
    assert owed["amount_msat"] == 1500 and owed["amount_sats"] == 1.5
    assert before + 3600 <= owed["invoice_expires"] \
        <= int(time.time()) + 3600
    assert owed["invoice_expires_at"].endswith("Z")
    assert owed["credential_expires"] > owed["invoice_expires"]
    text = reply["content"][0]["text"]
    assert "1.5 sats" in text and owed["invoice_expires_at"] in text
    assert owed["invoice"] in text and owed["handle"] in text

    # A wrong preimage is refused here; the invoice is not replaced.
    wrong = _call(url, "redeem", {"handle": owed["handle"],
                                  "preimage": "ab" * 32})
    assert wrong["isError"] is True
    assert "does not match" in wrong["content"][0]["text"]
    garbled = _call(url, "redeem", {"handle": "nonsense",
                                    "preimage": "ab" * 32})
    assert garbled["isError"] is True

    # The agent's wallet pays and hands back the preimage.
    preimage = MockPayProvider().pay(owed["invoice"], 2000)
    assert hashlib.sha256(bytes.fromhex(preimage)).hexdigest() \
        == owed["payment_hash"]
    done = _call(url, "redeem", {"handle": owed["handle"],
                                 "preimage": preimage})
    data = done["structuredContent"]
    assert done["isError"] is False and data["status"] == "ok"
    assert data["charged_msat"] == 1500
    assert len(data["results"]) == 2

    # Spent: redeeming again prices the search afresh, and says why.
    again = _call(url, "redeem", {"handle": owed["handle"],
                                  "preimage": preimage})["structuredContent"]
    assert again["status"] == "payment_required"
    assert again["payment_hash"] != owed["payment_hash"]
    assert "already used" in again["note"]


def test_mcp_never_redeems_without_the_preimage(mcp_paid):
    """Settlement seen by the broker's node is not enough for the MCP
    server: it holds no trusted front end key."""
    url, broker = mcp_paid
    owed = _call(url, "search", {"query": "jamming"})["structuredContent"]
    broker["minter"].mark_settled(owed["payment_hash"])
    empty = _call(url, "redeem", {"handle": owed["handle"],
                                  "preimage": ""})
    assert empty["isError"] is True


def test_mcp_protocol_edges(mcp_free):
    url, _broker = mcp_free
    r = httpx.post(url, json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
                   headers={"Origin": "https://evil.example"}, timeout=10)
    assert r.status_code == 403
    assert httpx.get(url, timeout=10).status_code == 405
    r = httpx.post(url, content=b"{not json", timeout=10)
    assert r.json()["error"]["code"] == -32700
    batch = httpx.post(url, timeout=10, json=[
        {"jsonrpc": "2.0", "id": 1, "method": "ping"},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}]).json()
    assert [m["id"] for m in batch] == [1, 2]
    unknown = _rpc(url, "tools/call", {"name": "pay", "arguments": {}})
    assert unknown["error"]["code"] == -32602
    extra = _call(url, "search", {"query": "x", "wallet": "please"})
    assert extra["isError"] is True and "unknown arguments" in \
        extra["content"][0]["text"]


# --- rigs --------------------------------------------------------------------

_PAID = {"pricing_mode": "quoted", "margin_flat_msat": 500,
         "minter": "mock"}


def _broker(tmp_path, *, receive, mcp=None):
    """A broker over loopback HTTP with one indexer (1000 msat when the
    entry is paid, free otherwise) and its statistics cached."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    paid = bool(receive)
    indexer = FakeIndexer(
        Identity.generate(), price_msat=1000 if paid else 0,
        db_path=tmp_path / "idx.db",
        results=[{"id": "d1", "url": "https://example.org/a",
                  "title": "Alpha", "snippet": "s"},
                 {"id": "d2", "url": "https://example.org/b",
                  "title": "Beta", "snippet": "s"}],
        terms={"jamming": (200, 1), "mitigation": (120, 1)})
    peers, stats = PeerStore(), StatsCache()
    transport = LoopbackTransport({"http://indexer.test": indexer.handle})
    cfg = BrokerConfig(listen_host="127.0.0.1", listen_port=0,
                       data_dir=tmp_path, receive=receive, mcp=mcp or {})
    gossip = Gossip(Identity.generate(), peers, transport, cfg, stats=stats)
    announcement = indexer.announcement("http://indexer.test")
    assert peers.add(announcement) and gossip.pull_stats(announcement)
    minter = MockInvoiceMinter()
    receiver = build_receiver(cfg)
    if receiver is not None:
        receiver = L402Receiver(b"entry-secret", tmp_path / "entry.db",
                                minter)
    node = BrokerNode(Identity.generate(), peers,
                      IndexerClient(MockPayProvider(), transport),
                      receiver=receiver, stats=stats)
    server = BrokerHTTPServer(node, cfg)
    server.start_background()
    return {"base": f"http://127.0.0.1:{server.port}", "server": server,
            "indexer": indexer, "minter": minter}


def _mcp_for(broker):
    server = MCPServer(MCPConfig(enabled=True, listen_port=0,
                                 broker_url=broker["base"]))
    server.start_background()
    return server


@pytest.fixture
def mcp_free(tmp_path):
    broker = _broker(tmp_path, receive={})
    server = _mcp_for(broker)
    yield f"http://127.0.0.1:{server.port}/mcp", broker
    server.shutdown()
    broker["server"].shutdown()


@pytest.fixture
def mcp_paid(tmp_path):
    broker = _broker(tmp_path, receive=_PAID)
    server = _mcp_for(broker)
    yield f"http://127.0.0.1:{server.port}/mcp", broker
    server.shutdown()
    broker["server"].shutdown()


def _rpc(url, method, params=None, rid=1):
    msg = {"jsonrpc": "2.0", "id": rid, "method": method}
    if params is not None:
        msg["params"] = params
    r = httpx.post(url, json=msg, timeout=30,
                   headers={"Accept": "application/json, text/event-stream"})
    assert r.status_code == 200, r.text
    return r.json()


def _call(url, name, arguments):
    return _rpc(url, "tools/call", {"name": name,
                                    "arguments": arguments})["result"]
