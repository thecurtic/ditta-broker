"""The broker's HTTP face: the four protocol paths, plus its client API.

Protocol paths (/announce, /peers, /corpus, /query) behave exactly as DITTA
v0 specifies for a node declaring only `broker` — which means /corpus and
/query answer `rejected`: on the wire a broker sends queries, it does not
answer them. Customers (the demo page, agents) use the /client/* API, which
is deliberately out of protocol scope:

  GET  /client/quote    the price of an ask before it is sent
  GET  /                self-description of everything here
  POST /client/search   the one paid door: customer knobs in (query, n,
                        max_msat, exclude_tor, pubkeys), L402 402 out,
                        byte-identical replay redeems; consume-on-service;
                        head start
  /client/peers         the network listing: identity, name, coverage,
                        corpus, pricing, transport, reachability
  /client/status        settlement/prefetch state for one payment hash
  /client/announcements this broker's identity and the indexer
                        announcements it holds, exactly as signed

The paid response is {request_id, results} — merged results carrying
sources[] attribution — plus, behind the client.expose_outcome config flag,
the full outcome record (and raw per-indexer sets behind expose_raw_sets):
the visibility seam the demonstration front end renders.

Transport rules from the spec still govern the protocol paths: bodies are
JSON; a message's type is known from its path and direction; `code` in an
Error is authoritative and the HTTP status advisory; payment-layer failures
are a fresh 402, never a protocol Error.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import messages
from .config import BrokerConfig
from .node import BrokerNode
from .payment.l402 import decode_claims
from .prefetch import Prefetcher
from .wire import ParseError, ValidationError, parse, validate


_NO_BUDGET = 9007199254740991   # a free route spends what its provider allows
_HEX64 = re.compile(r"[0-9a-f]{64}")
_MAX_SCOPE = 64   # pubkeys in one selection; the network is far smaller


def _auth_payment_hash(auth_header: str) -> str | None:
    """The payment hash named by an L402 authorization header, unverified —
    good enough to key a prefetch lookup, never for authorization."""
    parts = (auth_header or "").split(None, 1)
    if len(parts) != 2:
        return None
    claims = decode_claims(parts[1].partition(":")[0]) or {}
    return claims.get("h")


class BrokerHTTPServer:
    def __init__(self, node: BrokerNode, cfg: BrokerConfig,
                 outcome_store=None, naming=None):
        self.node = node
        self.cfg = cfg
        # The outcome store, when wired, upgrades the peer cards from
        # claimed to demonstrated (delivery evidence on vocabulary terms).
        self.outcomes = outcome_store
        # Names/descriptions/assessments join the cards here; routing
        # never reads them (the naming spec's hard rule).
        self.naming = naming
        # The head start: watch our own node for settlement and route the
        # pending query while the payer is still coming back to redeem.
        self.prefetch = None
        if node.receiver is not None \
                and cfg.receive.get("head_start", True):
            self.prefetch = Prefetcher(
                node.route_query, node.receiver.invoice_settled,
                poll_interval=float(
                    cfg.receive.get("head_start_poll_seconds", 2)),
                ttl=int(cfg.receive.get("head_start_ttl_seconds", 3600)))
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, fmt, *args):  # quiet; systemd has the journal
                pass

            def do_GET(self):
                # The spec leaves broker-client discovery out of scope, so
                # the broker describes itself: one GET tells an agent (or a
                # person) how to use every service this node offers.
                path = self.path.split("?")[0].rstrip("/") or "/"
                if path == "/":
                    out = json.dumps(outer.describe(), indent=2).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(out)))
                    self.end_headers()
                    self.wfile.write(out)
                    return
                if path in ("/client/quote", "/client/peers",
                            "/client/status", "/client/announcements"):
                    # The read-only client API answers GET; same dispatch.
                    status, headers, out = outer.dispatch(
                        self.path, b"", dict(self.headers))
                    self.send_response(status)
                    for key, value in headers.items():
                        self.send_header(key, value)
                    self.send_header("Content-Length", str(len(out)))
                    if out:
                        self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(out)
                    return
                self.send_response(404)
                hint = b'{"hint": "GET / describes this node\'s services"}'
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(hint)))
                self.end_headers()
                self.wfile.write(hint)

            def do_POST(self):
                try:
                    length = int(self.headers.get("Content-Length", 0))
                    body = self.rfile.read(length) if length else b""
                    status, headers, out = outer.dispatch(
                        self.path, body, dict(self.headers))
                except Exception:
                    import traceback; traceback.print_exc()
                    status, headers, out = 500, {}, b""
                self.send_response(status)
                for key, value in headers.items():
                    self.send_header(key, value)
                self.send_header("Content-Length", str(len(out)))
                if "Content-Type" not in headers and out:
                    self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(out)

        self._httpd = ThreadingHTTPServer(
            (cfg.listen_host, cfg.listen_port), Handler)

    @property
    def port(self) -> int:
        """The bound port — useful when config asked for an ephemeral 0."""
        return self._httpd.server_address[1]

    def serve_forever(self):
        self._httpd.serve_forever()

    def start_background(self) -> threading.Thread:
        thread = threading.Thread(target=self.serve_forever, daemon=True)
        thread.start()
        return thread

    def shutdown(self):
        if self.prefetch is not None:
            self.prefetch.stop()
        self._httpd.shutdown()

    def describe(self) -> dict:
        """The self-description served at GET /: everything an agent needs
        to use this broker, since the spec leaves the broker-client
        interface out of scope and there is nothing else to discover from.
        """
        if self.node.receiver is None:
            entry = {"pricing": "free"}
        elif self.cfg.pricing_mode == "quoted":
            entry = {"pricing": "quoted",
                     "detail": "each query is priced by routing over the "
                               "cached network plus this broker's margin; "
                               "GET /client/quote first to see the price"}
        else:
            entry = {"pricing": "static",
                     "options": self.cfg.entry_pricing}
        return {
            "name": "ditta-broker",
            "protocol": "ditta/v0",
            "roles": ["broker"],
            "pubkey": self.node.identity.pubkey,
            "spec": "https://github.com/ditta-protocol/ditta-spec",
            "protocol_paths": {
                "/announce": "POST a signed announcement",
                "/peers": "POST a signed peer_list_request",
                "/corpus": "rejected: a broker maintains no corpus",
                "/query": "rejected: on the wire a broker sends queries, "
                          "it does not answer them — clients use "
                          "/client/search",
            },
            "client_paths": {
                "/client/search": "POST {query, request_id, n?, max_msat?, "
                                  "exclude_tor?, pubkeys?}. An unpaid ask "
                                  "gets HTTP 402 with an L402 challenge "
                                  "header and the JSON body {error: "
                                  "payment_required, request_id, invoice, "
                                  "macaroon, payment_hash, amount_msat, "
                                  "invoice_expires (unix seconds, the end "
                                  "of the settlement window), "
                                  "credential_expires}. Pay "
                                  "the invoice, then retry the "
                                  "byte-identical body with Authorization: "
                                  "L402 <macaroon>:<preimage>. Response: "
                                  "{request_id, results[], scope, "
                                  "charged_msat} with per-result sources[] "
                                  "attribution.",
                "/client/quote": "GET ?query=<terms>&n=<count>"
                                 "&max_msat=<cap>&exclude_tor=<bool>"
                                 "&pubkeys=<hex,hex> — the price of the ask "
                                 "before it is sent, the scope it would "
                                 "query, and the age of the statistics "
                                 "behind it",
                "/client/peers": "GET — every indexer: pubkey, name, "
                                 "covers, endpoint, transport, corpus "
                                 "summary, pricing, reachability "
                                 "(searchable), characteristic terms",
                "/client/announcements": "GET — this broker's pubkey and "
                                         "endpoint, and the indexer "
                                         "announcements it holds (each "
                                         "verified on arrival), as "
                                         "received",
                "/client/status": "GET ?hash=<payment_hash> — whether a "
                                  "challenged invoice has settled on this "
                                  "broker's own node and whether results "
                                  "are already prefetched",
            },
            "entry": {**entry,
                      "max_results_limit": self.cfg.max_results_limit},
            **({"mcp": {"url": self.cfg.mcp_url,
                        "transport": "streamable-http",
                        "tools": ["search", "redeem", "quote",
                                  "list_indexers"]}}
               if self.cfg.mcp_url else {}),
            "scope": {
                "pubkeys": "optional list of indexer pubkeys (hex) to "
                           "narrow which indexers are asked; absent or "
                           "empty means the full network. Within the "
                           "scope this broker alone routes, ranks and "
                           "merges. It never widens a scope.",
                "identity": "store and send PUBKEYS, never names — names "
                            "are display labels the broker regenerates "
                            "and they drift",
                "errors": {
                    "unknown_pubkey": "a pubkey this broker does not "
                                      "currently list; pubkeys[] names "
                                      "each",
                    "empty_scope": "nothing in the scope can be asked "
                                   "(Tor exclusion, unreachable, price "
                                   "cap); skipped[] gives each pubkey's "
                                   "reason",
                    "unsatisfiable": "no total fits under max_msat; "
                                     "pubkeys[] names the indexers the "
                                     "price rests on",
                },
            },
        }

    # -- dispatch ----------------------------------------------------------

    def dispatch(self, path: str, body: bytes, headers: dict):
        parsed = urllib.parse.urlparse(path)
        clean = parsed.path.rstrip("/") or parsed.path
        if clean == "/client/search":
            return self.handle_client_search(body, headers)
        if clean == "/client/quote":
            return self.handle_client_quote(parsed.query)
        if clean == "/client/peers":
            view = self.node.peer_view(self.cfg.cards)
            if self.naming is not None:
                for x in view["indexers"]:
                    try:
                        x["naming"] = self.naming.view(x["pubkey"])
                    except Exception:
                        x["naming"] = None
            reach = self.node.reachability
            for x in view["indexers"]:
                # The network listing (search-and-selection.md §8): enough
                # for an agent to choose a scope on purpose. ``name`` is a
                # display label only — selections are made by pubkey.
                naming = x.get("naming") or {}
                x["name"] = naming.get("name")
                x["covers"] = naming.get("description")
                x["searchable"] = (reach is None
                                   or reach.allowed(x["pubkey"]))
                if not x["searchable"]:
                    x["unsearchable_reason"] = {
                        "never": "never_contacted"}.get(
                        x["reach"]["status"], x["reach"]["status"])
            if self.outcomes is not None:
                for x in view["indexers"]:
                    try:
                        ev = self.outcomes.term_evidence(x["pubkey"])
                    except Exception:
                        continue  # evidence is an upgrade, never a break
                    for entry in x.get("vocabulary", []):
                        if entry["term"] in ev["delivered"]:
                            entry["evidence"] = "delivered"
                        elif entry["term"] in ev["failed"]:
                            entry["evidence"] = "failed"
                    x["evidence"] = {
                        "demonstrated": len(ev["delivered"]),
                        "tried": len(ev["tried"]),
                        "answered": ev["answered"],
                    }
            return 200, {}, json.dumps(view).encode()
        if clean == "/client/announcements":
            # Read-only: the signed announcements this broker already holds
            # (any node may ask for them over /peers), exactly as received,
            # plus where this broker itself is reached — for the explainer's
            # live panel and for agents finding this API.
            return 200, {}, json.dumps({
                "broker": {"pubkey": self.node.identity.pubkey,
                           "endpoint": self.cfg.endpoint},
                "announcements": self.node.peers.indexers()[:20],
            }).encode()
        if clean == "/client/beacon":
            # A page interaction, aggregate-only: {request_id, url}. No
            # user identifier exists or is recorded — this credits the
            # indexers that served a clicked URL, nothing else.
            if self.outcomes is None:
                return 200, {}, b'{"ok": false}'
            try:
                data = json.loads(body)
                query_id = str(data.get("request_id", ""))[:64]
                url = str(data.get("url", ""))[:2048]
            except (ValueError, AttributeError):
                return 200, {}, b'{"ok": false}'
            from .merge import normalize_url

            ok = bool(query_id and url
                      and self.outcomes.add_click(query_id,
                                                  normalize_url(url)))
            return 200, {}, json.dumps({"ok": ok}).encode()
        if clean == "/client/status":
            # Has this challenge's invoice settled yet? A waiting client
            # shows "payment detected" (our own site then redeems with the
            # preimage slot empty). The prefetcher answers from memory;
            # when it has no entry (head start off, or lost to a restart)
            # the node itself is asked. Reveals only settlement of a hash
            # the asker holds.
            params = urllib.parse.parse_qs(parsed.query)
            payment_hash = (params.get("hash") or [""])[0]
            status = (self.prefetch.status(payment_hash)
                      if self.prefetch is not None
                      else {"settled": False, "ready": False})
            if not status["settled"] and self.node.receiver is not None \
                    and _HEX64.fullmatch(payment_hash):
                status["settled"] = \
                    self.node.receiver.invoice_settled(payment_hash)
            return 200, {}, json.dumps(status).encode()
        route = {
            "/announce": self.handle_announce,
            "/peers": self.handle_peers,
            "/corpus": self.handle_corpus,
            "/query": self.handle_query,
        }.get(clean)
        if route is None:
            return 404, {}, self._error(
                "rejected", None,
                "unknown path — GET / describes this node's services")
        return route(body, headers)

    def _error(self, code: str, request_id: str | None,
               message: str | None = None) -> bytes:
        err = messages.error(self.node.identity.pubkey, code,
                             request_id=request_id, message=message)
        err = self.node.identity.signed(err, "error")
        return json.dumps(err, separators=(",", ":")).encode()

    def _reject(self, exc, body: bytes):
        """Map a pipeline rejection onto the wire Error vocabulary."""
        code = "invalid_signature" if getattr(exc, "code", "") \
            == "bad_signature" else "malformed"
        request_id = None
        try:  # echo the request id when the body parsed far enough to have one
            m = parse(body)
            rid = m.get("request_id")
            if isinstance(rid, str) and 1 <= len(rid.encode()) <= 64:
                request_id = rid
        except ParseError:
            pass
        return 400, {}, self._error(code, request_id, str(exc))

    # -- endpoints ---------------------------------------------------------

    def handle_announce(self, body: bytes, headers: dict):
        try:
            m = parse(body)
            validate(m, "announcement")
        except (ParseError, ValidationError) as e:
            return self._reject(e, body)
        # Accepting an announcement is HTTP 200 with an empty body. A stale
        # or future-dated one is IGNORED, not an error — ignoring is the
        # spec's word, and the sender did nothing wrong on the wire.
        self.node.peers.add(m)
        return 200, {}, b""

    def handle_peers(self, body: bytes, headers: dict):
        try:
            m = parse(body)
            validate(m, "peer_list_request")
        except (ParseError, ValidationError) as e:
            return self._reject(e, body)
        limit = min(m.get("max_peers", self.cfg.max_peers_limit),
                    self.cfg.max_peers_limit)
        response = {
            "pubkey": self.node.identity.pubkey,
            "request_id": m["request_id"],
            "peers": self.node.peers.all()[:limit],
        }
        response = self.node.identity.signed(response, "peer_list_response")
        return 200, {}, json.dumps(response, separators=(",", ":")).encode()

    def handle_corpus(self, body: bytes, headers: dict):
        try:
            m = parse(body)
            validate(m, "corpus_statistics_request")
        except (ParseError, ValidationError) as e:
            return self._reject(e, body)
        # A pure broker maintains no corpus; refusing is local policy.
        return 400, {}, self._error(
            "rejected", m["request_id"],
            "this node is a broker and serves no corpus")

    def handle_query(self, body: bytes, headers: dict):
        """The protocol /query on a node that declares only `broker`.

        The spec is explicit (Transport): a node not declaring `indexer`
        answers /query with an Error, `rejected` — on the wire a broker
        sends queries, it does not answer them. This broker's own clients
        enter through the off-protocol /client/search, which is not part
        of the protocol surface.
        """
        try:
            m = parse(body)
            validate(m, "query_request")
        except (ParseError, ValidationError) as e:
            return self._reject(e, body)
        return 403, {}, self._error(
            "rejected", m["request_id"],
            "this node routes queries; it does not answer them — "
            "clients enter at POST /client/search (GET / describes it)")

    def handle_client_quote(self, query_string: str):
        """GET /client/quote — the entry price for an ask, before it is sent.

        Reads only the in-memory peer store and cached statistics; no
        indexer is contacted. The response echoes the ask so the client
        can show it unchanged later; the statistics age explains why the
        eventual invoice may differ.
        """
        params = urllib.parse.parse_qs(query_string)
        ask, problem = self._parse_ask(
            {k: v[0] for k, v in params.items()}, from_query=True)
        if problem:
            return 400, {}, json.dumps({"error": "bad_request",
                                        "message": problem}).encode()
        quote, total, problem = self._price(ask)
        scope_error = self._scope_problem(ask, quote)
        if scope_error:
            return 200, {}, json.dumps({**scope_error, "ask": ask}).encode()
        if problem:
            return 200, {}, json.dumps(
                {"error": "unsatisfiable", "reason": problem,
                 "pubkeys": list(quote.asked), "ask": ask}).encode()
        return 200, {}, json.dumps({
            "quote_msat": total,
            "ask": ask,
            "indexers_considered": len(
                self.node._candidates(ask["exclude_tor"],
                                      set(ask.get("pubkeys", [])))),
            "scope": self._quote_scope(ask, quote),
            "stats_age_s": quote.stats_age_seconds,
        }).encode()

    def _parse_ask(self, data: dict, *, from_query: bool = False):
        """Normalize the customer's knobs; returns (ask, problem)."""
        def as_int(v):
            try:
                return int(v)
            except (TypeError, ValueError):
                return None

        query = data.get("query")
        if not isinstance(query, str) or not query.strip():
            return None, "query is required"
        if len(query.encode()) > 4096:
            return None, "query too long"
        request_id = data.get("request_id")
        if from_query and request_id is None:
            request_id = ""   # a quote needs no correlation id
        if not isinstance(request_id, str)                 or (request_id and not 1 <= len(request_id.encode()) <= 64):
            return None, "request_id must be 1-64 bytes"
        n = as_int(data.get("n", 10))
        if n is None or not 1 <= n <= min(50, self.cfg.max_results_limit):
            return None, ("n must be between 1 and "
                          f"{min(50, self.cfg.max_results_limit)}")
        max_msat = data.get("max_msat")
        if max_msat is not None:
            max_msat = as_int(max_msat)
            if max_msat is None or max_msat < 0:
                return None, "max_msat must be a non-negative integer"
        exclude = data.get("exclude_tor")
        if from_query:
            exclude = str(exclude).lower() in ("1", "true")
        elif not isinstance(exclude, bool):
            exclude = exclude is True
        ask = {"query": query, "request_id": request_id, "n": n,
               "exclude_tor": exclude}
        if max_msat is not None:
            ask["max_msat"] = max_msat
        pubkeys = data.get("pubkeys")
        if from_query and isinstance(pubkeys, str):
            pubkeys = [p for p in pubkeys.split(",") if p]
        if pubkeys is not None:
            if not isinstance(pubkeys, list) or not all(
                    isinstance(p, str) and _HEX64.fullmatch(p)
                    for p in pubkeys):
                return None, ("pubkeys must be a list of 64-character "
                              "lowercase hex pubkeys")
            if len(pubkeys) > _MAX_SCOPE:
                return None, f"pubkeys may name at most {_MAX_SCOPE}"
            pubkeys = list(dict.fromkeys(pubkeys))   # dedupe, keep order
            if pubkeys:
                ask["pubkeys"] = pubkeys
        return ask, None

    def _scope_problem(self, ask: dict, quote):
        """(error body, None) when a scoped ask cannot be honoured, else
        None. Unknown pubkeys are named, never dropped; a scope nothing in
        which can be asked is an error, never a widening to the network."""
        pubkeys = ask.get("pubkeys")
        if not pubkeys:
            return None
        listed = {a["pubkey"] for a in self.node.peers.indexers()}
        unknown = [p for p in pubkeys if p not in listed]
        if unknown:
            return {"error": "unknown_pubkey", "pubkeys": unknown,
                    "message": "not an indexer this broker currently "
                               "lists: " + ", ".join(unknown)}
        if not quote.asked:
            return {"error": "empty_scope",
                    "skipped": [{"pubkey": p, "reason": r}
                                for p, r in quote.skipped],
                    "message": "none of the selected indexers can be "
                               "asked; the search was not widened to the "
                               "full network"}
        return None

    @staticmethod
    def _quote_scope(ask: dict, quote) -> dict:
        return {"selected": list(ask.get("pubkeys", [])),
                "queried": list(quote.asked),
                "skipped": [{"pubkey": p, "reason": r}
                            for p, r in quote.skipped]}

    @staticmethod
    def _route_scope(ask: dict, outcome) -> dict:
        """The effective scope of a finished route: who was asked, who of
        those failed, and — for a scoped ask only — each selected indexer
        that was not asked, with the reason."""
        selected = list(ask.get("pubkeys", []))
        chosen = set(selected)
        asked = [n for n in outcome.nodes if n.stage != "considered"]
        return {"selected": selected,
                "queried": [n.pubkey for n in asked],
                "failed": [n.pubkey for n in asked if n.stage == "failed"],
                "skipped": [{"pubkey": n.pubkey,
                             "reason": n.dropped or "not_routed"}
                            for n in outcome.nodes
                            if n.stage == "considered"
                            and n.pubkey in chosen]}

    def _price(self, ask: dict):
        """(quote, total_msat, problem) for an ask under current config.

        Free brokers price everything at zero. Quoted mode runs the real
        routing over the cache within the customer's budget; static mode
        charges the config price list. ``unsatisfiable`` means no total
        fits under max_msat.
        """
        budget = self.cfg.downstream_budget(ask.get("max_msat"))
        quote = self.node.quote("" + ask["query"], ask["n"], "per_query",
                                exclude_onion=ask["exclude_tor"],
                                budget_msat=budget,
                                scope=ask.get("pubkeys"))
        if self.node.receiver is None:
            return quote, 0, None
        if self.cfg.pricing_mode == "quoted":
            total = self.cfg.with_margin(quote.downstream_msat)
        else:
            price = self.cfg.entry_price("per_query")
            per_result = self.cfg.entry_price("per_result")
            if price is not None:
                total = price
            elif per_result is not None:
                total = per_result * ask["n"]
            else:
                return quote, 0, "this broker's price list offers no "                                  "per-query or per-result entry"
        cap = ask.get("max_msat")
        if cap is not None and total > cap:
            return quote, total, (f"the cheapest satisfying total is "
                                  f"{total} msat; max_msat is {cap}")
        return quote, total, None

    def _from_our_site(self, headers: dict) -> bool:
        """Does this request carry the web client's shared site secret?
        Unset secret: nothing is ever witnessed, and the site falls back
        to the pasted preimage like any other client."""
        secret = str(self.cfg.receive.get("site_secret") or "")
        offered = str(headers.get("X-Site-Secret") or "")
        return bool(secret) and hmac.compare_digest(secret, offered)

    def handle_client_search(self, body: bytes, headers: dict):
        """POST /client/search — the one customer door.

        The body is the customer envelope (§5 knobs), not a protocol
        message; the broker translates it into protocol query requests
        downstream. L402 wraps it exactly as before: unpaid asks get a 402
        with a plain JSON payment challenge, the credential binds the sha256
        of these exact bytes, and redemption replays them byte-identical.
        Consume-on-service and the head start both apply unchanged.
        """
        try:
            data = json.loads(body)
            if not isinstance(data, dict):
                raise ValueError("not an object")
        except (ValueError, UnicodeDecodeError):
            return 400, {}, json.dumps(
                {"error": "bad_request",
                 "message": "the body must be a JSON object"}).encode()
        ask, problem = self._parse_ask(data)
        if not problem and not ask["request_id"]:
            problem = "request_id is required"
        if problem:
            return 400, {}, json.dumps(
                {"error": "bad_request", "message": problem}).encode()
        rid = ask["request_id"]
        exclude = ask["exclude_tor"]

        quote, total, problem = self._price(ask)
        # Refused BEFORE any invoice is minted: a scope that cannot be
        # honoured is never charged for, and never widened.
        scope_error = self._scope_problem(ask, quote)
        if scope_error:
            return 400, {}, json.dumps(
                {"request_id": rid, **scope_error}).encode()
        if problem:
            return 400, {}, json.dumps(
                {"request_id": rid, "error": "unsatisfiable",
                 "pubkeys": list(quote.asked),
                 "message": problem}).encode()
        scope = tuple(ask.get("pubkeys", ())) or None

        charging = self.node.receiver is not None and total > 0
        auth = headers.get("Authorization", "")
        charged = 0
        # Our own site vouches for itself with receive.site_secret; only
        # then may an empty preimage slot redeem on the node's witness.
        # Agents on this same door present the preimage, as L402 says.
        witnessed = self._from_our_site(headers)
        if charging:
            body_hash = hashlib.sha256(body).hexdigest()
            if not self.node.entry_redeemable(
                    auth, "per_query", body_hash, request_id=rid,
                    witnessed=witnessed):
                challenge_headers, challenge_body = self.node.entry_challenge(
                    total, "per_query", 1, body_hash, request_id=rid)
                if self.prefetch is not None:
                    claims = decode_claims(challenge_body["macaroon"]) or {}
                    if claims.get("h"):
                        self.prefetch.register(claims["h"], {
                            "query": ask["query"], "max_results": ask["n"],
                            "pricing_type": "per_query",
                            "max_price": (self.cfg.downstream_budget(
                                ask.get("max_msat"))
                                if ask.get("max_msat") is not None
                                else quote.downstream_msat),
                            "exclude_onion": exclude,
                            "request_id": rid, "ask_echo": ask,
                            "quote_msat": total, "charged_msat": total,
                            "scope": scope,
                        })
                return 402, challenge_headers, json.dumps(
                    challenge_body, separators=(",", ":")).encode()
            token = decode_claims(
                (auth.split(None, 1) + [""])[1].partition(":")[0]) or {}
            charged = token.get("a", total)                 if isinstance(token.get("a", total), int) else total

        outcome = None
        if charging and self.prefetch is not None:
            payment_hash = _auth_payment_hash(auth)
            if payment_hash:
                outcome = self.prefetch.claim(payment_hash)
        if outcome is None:
            outcome = self.node.route_query(
                ask["query"], ask["n"], "per_query",
                (self.cfg.downstream_budget(ask.get("max_msat"))
                 if ask.get("max_msat") is not None
                 else quote.downstream_msat if charging else _NO_BUDGET),
                exclude_onion=exclude, request_id=rid, ask_echo=ask,
                quote_msat=total, charged_msat=charged, scope=scope)

        served = any(n.ok for n in outcome.nodes)
        response = {"request_id": rid, "results": outcome.results,
                    "scope": self._route_scope(ask, outcome),
                    # what this search cost the customer: a credential a
                    # route that served nothing leaves unspent costs 0
                    "charged_msat": charged if served else 0}
        if self.cfg.expose_outcome:
            response["outcome"] = outcome.to_dict(
                include_raw=self.cfg.expose_raw_sets)
        out = json.dumps(response, separators=(",", ":")).encode()
        out_headers = {}
        if charging:
            # Consume on SERVICE, not on presentation: only a route at
            # least one indexer answered spends the credential.
            if served:
                self.node.entry_consume(auth, rid, witnessed=witnessed)
            else:
                out_headers["X-Credential-Retained"] = "1"
        return 200, out_headers, out


