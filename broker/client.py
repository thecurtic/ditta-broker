"""Outbound protocol client: query an indexer, driving the L402 pay loop.

A query to a paid indexer comes back HTTP 402 with an L402 challenge. The
client asks its ``PayProvider`` to settle the invoice, then retries the
byte-identical body with the ``Authorization`` header L402 defines — the
macaroon and preimage travel only in that header, so the retried body is
unchanged (Transport). Result sets are validated through the wire pipeline
before they are handed back; every string in them is attacker-controlled
display content and must be treated as untrusted downstream.

Transport is an injectable seam: ``HttpxTransport`` speaks real HTTP;
``LoopbackTransport`` dispatches to an in-process handler so the whole
402→pay→retry→authorize loop can be exercised without a network or a node.
"""

from __future__ import annotations

import json
import re
import threading
import time
from abc import ABC, abstractmethod
from urllib.parse import urlsplit

from .payment import PayProvider, PaymentError
from .wire import ValidationError, accept

_CHALLENGE_RE = re.compile(
    r'(?:L402|LSAT)\s+macaroon="([^"]+)",\s*invoice="([^"]+)"')


class IndexerError(Exception):
    """A downstream indexer refused or returned something unusable."""

    def __init__(self, message: str, code: str | None = None):
        super().__init__(message)
        self.code = code


class Transport(ABC):
    @abstractmethod
    def post(self, url: str, body: bytes, headers: dict) -> tuple[int, dict, bytes]:
        """POST raw bytes; return (status, response_headers, response_body)."""


class HttpxTransport(Transport):
    """Real HTTP, with an optional Tor SOCKS route for .onion endpoints.

    A ``.onion`` host is never resolved or dialed directly: without a
    configured proxy it is refused outright (attempting DNS on an onion
    name leaks it), with one it goes through the SOCKS proxy, hostname
    resolved by Tor. Onion circuits are slow, so they get their own timeout.
    """

    def __init__(self, timeout: float = 20, verify=True,
                 onion_proxy: str | None = None, onion_timeout: float = 60):
        self._timeout = timeout
        self._verify = verify
        self._onion_proxy = onion_proxy
        self._onion_timeout = onion_timeout

    def post(self, url, body, headers):
        import httpx

        host = urlsplit(url).hostname or ""
        kwargs = dict(content=body, headers=headers,
                      timeout=self._timeout, verify=self._verify)
        if host.endswith(".onion"):
            if not self._onion_proxy:
                raise IndexerError(
                    "onion endpoint refused: no tor socks proxy configured")
            kwargs["proxy"] = self._onion_proxy
            kwargs["timeout"] = self._onion_timeout
        r = httpx.post(url, **kwargs)
        return r.status_code, dict(r.headers), r.content


class LoopbackTransport(Transport):
    """Routes requests to in-process handlers keyed by endpoint base URL.

    Handlers have the signature ``handler(path, body, headers) -> (status,
    headers, body)``. For tests and single-process multi-node setups.
    """

    def __init__(self, handlers: dict):
        self._handlers = handlers

    def post(self, url, body, headers):
        for base, handler in self._handlers.items():
            if url.startswith(base):
                return handler(url[len(base):], body, headers)
        raise IndexerError(f"no loopback handler for {url}")


