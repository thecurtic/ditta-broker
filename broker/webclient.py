"""The broker's human face: a search page that teaches the L402 flow.

A client of the broker's /client API, not a part of the broker: it reaches
the broker over HTTP only and imports nothing of it but the client library
(``tests/test_frontends.py`` enforces that), so an operator who wants their
own site leaves this off (``webclient.enabled``, default false) and builds
against the same API. The broker starts it when enabled; ``python -m broker
web`` runs it alone. Expose it separately from the broker's protocol face —
e.g. through a Cloudflare tunnel. The payment flow is deliberately visible,
because showing the machinery is the point:

1. As the user types, the button shows a live quote in sats — the broker's
   ``/quote`` (routing evaluated early over cached announcements and
   statistics; no indexer contacted), labelled an estimate, with the age of
   the statistics behind it.
2. Pressing Search POSTs the query. A paid broker answers 402; the page
   shows the invoice as a QR code and a copyable string.
3. The user pays on their phone. The page polls the broker's settlement
   status; the broker minted the invoice on its own node and sees it
   settle, so the moment it does the page proceeds by itself — nothing to
   paste. This is the trusted front end's privilege, granted by the
   operator's ``site_secret`` (any front end given the key gets the same);
   agents on the same client API present the preimage as L402 says. Pasting
   the preimage survives here only as the fallback, folded away under "Paid,
   but nothing happened?", for a site without the secret or a node that has
   not witnessed settlement (yet).
4. The ORIGINAL request bytes are replayed byte-identically with the
   macaroon attached — and the preimage slot empty, or filled from the
   fallback — never rebuilt. The browser holds the bytes; they persist in
   localStorage keyed by payment hash and are cleared when results arrive.
   Unredeemed searches wait in a side list with Resume/Discard, so an
   abandoned (even already-paid) invoice never hijacks a new visit. The
   broker stays stateless, and its
   macaroon is bound to the sha256 of those bytes, so a rebuilt request
   cannot redeem.
5. The broker fans out, pays indexers, merges; the page renders results —
   as the merged list, or as the route map: the fan-out drawn as one
   picture (broker, every indexer, the merged list), where tapping an
   indexer lists the results that indexer returned.
6. Every answered search is kept in this browser's localStorage (last 20)
   and shown in "Recent searches", where entries can be reopened, deleted
   one by one, or cleared. The URL names the shown search by an opaque id
   (``?r=``) — never the query text, which would outlive a deletion in the
   browser's own history — so Back/Forward, reload and session restore
   bring the results back without paying again. A fresh visit reopens the
   last results if they are under 30 minutes old and "New search" was not
   pressed since.

No accounts, no logins, no sessions: payment is the authorization. If the
invoice differs from the quote that was on the button, both numbers and the
difference are shown — a number never silently changes.

SECURITY: every string in a result set is attacker-controlled display
content (spec: Security considerations). The server passes only http/https
URLs through, the page writes to the DOM via textContent only and sets href
after re-checking the scheme, and a restrictive CSP (now with ``img-src
data:`` for the QR, generated server-side by segno from our own broker's
invoice) backstops both.
"""

from __future__ import annotations

import base64
import gzip
import hashlib
import json
import re
import sys
import urllib.parse
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# A client of the broker's /client API, never a part of the broker: only
# the client library may be imported, and nothing here speaks DITTA.
from .clientlib import (ALLOWED_SCHEMES as _ALLOWED_SCHEMES, BrokerAPI,
                        as_int as _as_int, header, hex64 as _hex64,
                        new_request_id, parse_challenge, preimage_matches,
                        sanitize_results, well_formed_preimage)

_MAX_QUERY_BYTES = 1024
_MAX_BODY_BYTES = 65536
_MAX_SCOPE = 64
_REASON_RE = re.compile(r"^[a-z_]{1,32}$")


