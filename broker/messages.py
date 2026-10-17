"""Builders for the messages the broker originates.

Each builder returns an unsigned message dict with the exact Appendix A keys.
Signing (adding ``sig``) is ``Identity.signed``; anonymous query requests are
left unsigned. Every value is validated by ``wire.validate`` before it goes on
the wire, so these builders stay thin.
"""

from __future__ import annotations

import secrets


def new_request_id() -> str:
    """A fresh correlation id: 16 hex chars, well inside the 1-64 byte limit."""
    return secrets.token_hex(8)


def query_request(query: str, max_results: int, pricing_type: str,
                  max_price: int, request_id: str | None = None,
                  pubkey: str | None = None,
                  query_count: int | None = None) -> dict:
    msg = {
        "query": query,
        "max_results": max_results,
        "request_id": request_id or new_request_id(),
        "pricing_type": pricing_type,
        "max_price": max_price,
    }
    if query_count is not None:  # per_batch only: names the batch option
        msg["query_count"] = query_count
    if pubkey is not None:
        msg["pubkey"] = pubkey
    return msg


def payment_challenge(pubkey: str, request_id: str, invoice: str,
                      macaroon: str, expiry: int) -> dict:
    """The body of a 402: duplicates the L402 header under a signature."""
    return {
        "pubkey": pubkey,
        "request_id": request_id,
        "invoice": invoice,
        "macaroon": macaroon,
        "expiry": expiry,
    }


def peer_list_request(pubkey: str, request_id: str | None = None,
                      max_peers: int | None = None) -> dict:
    msg = {"pubkey": pubkey, "request_id": request_id or new_request_id()}
    if max_peers is not None:
        msg["max_peers"] = max_peers
    return msg


def corpus_statistics_request(pubkey: str, statistics_version: int = 0,
                              request_id: str | None = None,
                              max_terms: int | None = None) -> dict:
    msg = {
        "pubkey": pubkey,
        "request_id": request_id or new_request_id(),
        "statistics_version": statistics_version,
    }
    if max_terms is not None:
        msg["max_terms"] = max_terms
    return msg


def error(pubkey: str, code: str, request_id: str | None = None,
          message: str | None = None) -> dict:
    msg = {"pubkey": pubkey, "code": code}
    if request_id is not None:
        msg["request_id"] = request_id
    if message is not None:
        msg["message"] = message
    return msg