class IndexerClient:
    def __init__(self, pay_provider: PayProvider, transport: Transport):
        self._pay = pay_provider
        self._transport = transport
        # Asks fan out concurrently, but settlement is serialized: the pay
        # providers (NWC ledger, spend caps) assume one payment at a time.
        self._pay_lock = threading.Lock()

    @property
    def can_pay(self) -> bool:
        """Whether this client can settle invoices; economics consults it."""
        return self._pay.can_pay

    def query(self, endpoint: str, query_msg: dict, pricing_type: str,
              max_price: int, indexer_pubkey: str | None = None,
              receipt: dict | None = None) -> dict:
        """Run a query against one indexer, paying if challenged.

        ``query_msg`` is the fully-formed (and, if identified, signed) query
        request. ``indexer_pubkey`` is the identity from the announcement the
        route rests on; a 402 whose signed body does not verify against it is
        never paid (Payment profile). Returns the validated result set
        message. Raises IndexerError on refusal, malformed response, or an
        unaffordable/undoable payment.
        """
        url = endpoint.rstrip("/") + "/query"
        body = json.dumps(query_msg, separators=(",", ":")).encode("utf-8")
        headers = {"Content-Type": "application/json"}

        status, resp_headers, resp_body = self._post(url, body, headers)

        if status == 402:
            self._check_challenge_body(resp_headers, resp_body,
                                       query_msg["request_id"],
                                       indexer_pubkey)
            preimage = self._settle(resp_headers, max_price)
            if receipt is not None:  # the caller's record of the payment
                import hashlib
                receipt["payment_hash"] = hashlib.sha256(
                    bytes.fromhex(preimage)).hexdigest()
            auth = self._authorization(resp_headers, preimage)
            status, resp_headers, resp_body = self._post(
                url, body, {**headers, "Authorization": auth})
            if status == 402:
                raise IndexerError("indexer re-challenged after payment")

        if status != 200:
            raise self._as_error(resp_body, status)

        try:
            message, _ = accept(resp_body, "result_set")
        except ValidationError as e:
            raise IndexerError(f"malformed result set: {e}") from e
        return message

    def _post(self, url, body, headers):
        """Every transport failure is an IndexerError, whatever the transport.

        The routing contract — one indexer's failure never fails the route —
        holds only if nothing below this line can raise anything else. An
        unreachable endpoint (DNS failure, refused connection, timeout) is
        the same routing fact as a refusal: skip the node, continue.
        """
        try:
            return self._transport.post(url, body, headers)
        except IndexerError:
            raise
        except Exception as e:
            raise IndexerError(f"indexer unreachable: {e}") from e

    # -- L402 helpers ------------------------------------------------------

    def _check_challenge_body(self, headers: dict, body: bytes,
                              request_id: str, indexer_pubkey: str | None):
        """The Payment profile's rule for the payer: the body is authoritative.

        A broker MUST NOT pay from a 402 whose body is missing or unsigned,
        whose signature does not verify against the announcement pubkey it
        routed on, whose request identifier does not match its query, or
        whose header disagrees with its body.
        """
        try:
            m, _ = accept(body, "payment_challenge")
        except (ValidationError, Exception) as e:
            raise IndexerError(
                f"402 without a valid signed payment challenge body — "
                f"refusing to pay: {e}") from e
        if indexer_pubkey is not None and m["pubkey"] != indexer_pubkey:
            raise IndexerError(
                "payment challenge signed by a different identity than the "
                "announcement routed on — refusing to pay")
        if m["request_id"] != request_id:
            raise IndexerError(
                "payment challenge answers a different request — "
                "refusing to pay")
        macaroon, invoice = self._challenge(headers)
        if m["macaroon"] != macaroon or m["invoice"] != invoice:
            raise IndexerError(
                "402 header disagrees with its signed body — refusing to pay")
        if m["expiry"] < int(time.time()):
            raise IndexerError("payment challenge credential already expired")

    def _challenge(self, headers: dict):
        raw = headers.get("WWW-Authenticate") or headers.get("www-authenticate", "")
        m = _CHALLENGE_RE.search(raw)
        if not m:
            raise IndexerError("402 without a parseable L402 challenge")
        return m.group(1), m.group(2)  # macaroon, invoice

    def _settle(self, headers: dict, max_price: int) -> str:
        _macaroon, invoice = self._challenge(headers)
        try:
            with self._pay_lock:
                return self._pay.pay(invoice, max_price)
        except PaymentError as e:
            raise IndexerError(f"could not pay indexer: {e}") from e

    def _authorization(self, headers: dict, preimage: str) -> str:
        macaroon, _invoice = self._challenge(headers)
        return f"L402 {macaroon}:{preimage}"

    def _as_error(self, body: bytes, status: int) -> IndexerError:
        try:
            message, _ = accept(body, "error")
            return IndexerError(
                f"indexer error {message['code']}"
                + (f": {message['message']}" if message.get("message") else ""),
                code=message["code"])
        except (ValidationError, Exception):
            return IndexerError(f"indexer returned HTTP {status}")