@dataclass(frozen=True)
class WebClientConfig:
    enabled: bool = False          # off unless the operator turns it on
    listen_host: str = "127.0.0.1"
    listen_port: int = 8420
    broker_url: str = "http://127.0.0.1:8410"
    title: str = "Search"
    max_results: int = 10
    # Must cover the broker's slowest route: fan-out is concurrent, so the
    # bound is one Tor circuit's timeout (60s), not a sum — but a client
    # that gives up before its broker finishes strands a paid redemption.
    timeout: float = 120.0
    # The broker's trusted front end key (receive.site_secret there): it
    # lets THIS site redeem with the preimage slot empty, on the broker's
    # own node's witness of settlement. Any front end the operator gives
    # the key gets the same; unset, the page falls back to the paste.
    site_secret: str = ""


def config_from(section: dict, broker_url: str | None = None
                ) -> WebClientConfig:
    """A WebClientConfig from the ``webclient:`` config section;
    ``broker_url`` is the default when the section names none."""
    raw = dict(section or {})
    if broker_url and not raw.get("broker_url"):
        raw["broker_url"] = broker_url
    return WebClientConfig(**raw)


def _clean_pubkeys(v) -> list[str] | None:
    """A selection as the broker wants it: hex pubkeys, deduplicated, in
    order. None when malformed — the page sent something it never should,
    and a scope is never silently dropped to the full network."""
    if v is None or v == "" or v == []:
        return []
    if isinstance(v, str):
        v = [p for p in v.split(",") if p]
    if not isinstance(v, list) or len(v) > _MAX_SCOPE \
            or not all(_hex64(p) for p in v):
        return None
    return list(dict.fromkeys(v))


def _sanitize_scope(scope) -> dict | None:
    """Whitelist the broker's effective-scope report: pubkeys and short
    machine reason codes only."""
    if not isinstance(scope, dict):
        return None
    keys = lambda k: [p for p in (scope.get(k) or [])[:_MAX_SCOPE]
                      if _hex64(p)]
    return {
        "selected": keys("selected"),
        "queried": keys("queried"),
        "failed": keys("failed"),
        "skipped": [{"pubkey": x["pubkey"],
                     "reason": x["reason"]
                     if isinstance(x.get("reason"), str)
                     and _REASON_RE.match(x["reason"]) else "other"}
                    for x in (scope.get("skipped") or [])[:_MAX_SCOPE]
                    if isinstance(x, dict) and _hex64(x.get("pubkey"))],
    }


def _scope_error(data: dict) -> dict | None:
    """The broker's machine-readable scope refusals, passed on intact."""
    code = data.get("error")
    if code == "unknown_pubkey":
        return {"error": "unknown_pubkey",
                "pubkeys": [p for p in (data.get("pubkeys") or [])
                            [:_MAX_SCOPE] if _hex64(p)]}
    if code == "empty_scope":
        return {"error": "empty_scope",
                "skipped": (_sanitize_scope(
                    {"skipped": data.get("skipped")}) or {}).get(
                    "skipped", [])}
    return None


def _clean_ask(query, n, max_msat, exclude, cfg,
               pubkeys=None) -> dict | None:
    if not isinstance(query, str) or not query.strip() \
            or len(query.encode()) > _MAX_QUERY_BYTES:
        return None
    try:
        n = max(1, min(int(n), 50))   # cfg.max_results is only the default
    except (TypeError, ValueError):
        n = cfg.max_results
    ask = {"query": query.strip(), "n": n, "exclude_tor": bool(exclude)}
    try:
        if max_msat is not None and str(max_msat).strip() != "":
            ask["max_msat"] = max(0, int(max_msat))
    except (TypeError, ValueError):
        pass
    pubkeys = _clean_pubkeys(pubkeys)
    if pubkeys is None:
        return None
    if pubkeys:
        ask["pubkeys"] = pubkeys
    return ask


def _ask_from_params(params: dict, cfg) -> dict | None:
    get = lambda k, d=None: (params.get(k) or [d])[0]
    return _clean_ask(get("q"), get("n", cfg.max_results), get("max_msat"),
                      str(get("exclude_tor", "")).lower() in ("1", "true"),
                      cfg, get("pubkeys"))


def _ask_from_json(data: dict, cfg) -> dict | None:
    return _clean_ask(data.get("q"), data.get("n", cfg.max_results),
                      data.get("max_msat"),
                      data.get("exclude_onion") is True
                      or data.get("exclude_tor") is True, cfg,
                      data.get("pubkeys"))


