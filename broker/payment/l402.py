"""Receive-side L402: challenge minting and authorization.

Implements the paid half of the Transport section and the Payment profile's
credential rules. An unauthorized query is answered with a 402 and

    WWW-Authenticate: L402 macaroon="<base64>", invoice="<bolt11>"

The broker retries the byte-identical body with

    Authorization: L402 <base64(macaroon)>:<hex(preimage)>

The macaroon is opaque to the payer: ours is a compact HMAC-authenticated
token binding payment_hash, pricing type, quantity, and expiry. Redemption
needs no invoice lookup — knowing the preimage of a hash we minted proves the
invoice settled. Written against the spec and the L402 reference, independent
of any other implementation.

Off-protocol, and only for the broker's own human-facing site: a
presentation the server vouches for (``witnessed=True``) may leave the
preimage slot empty (``L402 <macaroon>:``). The broker minted the invoice
on its own node, so that node is a witness of equal standing: a token whose
HMAC checks out redeems once the minter reports the invoice settled. A
person pays and the results arrive; pasting the preimage is the site's
fallback, not its flow. Every other presentation — agents on the client
API, protocol peers — is unwitnessed, and an empty slot is simply a bad
credential: the preimage, which needs no lookup at all, is required.

State follows the Payment profile exactly: nothing between challenge and
redemption; after first redemption, a spent mark for a single-use token, or
the set of served request identifiers for a multi-use one (a repeated
request identifier is served without being counted again). A spent or
expired token yields a fresh 402, never a protocol Error — payment-layer
failures are L402's concern, not the wire's.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import sqlite3
import time
from pathlib import Path

from .invoice import InvoiceMinter

_SCHEMA = """
CREATE TABLE IF NOT EXISTS l402_tokens (
    payment_hash TEXT PRIMARY KEY,
    remaining    INTEGER NOT NULL,
    expiry       INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS l402_served (
    payment_hash TEXT NOT NULL,
    request_id   TEXT NOT NULL,
    PRIMARY KEY (payment_hash, request_id)
);
"""


def decode_claims(macaroon_b64: str) -> dict | None:
    """Decode a token's claims WITHOUT verifying its authenticity.

    The HMAC only checks out on the node that minted the token; anyone else
    can still read the claims — payment hash ``h``, pricing type ``t``,
    quantity ``q``, expiry ``e``, and body binding ``b`` when present — to
    match a token to a pending request or an invoice. Display and bookkeeping
    only; never authorization.
    """
    try:
        blob = base64.urlsafe_b64decode(macaroon_b64.strip())
        body_b64, _, _mac = blob.rpartition(b".")
        claims = json.loads(base64.urlsafe_b64decode(body_b64))
    except (ValueError, json.JSONDecodeError):
        return None
    return claims if isinstance(claims, dict) else None


class L402Receiver:
    def __init__(self, secret: bytes, db_path: str | Path,
                 minter: InvoiceMinter, token_ttl: int = 86400):
        self._secret = hashlib.sha256(b"ditta-broker-l402:" + secret).digest()
        db_path = Path(db_path)
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._con = sqlite3.connect(db_path, check_same_thread=False)
        self._con.executescript(_SCHEMA)
        self._con.commit()
        self._minter = minter
        self._ttl = token_ttl

    # -- challenge ---------------------------------------------------------

    def challenge(self, amount_msat: int, pricing_type: str,
                  quantity: int, body_sha256: str | None = None,
                  bound_pubkey: str | None = None) -> dict:
        """Mint an invoice and a token; return the challenge's parts.

        Returns ``{"header", "macaroon", "invoice", "expiry",
        "payment_hash", "amount_msat", "invoice_expires"}`` — the header is
        the WWW-Authenticate value, and macaroon/invoice/expiry are what the
        signed payment challenge body duplicates (Payment profile: the
        expiry MUST be recorded in the macaroon and stated in the body).
        ``expiry`` is when the credential lapses; ``invoice_expires`` is the
        end of the settlement window, after which the invoice cannot be
        paid at all.

        ``body_sha256`` (hex) binds the token to the exact request bytes it
        was minted for: redemption then requires the byte-identical retry the
        spec describes, and anyone holding the token can check — without the
        HMAC secret — which request it belongs to (see ``decode_claims``).
        ``bound_pubkey`` binds a token minted for a SIGNED query to that
        query's key, as the Payment profile requires — a presentation under
        any other key gets a fresh 402.
        """
        payment_hash, bolt11 = self._minter.mint(amount_msat, "ditta query")
        now = int(time.time())
        expiry = now + self._ttl
        claims = {
            "h": payment_hash,
            "t": pricing_type,
            "q": quantity,
            "e": expiry,
            "a": amount_msat,   # what was charged — read back for receipts
        }
        if body_sha256 is not None:
            claims["b"] = body_sha256
        if bound_pubkey is not None:
            claims["k"] = bound_pubkey
        body = json.dumps(claims, separators=(",", ":")).encode()
        mac = hmac.new(self._secret, body, hashlib.sha256).hexdigest()
        token = base64.urlsafe_b64encode(
            base64.urlsafe_b64encode(body) + b"." + mac.encode()).decode()
        return {
            "header": f'L402 macaroon="{token}", invoice="{bolt11}"',
            "macaroon": token,
            "invoice": bolt11,
            "expiry": expiry,
            "payment_hash": payment_hash,
            "amount_msat": amount_msat,
            "invoice_expires": now + self._minter.invoice_expiry,
        }

    # -- redemption --------------------------------------------------------

    def authorize(self, auth_header: str, pricing_type: str,
                  body_sha256: str | None = None,
                  pubkey: str | None = None,
                  request_id: str | None = None) -> bool:
        """True if the header carries a valid, unspent, unexpired token of the
        right pricing type — and consumes one use. False means answer with a
        fresh challenge (never a protocol Error).

        A token minted with a body binding redeems only against the same
        ``body_sha256``; one minted with a key binding only under the same
        requester ``pubkey``. Under a multi-use token, a ``request_id``
        already served does not consume another use (Payment profile: a
        repeated identifier is not counted again).
        """
        claims = self._checked(auth_header, pricing_type, body_sha256, pubkey)
        if claims is None:
            return False
        if claims["q"] > 1 and request_id is not None:
            return self._consume_multi(claims["h"], claims["q"], claims["e"],
                                       request_id)
        return self._consume(claims["h"], claims["q"], claims["e"])

    def invoice_settled(self, payment_hash: str) -> bool:
        """Whether our own node has seen this invoice settle — the head
        start's trigger, and the proof behind a preimage-less redemption."""
        return self._minter.settled(payment_hash)

    # -- consume-on-service ------------------------------------------------
    #
    # A credential is spent by SERVICE, not by presentation: ``redeemable``
    # checks everything without consuming, and ``consume`` is called only
    # once results were actually produced. A route that produced nothing
    # leaves the credential intact for a retry — the payer never loses a
    # paid credential to a broken network.

    def redeemable(self, auth_header: str, pricing_type: str,
                   body_sha256: str | None = None,
                   pubkey: str | None = None,
                   request_id: str | None = None,
                   witnessed: bool = False) -> bool:
        """All of ``authorize``'s checks, none of its consumption.
        ``witnessed`` lets an empty preimage slot redeem on our node's
        word — the caller vouches the presentation came from our site."""
        claims = self._checked(auth_header, pricing_type, body_sha256, pubkey,
                               witnessed)
        if claims is None:
            return False
        if claims["q"] > 1 and request_id is not None \
                and self._already_served(claims["h"], request_id):
            return True  # a repeated identifier is never counted again
        row = self._con.execute(
            "SELECT remaining FROM l402_tokens WHERE payment_hash = ?",
            (claims["h"],)).fetchone()
        return row is None or row[0] > 0

    def consume(self, auth_header: str,
                request_id: str | None = None,
                witnessed: bool = False) -> bool:
        """Spend one use — call only after the request was actually served."""
        claims = self._decode(auth_header, witnessed)
        if claims is None:
            return False
        if claims["q"] > 1 and request_id is not None:
            return self._consume_multi(claims["h"], claims["q"], claims["e"],
                                       request_id)
        return self._consume(claims["h"], claims["q"], claims["e"])

    def _checked(self, auth_header: str, pricing_type: str,
                 body_sha256: str | None, pubkey: str | None,
                 witnessed: bool = False) -> dict | None:
        claims = self._decode(auth_header, witnessed)
        if claims is None:
            return None
        if claims["t"] != pricing_type or claims["e"] < int(time.time()):
            return None
        if "b" in claims and claims["b"] != body_sha256:
            return None
        if "k" in claims and claims["k"] != pubkey:
            return None
        return claims

    def _already_served(self, payment_hash: str, request_id: str) -> bool:
        return self._con.execute(
            "SELECT 1 FROM l402_served WHERE payment_hash = ? "
            "AND request_id = ?", (payment_hash, request_id)).fetchone() \
            is not None

    def _decode(self, auth_header: str,
                witnessed: bool = False) -> dict | None:
        parts = (auth_header or "").strip().split(None, 1)
        if len(parts) != 2 or parts[0] not in ("L402", "LSAT"):
            return None
        token_b64, _, preimage_hex = parts[1].partition(":")
        try:
            blob = base64.urlsafe_b64decode(token_b64.strip())
            body_b64, _, mac = blob.rpartition(b".")
            body = base64.urlsafe_b64decode(body_b64)
            if not hmac.compare_digest(
                    hmac.new(self._secret, body, hashlib.sha256).hexdigest(),
                    mac.decode("ascii")):
                return None
            claims = json.loads(body)
            preimage = bytes.fromhex(preimage_hex.strip())
        except (ValueError, json.JSONDecodeError, UnicodeDecodeError):
            return None
        if not preimage:
            # The slot is empty. Only a presentation the server vouches
            # for (our own site) may lean on our node as the witness; the
            # HMAC has already shown we minted this token, so settlement of
            # its invoice is ours to know — ask the minter, never the payer.
            if not witnessed:
                return None
            return claims if self._minter.settled(claims["h"]) else None
        if hashlib.sha256(preimage).hexdigest() != claims["h"]:
            return None
        return claims

    def _consume_multi(self, payment_hash: str, quantity: int, expiry: int,
                       request_id: str) -> bool:
        """One use per distinct request identifier: a repeated identifier is
        served again without touching the credential's remaining count."""
        if self._already_served(payment_hash, request_id):
            return True
        if not self._consume(payment_hash, quantity, expiry):
            return False
        self._con.execute(
            "INSERT OR IGNORE INTO l402_served (payment_hash, request_id) "
            "VALUES (?, ?)", (payment_hash, request_id))
        self._con.commit()
        return True

    def _consume(self, payment_hash: str, quantity: int, expiry: int) -> bool:
        cur = self._con.execute(
            "UPDATE l402_tokens SET remaining = remaining - 1 "
            "WHERE payment_hash = ? AND remaining > 0", (payment_hash,))
        if cur.rowcount == 0:
            seen = self._con.execute(
                "SELECT 1 FROM l402_tokens WHERE payment_hash = ?",
                (payment_hash,)).fetchone()
            if seen:
                self._con.commit()
                return False  # exhausted
            self._con.execute(
                "INSERT INTO l402_tokens (payment_hash, remaining, expiry) "
                "VALUES (?, ?, ?)", (payment_hash, quantity - 1, expiry))
        self._con.commit()
        return True
