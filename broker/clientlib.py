"""What a front end needs to use a broker's /client API, and nothing else.

The broker's front ends (the web client, the MCP server) are clients of the
broker, not parts of it: they reach it only over HTTP through the public
``/client`` API, exactly as a front end someone else builds would. So this
module imports nothing from the broker package — the one exception allowed
to a front end is ``broker.wire``, the standalone protocol library, for
verifying signed announcements independently. ``tests/test_frontends.py``
enforces that.

Everything here is grounded in the documented API: the L402 challenge
header, the signed payment-challenge body, and the 402's off-protocol
``X-Payment-Hash`` / ``X-Invoice-Amount-Msat`` / ``X-Invoice-Expires``
headers, so no front end ever decodes the broker's private token format.
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets

ALLOWED_SCHEMES = ("http://", "https://")
_CHALLENGE_RE = re.compile(
    r'(?:L402|LSAT)\s+macaroon="([^"]+)",\s*invoice="([^"]+)"')
_PREIMAGE_RE = re.compile(r"^[0-9a-f]{64}$")


def hex64(v) -> bool:
    return isinstance(v, str) and bool(re.fullmatch(r"[0-9a-f]{64}", v))


def as_int(v) -> int:
    return v if isinstance(v, int) and not isinstance(v, bool) else 0


def new_request_id() -> str:
    """A fresh correlation id: 16 hex chars, inside the 1-64 byte limit."""
    return secrets.token_hex(8)


def well_formed_preimage(preimage: str) -> bool:
    return bool(_PREIMAGE_RE.match(preimage or ""))


def preimage_matches(preimage: str, payment_hash: str) -> bool:
    """Whether ``preimage`` (hex) proves payment of ``payment_hash``."""
    if not well_formed_preimage(preimage) or not hex64(payment_hash):
        return False
    return hashlib.sha256(bytes.fromhex(preimage)).hexdigest() \
        == payment_hash


class BrokerAPI:
    """Plain HTTP to one broker. httpx is imported lazily."""

    def __init__(self, broker_url: str, timeout: float = 120.0):
        self.broker_url = broker_url.rstrip("/")
        self.timeout = timeout

    def get(self, path: str):
        import httpx

        resp = httpx.get(self.broker_url + path, timeout=self.timeout)
        return resp.status_code, _headers(resp), resp.content

    def post(self, path: str, body: bytes, headers: dict):
        import httpx

        resp = httpx.post(self.broker_url + path, content=body,
                          headers=headers, timeout=self.timeout)
        return resp.status_code, _headers(resp), resp.content


def _headers(resp) -> dict:
    """Response headers with lower-case names, whatever the server sent."""
    return {k.lower(): v for k, v in resp.headers.items()}


def header(headers: dict, name: str) -> str:
    """A header by name, matched case-insensitively."""
    name = name.lower()
    for k, v in (headers or {}).items():
        if k.lower() == name:
            return v
    return ""


def parse_challenge(headers: dict, body: bytes) -> dict | None:
    """A /client 402 as a front end needs it, or None when it is not a
    usable challenge.

    Returns ``macaroon`` and ``invoice`` (from the L402 header), and from
    the broker's 402 headers ``payment_hash``, ``amount_msat`` and
    ``invoice_expires`` (unix seconds, the end of the settlement window),
    plus ``credential_expires`` from the signed body's ``expiry``.
    """
    m = _CHALLENGE_RE.search(header(headers, "WWW-Authenticate"))
    if not m:
        return None
    macaroon, invoice = m.group(1), m.group(2)
    try:
        signed = json.loads(body)
    except (ValueError, TypeError):
        signed = None
    if not isinstance(signed, dict) or signed.get("invoice") != invoice \
            or signed.get("macaroon") != macaroon:
        return None   # the body is authoritative and must agree
    payment_hash = header(headers, "X-Payment-Hash").strip().lower()
    return {
        "macaroon": macaroon,
        "invoice": invoice,
        "payment_hash": payment_hash if hex64(payment_hash) else None,
        "amount_msat": _int_header(headers, "X-Invoice-Amount-Msat"),
        "invoice_expires": _int_header(headers, "X-Invoice-Expires"),
        "credential_expires": as_int(signed.get("expiry")) or None,
    }


def _int_header(headers: dict, name: str) -> int | None:
    v = header(headers, name).strip()
    return int(v) if v.isdigit() else None


def sanitize_results(results: list[dict]) -> list[dict]:
    """Keep only display-safe fields, and only URLs a browser may follow.

    A result whose URL is not plain http(s) is kept but stripped of its link:
    the indexer's relevance judgment survives, its unfollowable URL does not.
    ``sources`` (which indexer served it, at what rank) passes through a
    whitelist of validated pubkeys and integer ranks.
    """
    out = []
    for r in results:
        if not isinstance(r, dict):
            continue
        url = r.get("url", "")
        entry = {
            "title": r.get("title", "") if isinstance(r.get("title"), str)
            else "",
            "snippet": r.get("snippet", "")
            if isinstance(r.get("snippet"), str) else "",
            "url": url if isinstance(url, str)
            and url.startswith(ALLOWED_SCHEMES) else None,
        }
        sources = [{"pubkey": x["pubkey"], "rank": as_int(x.get("rank"))}
                   for x in (r.get("sources") or [])[:10]
                   if isinstance(x, dict) and hex64(x.get("pubkey"))]
        if sources:
            entry["sources"] = sources
        if "weak" in r:
            entry["weak"] = r.get("weak") is True
        if isinstance(r.get("confidence"), (int, float)):
            entry["confidence"] = float(r["confidence"])
        out.append(entry)
    return out