_STAGES = ("considered", "routed", "paid", "responded", "failed")


def _sanitize_outcome(o: dict) -> dict:
    """Whitelist the broker's outcome record before it reaches the page."""
    ask = o.get("ask") if isinstance(o.get("ask"), dict) else {}
    out = {
        "request_id": str(o.get("request_id", ""))[:64],
        "query": str(o.get("query", ""))[:1024],
        "quote_msat": _as_int(o.get("quote_msat")),
        "charged_msat": _as_int(o.get("charged_msat")),
        "cost_msat": _as_int(o.get("cost_msat")),
        "started": _as_int(o.get("started")),
        "finished": _as_int(o.get("finished")),
        "ask": {
            "n": _as_int(ask.get("n")),
            "exclude_tor": ask.get("exclude_tor") is True,
            **({"max_msat": _as_int(ask.get("max_msat"))}
               if ask.get("max_msat") is not None else {}),
        },
        "indexers": [],
    }
    for x in (o.get("indexers") or [])[:20]:
        if not isinstance(x, dict) or not _hex64(x.get("pubkey")):
            continue
        entry = {
            "pubkey": x["pubkey"],
            "transport": "tor" if x.get("transport") == "tor"
            else "clearnet",
            "stage": x.get("stage")
            if x.get("stage") in _STAGES else "considered",
            "price_msat": _as_int(x.get("price_msat")),
            "paid_msat": _as_int(x.get("paid_msat")),
            "coverage": float(x["coverage"])
            if isinstance(x.get("coverage"), (int, float)) else 0.0,
        }
        for key in ("latency_ms", "result_count", "survived"):
            if isinstance(x.get(key), int):
                entry[key] = x[key]
        for key in ("dropped", "error"):
            if isinstance(x.get(key), str):
                entry[key] = x[key][:300]
        out["indexers"].append(entry)
    raw = o.get("raw")
    if isinstance(raw, dict):
        out["raw"] = {pk: sanitize_results(rs)
                      for pk, rs in list(raw.items())[:20]
                      if _hex64(pk) and isinstance(rs, list)}
    return out


def qr_data_uri(text: str) -> str:
    import segno

    # Uppercase bech32 QR-encodes in the compact alphanumeric mode.
    return segno.make(text.upper()).svg_data_uri(scale=4)


class WebClient:
    """The API half, separated from HTTP plumbing so tests can call it."""

    def __init__(self, cfg: WebClientConfig):
        self.cfg = cfg
        self._broker = BrokerAPI(cfg.broker_url, timeout=cfg.timeout)

    def _post_broker(self, path: str, body: bytes, headers: dict):
        return self._broker.post(path, body, headers)

    def _get_broker(self, path: str):
        return self._broker.get(path)

    # -- quote -------------------------------------------------------------

    def quote(self, ask: dict) -> dict:
        params = {"query": ask.get("query", ""), "n": ask.get("n", 10)}
        if ask.get("max_msat") is not None:
            params["max_msat"] = ask["max_msat"]
        if ask.get("exclude_tor"):
            params["exclude_tor"] = "1"
        if ask.get("pubkeys"):
            params["pubkeys"] = ",".join(ask["pubkeys"])
        status, _h, body = self._get_broker(
            "/client/quote?" + urllib.parse.urlencode(params))
        try:
            data = json.loads(body)
        except ValueError:
            return {"error": "broker returned an unreadable quote"}
        if not isinstance(data, dict):
            return {"error": "broker returned an unreadable quote"}
        scope_error = _scope_error(data)
        if scope_error:
            return scope_error
        if data.get("error") == "unsatisfiable":
            return {"error": "unsatisfiable",
                    "reason": str(data.get("reason", ""))[:300],
                    "pubkeys": [p for p in (data.get("pubkeys") or [])
                                [:_MAX_SCOPE] if _hex64(p)]}
        if status != 200:
            return {"error": str(data.get("message",
                                          data.get("error", "refused")))}
        return {"quote_msat": _as_int(data.get("quote_msat")),
                "indexers_considered": _as_int(
                    data.get("indexers_considered")),
                "scope": _sanitize_scope(data.get("scope")),
                "stats_age_s": (data["stats_age_s"]
                                if isinstance(data.get("stats_age_s"), int)
                                else None)}

    # -- peers (the cards) --------------------------------------------------

    def peers(self) -> dict:
        """The indexer cards — re-shaped through a whitelist, never passed.

        Term strings, endpoints, and language tags are indexer-controlled
        display content, rendered (like result fields) via textContent
        only. Pubkeys are validated to hex so the page may use them as
        element keys.
        """
        status, _h, body = self._get_broker("/client/peers")
        try:
            data = json.loads(body)
        except ValueError:
            data = None
        if status != 200 or not isinstance(data, dict):
            return {"error": "broker did not return the peer view"}
        out = []
        for x in (data.get("indexers") or [])[:20]:
            if not isinstance(x, dict) or not _hex64(x.get("pubkey")):
                continue
            corpus = x.get("corpus") or {}
            out.append({
                "pubkey": x["pubkey"],
                "name": str(x.get("name") or "")[:60] or None,
                "covers": str(x.get("covers") or "")[:600] or None,
                "searchable": x.get("searchable") is not False,
                **({"unsearchable_reason": x["unsearchable_reason"]}
                   if isinstance(x.get("unsearchable_reason"), str)
                   and _REASON_RE.match(x["unsearchable_reason"])
                   else {}),
                "endpoint": str(x.get("endpoint", ""))[:200],
                "transport": ("tor" if x.get("transport") == "tor"
                              else "clearnet"),
                "corpus": {
                    "document_count": _as_int(corpus.get("document_count")),
                    "term_count": _as_int(corpus.get("term_count")),
                    "languages": [lang for lang
                                  in (corpus.get("languages") or [])[:8]
                                  if isinstance(lang, str)],
                },
                "pricing": [
                    {"type": o["type"], "price": _as_int(o.get("price"))}
                    for o in (x.get("pricing") or [])[:4]
                    if isinstance(o, dict) and isinstance(o.get("type"), str)],
                "vocabulary": [
                    {"term": v["term"][:64],
                     "pct": float(v["pct"])
                     if isinstance(v.get("pct"), (int, float)) else 0.0,
                     "lift": float(v["lift"])
                     if isinstance(v.get("lift"), (int, float)) else 0.0,
                     **({"evidence": v["evidence"]}
                        if v.get("evidence") in ("delivered", "failed")
                        else {})}
                    for v in (x.get("vocabulary") or [])[:20]
                    if isinstance(v, dict) and isinstance(v.get("term"), str)],
                "reach": ({
                    "status": x["reach"].get("status")
                    if x["reach"].get("status") in
                    ("reachable", "degraded", "unreachable", "never")
                    else "never",
                    "last_ok": x["reach"]["last_ok"]
                    if isinstance(x["reach"].get("last_ok"), int) else None,
                    "last_error": str(x["reach"].get("last_error") or "")[:200]
                    or None,
                    "last_error_at": x["reach"]["last_error_at"]
                    if isinstance(x["reach"].get("last_error_at"), int)
                    else None,
                } if isinstance(x.get("reach"), dict) else None),
                "naming": ({
                    "name": str(x["naming"].get("name") or "")[:60] or None,
                    "description": str(
                        x["naming"].get("description") or "")[:600] or None,
                    "assessment": str(
                        x["naming"].get("assessment") or "")[:500] or None,
                    "status": x["naming"].get("status")
                    if x["naming"].get("status") in ("ok", "held", "pending")
                    else None,
                    "last_error": str(
                        x["naming"].get("last_error") or "")[:300] or None,
                    "evidence_base": ({
                        "reported_documents": _as_int(
                            x["naming"]["evidence_base"].get(
                                "reported_documents")),
                        "observed_results": _as_int(
                            x["naming"]["evidence_base"].get(
                                "observed_results")),
                        "queries_routed": _as_int(
                            x["naming"]["evidence_base"].get(
                                "queries_routed")),
                    } if isinstance(x["naming"].get("evidence_base"), dict)
                       else None),
                    "history": [
                        {"name": str(h.get("name", ""))[:60],
                         "from": _as_int(h.get("from")),
                         "to": _as_int(h.get("to")) or None}
                        for h in (x["naming"].get("history") or [])[:10]
                        if isinstance(h, dict)],
                } if isinstance(x.get("naming"), dict) else None),
                "last_ingest": _as_int(x.get("last_ingest")),
                "stats_age_s": (x["stats_age_s"]
                                if isinstance(x.get("stats_age_s"), int)
                                else None),
            })
        return {"indexers": out, "count": len(out)}

    def announcements(self) -> dict:
        """The explainer's live panel: the broker's own identity and the
        announcements it holds, shown unchanged. The broker verified each
        one on arrival; to the page they are data to display."""
        status, _h, body = self._get_broker("/client/announcements")
        try:
            data = json.loads(body) if status == 200 else None
        except ValueError:
            data = None
        if not isinstance(data, dict):
            return {"error": "broker did not return its announcements"}
        broker = data.get("broker") if isinstance(data.get("broker"), dict) \
            else {}
        endpoint = broker.get("endpoint")
        out = []
        for a in (data.get("announcements") or [])[:20]:
            if isinstance(a, dict) and _hex64(a.get("pubkey")):
                out.append(a)
        return {
            "broker": {
                "pubkey": broker.get("pubkey")
                if _hex64(broker.get("pubkey")) else None,
                "endpoint": endpoint if isinstance(endpoint, str)
                and endpoint.startswith(_ALLOWED_SCHEMES) else None,
            },
            "announcements": out,
        }

    def pending_status(self, payment_hash: str) -> dict:
        """Has the broker seen this invoice settle (and prefetched)?"""
        if not _hex64(payment_hash):
            return {"settled": False, "ready": False}
        status, _h, body = self._get_broker(
            f"/client/status?hash={payment_hash}")
        try:
            data = json.loads(body) if status == 200 else {}
        except ValueError:
            data = {}
        return {"settled": data.get("settled") is True,
                "ready": data.get("ready") is True}

    def beacon(self, request_id: str, url: str) -> bool:
        if not request_id or not url:
            return False
        status, _h, body = self._post_broker(
            "/client/beacon",
            json.dumps({"request_id": request_id,
                        "url": url}).encode(),
            {"Content-Type": "application/json"})
        try:
            return status == 200 and json.loads(body).get("ok") is True
        except ValueError:
            return False

    # -- search (step 2: original envelope, 402 surfaced) -------------------

    def search(self, ask: dict, quote_msat: int) -> dict:
        envelope = {"query": ask["query"], "request_id": new_request_id(),
                    "n": ask.get("n", 10)}
        if ask.get("max_msat") is not None:
            envelope["max_msat"] = ask["max_msat"]
        if ask.get("exclude_tor"):
            envelope["exclude_tor"] = True
        if ask.get("pubkeys"):
            envelope["pubkeys"] = list(ask["pubkeys"])
        request_bytes = json.dumps(
            envelope, separators=(",", ":")).encode()
        status, headers, body = self._post_broker(
            "/client/search", request_bytes,
            {"Content-Type": "application/json"})
        if status == 402:
            payload = self._challenge_payload(headers, body, request_bytes,
                                              quote_msat)
            payload["ask"] = envelope
            return payload
        return self._final(status, body, envelope["request_id"], headers)

    # -- complete (step 4: byte-identical replay) --------------------------

    def complete(self, request_b64: str, macaroon: str, preimage: str,
                 quote_msat: int, payment_hash: str = "",
                 request_sha256: str = "") -> dict:
        """Replay the held bytes. ``preimage`` empty means: redeem on the
        broker's own witness of settlement (checked first, so an unpaid
        invoice is never replaced by a fresh challenge); non-empty is the
        fallback paste, verified locally against the payment hash.

        ``payment_hash`` and ``request_sha256`` are what the 402 told the
        page when the search was first sent; the replay is checked against
        both, so a damaged pending record never spends a paid credential's
        retry on the wrong bytes."""
        try:
            request_bytes = base64.b64decode(request_b64, validate=True)
        except Exception:
            return {"error": "pending request bytes are corrupted"}
        if len(request_bytes) > _MAX_BODY_BYTES:
            return {"error": "pending request is implausibly large"}
        try:
            envelope = json.loads(request_bytes)
            assert isinstance(envelope, dict)
            request_id = envelope["request_id"]
            assert isinstance(request_id, str) and request_id
        except Exception:
            return {"error": "pending bytes are not a valid search"}
        if not _hex64(payment_hash) or not _hex64(request_sha256) \
                or not macaroon:
            return {"error": "the pending payment is incomplete — start "
                             "the search again"}
        if hashlib.sha256(request_bytes).hexdigest() != request_sha256:
            return {"error": "this payment was not issued for the pending "
                             "request — the bytes must replay unchanged"}

        preimage = (preimage or "").strip().lower()
        if preimage and not well_formed_preimage(preimage):
            return {"error": "the preimage must be 64 hex characters — copy "
                             "it from the payment details in your wallet"}
        if preimage:
            if not preimage_matches(preimage, payment_hash):
                return {"error": "that preimage does not match this "
                                 "invoice's payment hash — check you copied "
                                 "the preimage (not the payment hash) of "
                                 "the right payment"}
        elif not self.cfg.site_secret:
            return {"error": "this site has no site_secret shared with its "
                             "broker, so it cannot collect on the broker's "
                             "word — paste the preimage from your wallet"}
        elif not self.pending_status(payment_hash)["settled"]:
            # Replaying an unpaid token would earn a fresh 402 and replace
            # the invoice the person may be paying this second.
            return {"unsettled": True,
                    "note": "the broker has not seen this payment yet — "
                            "results appear by themselves once it does"}

        replay_headers = {"Content-Type": "application/json",
                          "Authorization": f"L402 {macaroon}:{preimage}"}
        if not preimage:
            replay_headers["X-Site-Secret"] = self.cfg.site_secret
        status, headers, body = self._post_broker(
            "/client/search", request_bytes, replay_headers)
        if status == 402:
            # The token did not redeem (expired or spent): a fresh challenge,
            # never a dead end.
            payload = self._challenge_payload(headers, body, request_bytes,
                                              quote_msat)
            payload["ask"] = envelope
            payload["note"] = ("the previous token did not redeem — a fresh "
                               "invoice was issued")
            return payload
        return self._final(status, body, request_id, headers)

    # -- shared ------------------------------------------------------------

    def _challenge_payload(self, headers: dict, body: bytes,
                           request_bytes: bytes, quote_msat: int) -> dict:
        ch = parse_challenge(body)
        if ch is None:
            return {"error": "402 without a usable payment challenge"}
        return {
            "payment_required": True,
            "invoice": ch["invoice"],
            "macaroon": ch["macaroon"],
            "payment_hash": ch["payment_hash"],
            "amount_msat": ch["amount_msat"],
            "invoice_expires": ch["invoice_expires"],
            "quote_msat": quote_msat,
            "request_b64": base64.b64encode(request_bytes).decode(),
            "request_sha256": hashlib.sha256(request_bytes).hexdigest(),
            "qr": qr_data_uri(ch["invoice"]),
        }

    def _final(self, status: int, body: bytes, request_id: str,
               headers: dict | None = None) -> dict:
        try:
            data = json.loads(body)
            assert isinstance(data, dict)
        except Exception:
            return {"error": f"broker returned an unreadable response "
                             f"(HTTP {status})"}
        if status != 200:
            scope_error = _scope_error(data)
            if scope_error:
                return scope_error
            if data.get("error") == "unsatisfiable":
                return {"price_exceeded": True,
                        "detail": str(data.get("message", ""))[:300],
                        "pubkeys": [p for p in (data.get("pubkeys") or [])
                                    [:_MAX_SCOPE] if _hex64(p)]}
            return {"error": str(data.get("message",
                                          data.get("error",
                                                   f"HTTP {status}")))[:300]}
        if data.get("request_id") != request_id:
            return {"error": "broker echoed the wrong request id"}
        out = {"request_id": request_id,
               "results": sanitize_results(data.get("results") or []),
               "scope": _sanitize_scope(data.get("scope")),
               "charged_msat": _as_int(data.get("charged_msat"))}
        outcome = data.get("outcome")
        if isinstance(outcome, dict):
            out["outcome"] = _sanitize_outcome(outcome)
        retained = header(headers or {}, "X-Credential-Retained")
        if retained == "1":
            # No indexer answered, so the broker did NOT consume the
            # credential — the pending payment must survive for a retry.
            out["credential_retained"] = True
        return out


_WEB = Path(__file__).parent / "web"
_FONTS = (("IBM Plex Sans", 400, "ibm-plex-sans-latin-400-normal.woff2"),
          ("IBM Plex Sans", 500, "ibm-plex-sans-latin-500-normal.woff2"),
          ("IBM Plex Sans", 600, "ibm-plex-sans-latin-600-normal.woff2"),
          ("IBM Plex Mono", 400, "ibm-plex-mono-latin-400-normal.woff2"))
def _fonts() -> str:
    return "".join(
        f"@font-face{{font-family:'{family}';font-style:normal;"
        f"font-weight:{weight};font-display:swap;src:url(data:font/woff2;"
        f"base64,{base64.b64encode((_WEB / 'fonts' / name).read_bytes()).decode()}"
        f") format('woff2')}}\n"
        for family, weight, name in _FONTS)


def _fill(template: str, title: str, slots) -> bytes:
    page = (_WEB / template).read_text().replace("__TITLE__",
                                                 _html_escape(title))
    for slot, content in slots:
        if page.count(slot) != 1:
            raise RuntimeError(f"{template}: expected one {slot}")
        page = page.replace(slot, content)
    return page.encode()


def build_page(title: str) -> bytes:
    """Assemble the search page from the real files under ``web/``.

    A single response with no build step: the Plex fonts are inlined as
    base64 woff2 (``font-src data:``), and our CSS and JS go in as they
    are, the figures (``figures.js``, our own labeled diagrams) ahead of
    the page script. Done once at startup, never per request.
    """
    return _fill("page.html", title, (
        ("/*@@STYLE@@*/", _fonts() + (_WEB / "page.css").read_text()),
        ("/*@@SCRIPT@@*/", (_WEB / "figures.js").read_text() + "\n"
         + (_WEB / "page.js").read_text())))


def build_protocol_page(title: str) -> bytes:
    """The ``/protocol`` explainer, assembled the same way from the same
    figures, with its own CSS and script."""
    return _fill("protocol.html", title, (
        ("/*@@STYLE@@*/", _fonts() + (_WEB / "page.css").read_text()
         + (_WEB / "protocol.css").read_text()),
        ("/*@@SCRIPT@@*/", (_WEB / "figures.js").read_text() + "\n"
         + (_WEB / "protocol.js").read_text())))


class WebClientServer:
    def __init__(self, cfg: WebClientConfig):
        self.cfg = cfg
        self.api = WebClient(cfg)
        page = build_page(cfg.title)
        page_gz = gzip.compress(page, 9)   # once, not per request
        protocol = build_protocol_page(cfg.title)
        protocol_gz = gzip.compress(protocol, 9)
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, fmt, *args):
                pass

            def _send(self, status, body: bytes, content_type: str,
                      gzipped: bytes | None = None):
                # Compress here rather than trusting whatever proxy sits in
                # front: the page carries inlined fonts and figures.
                accepts = "gzip" in (self.headers.get("Accept-Encoding")
                                     or "").lower()
                if accepts and gzipped is None and len(body) > 1024:
                    gzipped = gzip.compress(body, 6)
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                if accepts and gzipped is not None:
                    body = gzipped
                    self.send_header("Content-Encoding", "gzip")
                self.send_header("Vary", "Accept-Encoding")
                self.send_header("Content-Length", str(len(body)))
                self.send_header(
                    "Content-Security-Policy",
                    "default-src 'none'; style-src 'unsafe-inline'; "
                    "script-src 'unsafe-inline'; connect-src 'self'; "
                    "img-src data:; font-src data:; base-uri 'none'; "
                    "form-action 'self'")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Referrer-Policy", "no-referrer")
                self.end_headers()
                self.wfile.write(body)

            def _json(self, data: dict):
                self._send(200, json.dumps(data).encode(), "application/json")

            def _read_json(self):
                length = int(self.headers.get("Content-Length", 0))
                if length > _MAX_BODY_BYTES:
                    return None
                try:
                    return json.loads(self.rfile.read(length))
                except (ValueError, TypeError):
                    return None

            def do_GET(self):
                parsed = urllib.parse.urlparse(self.path)
                if parsed.path == "/":
                    self._send(200, page, "text/html; charset=utf-8",
                               page_gz)
                    return
                if parsed.path == "/protocol":
                    self._send(200, protocol, "text/html; charset=utf-8",
                               protocol_gz)
                    return
                if parsed.path == "/api/quote":
                    params = urllib.parse.parse_qs(parsed.query)
                    if _clean_pubkeys(
                            (params.get("pubkeys") or [None])[0]) is None:
                        self._json({"error": "malformed_scope"})
                        return
                    ask = _ask_from_params(params, outer.cfg)
                    if ask is None:
                        self._json({"error": "empty or oversized query"})
                        return
                    try:
                        self._json(outer.api.quote(ask))
                    except Exception as e:
                        self._json({"error": f"broker unreachable: {e}"})
                    return
                if parsed.path == "/api/pending-status":
                    params = urllib.parse.parse_qs(parsed.query)
                    payment_hash = (params.get("hash") or [""])[0]
                    try:
                        self._json(outer.api.pending_status(payment_hash))
                    except Exception:
                        self._json({"settled": False, "ready": False})
                    return
                if parsed.path == "/api/announcements":
                    try:
                        self._json(outer.api.announcements())
                    except Exception as e:
                        self._json({"error": f"broker unreachable: {e}"})
                    return
                if parsed.path == "/api/peers":
                    try:
                        self._json(outer.api.peers())
                    except Exception as e:
                        self._json({"error": f"broker unreachable: {e}"})
                    return
                self._send(404, b"not found", "text/plain")

            def do_POST(self):
                parsed = urllib.parse.urlparse(self.path)
                data = self._read_json()
                if data is None:
                    self._json({"error": "malformed request"})
                    return
                try:
                    if parsed.path == "/api/search":
                        if _clean_pubkeys(data.get("pubkeys")) is None:
                            self._json({"error": "malformed_scope"})
                            return
                        ask = _ask_from_json(data, outer.cfg)
                        if ask is None:
                            self._json({"results": []})
                            return
                        quote_msat = data.get("quote_msat")
                        if not isinstance(quote_msat, int) or quote_msat < 0:
                            quote_msat = 0
                        self._json(outer.api.search(ask, quote_msat))
                        return
                    if parsed.path == "/api/beacon":
                        # Aggregate interaction only: request_id + url.
                        # Nothing identifies the person; nothing persists
                        # client-side. Fire-and-forget by design.
                        try:
                            self._json({"ok": outer.api.beacon(
                                str(data.get("request_id") or "")[:64],
                                str(data.get("url") or "")[:2048])})
                        except Exception:
                            self._json({"ok": False})
                        return
                    if parsed.path == "/api/complete":
                        quote_msat = data.get("quote_msat")
                        if not isinstance(quote_msat, int) or quote_msat < 0:
                            quote_msat = 0
                        self._json(outer.api.complete(
                            str(data.get("request_b64") or ""),
                            str(data.get("macaroon") or ""),
                            str(data.get("preimage") or ""),
                            quote_msat,
                            str(data.get("payment_hash") or ""),
                            str(data.get("request_sha256") or "")))
                        return
                except Exception as e:
                    self._json({"error": f"broker unreachable: {e}"})
                    return
                self._send(404, b"not found", "text/plain")

        self._httpd = ThreadingHTTPServer(
            (cfg.listen_host, cfg.listen_port), Handler)

    @property
    def port(self) -> int:
        return self._httpd.server_address[1]

    def serve_forever(self):
        self._httpd.serve_forever()

    def start_background(self):
        import threading

        thread = threading.Thread(target=self.serve_forever, daemon=True)
        thread.start()
        return thread

    def shutdown(self):
        self._httpd.shutdown()


def _html_escape(s: str) -> str:
    return (s.replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


if __name__ == "__main__":
    # Kept for existing service files: the same as ``python -m broker web``.
    from .__main__ import main as _main

    sys.exit(_main(["web", *sys.argv[1:]]))
