"""Protocol validation and the full accept/reject pipeline.

Everything the specification says about structure and meaning: which fields
each message type carries, their types and ranges, the closed lists (roles,
pricing types), field combinations (pubkey/sig all-or-nothing, query_count
with per_batch only, indexer requires corpus), the request-identifier byte
limit, hex case, the signing policy, and signature verification.

Assumes its input already passed ``parse.parse``. ``ValidationError.code``
carries the conformance rejection code named in the Conformance section.

Two deliberate asymmetries, both from the spec: an announcement's top-level
fields are open (unknown ones are returned for logging and covered by the
signature) while every other shape is closed; and the error ``code`` list is
soft-closed — an unknown code is valid on the wire, treated as ``rejected``.
"""

from __future__ import annotations

import re

from . import bip340
from .canonical import canonical_bytes
from .parse import ParseError, parse

_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_HEX128 = re.compile(r"^[0-9a-f]{128}$")

# The hash-tag prefix is an opaque protocol constant, fixed for the life of
# the protocol (Canonical serialization).
TAG_PREFIX = "ditta/v0/"

ROLES = ("broker", "indexer")
PRICING_TYPES = ("per_query", "per_result", "per_batch")

# Messages whose signature is mandatory (Canonical serialization section).
_MUST_SIGN = frozenset({
    "announcement", "peer_list_request",
    "corpus_statistics_request", "corpus_statistics_response",
    "payment_challenge",
})

_MESSAGE_TYPES = frozenset({
    "announcement", "peer_list_request", "peer_list_response",
    "corpus_statistics_request", "corpus_statistics_response",
    "query_request", "payment_challenge", "result_set", "error",
})


class ValidationError(Exception):
    def __init__(self, code: str, message: str = ""):
        super().__init__(message or code)
        self.code = code


# --- small field helpers ----------------------------------------------------

def _require(obj: dict, key: str):
    if key not in obj:
        raise ValidationError("missing_field", f"required field {key!r} absent")
    return obj[key]


def _as_int(value, key: str) -> int:
    # bool is excluded at the parse layer, so any int here is a real integer.
    if not isinstance(value, int):
        raise ValidationError("wrong_type", f"{key!r} must be an integer")
    return value


def _as_str(value, key: str) -> str:
    if not isinstance(value, str):
        raise ValidationError("wrong_type", f"{key!r} must be a string")
    return value


def _as_list(value, key: str) -> list:
    if not isinstance(value, list):
        raise ValidationError("wrong_type", f"{key!r} must be an array")
    return value


def _as_obj(value, key: str) -> dict:
    if not isinstance(value, dict):
        raise ValidationError("wrong_type", f"{key!r} must be an object")
    return value


def _hex(value, pattern, key: str):
    s = _as_str(value, key)
    if not pattern.match(s):
        raise ValidationError("bad_hex", f"{key!r} is not lowercase hex")
    return s


def _reject_unknown(obj: dict, allowed, where: str):
    for key in obj:
        if key not in allowed:
            raise ValidationError(
                "unknown_field", f"unknown field {key!r} in {where}")


def _require_all(obj: dict, required, where: str):
    """Missing required fields are reported before unknown ones (Conformance:
    the first stated precedence), so closed shapes check presence first."""
    for key in required:
        if key not in obj:
            raise ValidationError(
                "missing_field", f"required field {key!r} absent in {where}")


def _request_id(value) -> str:
    s = _as_str(value, "request_id")
    n = len(s.encode("utf-8"))
    if not 1 <= n <= 64:
        raise ValidationError(
            "bad_length", "request_id must be 1-64 encoded bytes")
    return s


def _verify_signature(message: dict, message_type: str, pubkey_hex: str,
                      sig_hex: str):
    body = {k: v for k, v in message.items() if k != "sig"}
    digest = bip340.tagged_hash(
        TAG_PREFIX + message_type, canonical_bytes(body))
    if not bip340.verify(digest, bytes.fromhex(pubkey_hex),
                         bytes.fromhex(sig_hex)):
        raise ValidationError("bad_signature", "signature does not verify")


# --- announcement -----------------------------------------------------------

_ANNOUNCEMENT_KNOWN = {
    "pubkey", "protocol_version", "endpoint", "timestamp",
    "roles", "pricing", "corpus", "sig",
}
_CORPUS_KEYS = {
    "document_count", "term_count", "updated",
    "languages", "statistics_versions",
}
_PRICING_KEYS = {"type", "price", "query_count"}


def _validate_pricing(pricing: list):
    # Every option is selectable by type and, for per_batch, by count:
    # per_query and per_result at most once, per_batch once per count.
    seen_types = set()
    seen_batch_counts = set()
    for opt in pricing:
        opt = _as_obj(opt, "pricing entry")
        _reject_unknown(opt, _PRICING_KEYS, "pricing option")
        ptype = _as_str(_require(opt, "type"), "type")
        if ptype not in PRICING_TYPES:
            raise ValidationError("bad_enum", f"unknown pricing type {ptype!r}")
        _as_int(_require(opt, "price"), "price")
        if ptype == "per_batch":
            if "query_count" not in opt:
                raise ValidationError(
                    "bad_combination", "per_batch requires query_count")
            qc = _as_int(opt["query_count"], "query_count")
            if qc <= 1:
                raise ValidationError(
                    "out_of_range", "query_count must be greater than one")
            if qc in seen_batch_counts:
                raise ValidationError(
                    "duplicate_value",
                    f"two per_batch options with query_count {qc}")
            seen_batch_counts.add(qc)
        else:
            if "query_count" in opt:
                raise ValidationError(
                    "bad_combination", "query_count only valid for per_batch")
            if ptype in seen_types:
                raise ValidationError(
                    "duplicate_value", f"duplicate pricing type {ptype!r}")
            seen_types.add(ptype)


def _validate_corpus(corpus: dict):
    corpus = _as_obj(corpus, "corpus")
    _reject_unknown(corpus, _CORPUS_KEYS, "corpus summary")
    _as_int(_require(corpus, "document_count"), "document_count")
    _as_int(_require(corpus, "term_count"), "term_count")
    _as_int(_require(corpus, "updated"), "updated")
    for lang in _as_list(_require(corpus, "languages"), "languages"):
        _as_str(lang, "language tag")
    for ver in _as_list(_require(corpus, "statistics_versions"),
                        "statistics_versions"):
        _as_int(ver, "statistics version")


def _validate_announcement(m: dict, *, verify: bool = True):
    unknown = [k for k in m if k not in _ANNOUNCEMENT_KNOWN]

    pubkey = _hex(_require(m, "pubkey"), _HEX64, "pubkey")
    if "sig" not in m:
        raise ValidationError("missing_field", "announcements must be signed")
    sig = _hex(m["sig"], _HEX128, "sig")
    _as_int(_require(m, "protocol_version"), "protocol_version")
    _as_str(_require(m, "endpoint"), "endpoint")
    _as_int(_require(m, "timestamp"), "timestamp")

    roles = _as_list(_require(m, "roles"), "roles")
    if not roles:
        raise ValidationError("out_of_range", "roles must be non-empty")
    seen = set()
    for role in roles:
        role = _as_str(role, "role")
        if role not in ROLES:
            raise ValidationError("bad_enum", f"unknown role {role!r}")
        if role in seen:
            raise ValidationError("duplicate_value", f"duplicate role {role!r}")
        seen.add(role)

    pricing = _as_list(_require(m, "pricing"), "pricing")
    _validate_pricing(pricing)

    is_indexer = "indexer" in seen
    has_corpus = "corpus" in m
    if is_indexer and not has_corpus:
        raise ValidationError("bad_combination", "indexer requires a corpus")
    if has_corpus and not is_indexer:
        raise ValidationError(
            "bad_combination", "corpus present without indexer role")
    if is_indexer and not pricing:
        raise ValidationError(
            "bad_combination", "indexer requires at least one pricing option")
    if pricing and not is_indexer:
        raise ValidationError(
            "bad_combination",
            "a node not declaring indexer carries an empty pricing array")
    if has_corpus:
        _validate_corpus(m["corpus"])

    if verify:
        _verify_signature(m, "announcement", pubkey, sig)
    return unknown


# --- peer list --------------------------------------------------------------

_PEER_LIST_REQUEST_KEYS = {"pubkey", "request_id", "max_peers", "sig"}
_PEER_LIST_RESPONSE_KEYS = {"pubkey", "request_id", "peers", "sig"}


def _optional_positive_int(obj: dict, key: str):
    if key in obj:
        value = _as_int(obj[key], key)
        if value <= 0:
            raise ValidationError("out_of_range", f"{key!r} must exceed zero")


def _validate_peer_list_request(m: dict):
    _require_all(m, ("pubkey", "request_id", "sig"), "peer list request")
    _reject_unknown(m, _PEER_LIST_REQUEST_KEYS, "peer list request")
    pubkey = _hex(m["pubkey"], _HEX64, "pubkey")
    _request_id(m["request_id"])
    _optional_positive_int(m, "max_peers")
    sig = _hex(m["sig"], _HEX128, "sig")
    _verify_signature(m, "peer_list_request", pubkey, sig)


def _validate_peer_list_response(m: dict):
    _require_all(m, ("pubkey", "request_id", "peers"), "peer list response")
    _reject_unknown(m, _PEER_LIST_RESPONSE_KEYS, "peer list response")
    pubkey = _hex(m["pubkey"], _HEX64, "pubkey")
    _request_id(m["request_id"])
    peers = _as_list(m["peers"], "peers")
    for entry in peers:
        _as_obj(entry, "peer entry")
    if "sig" in m:
        sig = _hex(m["sig"], _HEX128, "sig")
        _verify_signature(m, "peer_list_response", pubkey, sig)


def validate_peers(m: dict):
    """Re-validate the announcements embedded in a peer list response.

    A failing embedded announcement is discarded without invalidating the
    rest. Returns (accepted, discarded) lists of announcement dicts.
    """
    accepted, discarded = [], []
    for entry in m.get("peers", []):
        try:
            _validate_announcement(entry, verify=True)
            accepted.append(entry)
        except (ParseError, ValidationError):
            discarded.append(entry)
    return accepted, discarded


# --- corpus statistics ------------------------------------------------------

_CORPUS_REQUEST_KEYS = {
    "pubkey", "request_id", "statistics_version", "max_terms", "sig"}
_CORPUS_RESPONSE_KEYS = {
    "pubkey", "request_id", "statistics_version", "updated", "terms", "sig"}
_TERM_KEYS = {"term", "document_count", "newest"}


def _validate_corpus_statistics_request(m: dict):
    _require_all(m, ("pubkey", "request_id", "statistics_version", "sig"),
                 "corpus statistics request")
    _reject_unknown(m, _CORPUS_REQUEST_KEYS, "corpus statistics request")
    pubkey = _hex(m["pubkey"], _HEX64, "pubkey")
    _request_id(m["request_id"])
    _as_int(m["statistics_version"], "statistics_version")
    _optional_positive_int(m, "max_terms")
    sig = _hex(m["sig"], _HEX128, "sig")
    _verify_signature(m, "corpus_statistics_request", pubkey, sig)


def _validate_corpus_statistics_response(m: dict):
    _require_all(m, ("pubkey", "request_id", "statistics_version",
                     "updated", "terms", "sig"), "corpus statistics response")
    _reject_unknown(m, _CORPUS_RESPONSE_KEYS, "corpus statistics response")
    pubkey = _hex(m["pubkey"], _HEX64, "pubkey")
    _request_id(m["request_id"])
    _as_int(m["statistics_version"], "statistics_version")
    _as_int(m["updated"], "updated")
    for term in _as_list(m["terms"], "terms"):
        term = _as_obj(term, "term entry")
        _require_all(term, _TERM_KEYS, "term entry")
        _reject_unknown(term, _TERM_KEYS, "term entry")
        _as_str(term["term"], "term")
        _as_int(term["document_count"], "document_count")
        _as_int(term["newest"], "newest")
    sig = _hex(m["sig"], _HEX128, "sig")
    _verify_signature(m, "corpus_statistics_response", pubkey, sig)


# --- query request ----------------------------------------------------------

_QUERY_KEYS = {
    "pubkey", "query", "max_results", "request_id",
    "pricing_type", "max_price", "query_count", "sig",
}
_QUERY_REQUIRED = ("query", "max_results", "request_id",
                   "pricing_type", "max_price")


def _validate_query_request(m: dict):
    _require_all(m, _QUERY_REQUIRED, "query request")
    _reject_unknown(m, _QUERY_KEYS, "query request")
    _as_str(m["query"], "query")
    max_results = _as_int(m["max_results"], "max_results")
    if max_results <= 0:
        raise ValidationError("out_of_range", "max_results must exceed zero")
    _request_id(m["request_id"])
    ptype = _as_str(m["pricing_type"], "pricing_type")
    if ptype not in PRICING_TYPES:
        raise ValidationError("bad_enum", f"unknown pricing type {ptype!r}")
    _as_int(m["max_price"], "max_price")

    if ptype == "per_batch":
        if "query_count" not in m:
            raise ValidationError(
                "bad_combination", "a per_batch query names its query_count")
        qc = _as_int(m["query_count"], "query_count")
        if qc <= 1:
            raise ValidationError(
                "out_of_range", "query_count must be greater than one")
    elif "query_count" in m:
        raise ValidationError(
            "bad_combination", "query_count only valid for per_batch")

    has_pubkey = "pubkey" in m
    has_sig = "sig" in m
    if ptype == "per_batch" and not (has_pubkey and has_sig):
        # A credential authorizing more than one query must be bound to a
        # key, so a batch query must be signed (Payment profile).
        raise ValidationError(
            "bad_combination", "a per_batch query must be signed")
    if has_pubkey != has_sig:
        raise ValidationError(
            "bad_combination", "pubkey and sig are all-or-nothing")
    if has_pubkey:
        pubkey = _hex(m["pubkey"], _HEX64, "pubkey")
        sig = _hex(m["sig"], _HEX128, "sig")
        _verify_signature(m, "query_request", pubkey, sig)


# --- payment challenge ------------------------------------------------------

_PAYMENT_CHALLENGE_KEYS = {
    "pubkey", "request_id", "invoice", "macaroon", "expiry", "sig"}
_PAYMENT_CHALLENGE_REQUIRED = (
    "pubkey", "request_id", "invoice", "macaroon", "expiry")


def _validate_payment_challenge(m: dict):
    """The signed body of a 402: issuer authentication for the payer.

    The invoice and macaroon are opaque strings here — their conformance to
    BOLT-11 and macaroon formats is the payment layer's concern; the wire
    checks shape and the signature that binds them to the indexer identity.
    """
    _require_all(m, _PAYMENT_CHALLENGE_REQUIRED, "payment challenge")
    _reject_unknown(m, _PAYMENT_CHALLENGE_KEYS, "payment challenge")
    pubkey = _hex(m["pubkey"], _HEX64, "pubkey")
    _request_id(m["request_id"])
    _as_str(m["invoice"], "invoice")
    _as_str(m["macaroon"], "macaroon")
    _as_int(m["expiry"], "expiry")
    if "sig" not in m:
        raise ValidationError(
            "missing_field", "payment challenges must be signed")
    sig = _hex(m["sig"], _HEX128, "sig")
    _verify_signature(m, "payment_challenge", pubkey, sig)


# --- result set -------------------------------------------------------------

_RESULT_SET_KEYS = {"pubkey", "request_id", "results", "sig"}
_RESULT_KEYS = {"id", "url", "title", "snippet"}


def _validate_result_set(m: dict):
    _require_all(m, ("pubkey", "request_id", "results"), "result set")
    _reject_unknown(m, _RESULT_SET_KEYS, "result set")
    pubkey = _hex(m["pubkey"], _HEX64, "pubkey")
    _request_id(m["request_id"])
    for result in _as_list(m["results"], "results"):
        result = _as_obj(result, "result entry")
        _require_all(result, ("id", "url", "title", "snippet"),
                     "result entry")
        _reject_unknown(result, _RESULT_KEYS, "result entry")
        _as_str(result["id"], "id")
        _as_str(result["url"], "url")
        _as_str(result["title"], "title")
        _as_str(result["snippet"], "snippet")
    if "sig" in m:
        sig = _hex(m["sig"], _HEX128, "sig")
        _verify_signature(m, "result_set", pubkey, sig)


# --- error ------------------------------------------------------------------

_ERROR_KEYS = {"pubkey", "request_id", "code", "message", "sig"}


def _validate_error(m: dict):
    _require_all(m, ("pubkey", "code"), "error")
    _reject_unknown(m, _ERROR_KEYS, "error")
    pubkey = _hex(m["pubkey"], _HEX64, "pubkey")
    if "request_id" in m:
        _request_id(m["request_id"])
    # The code list is closed to senders but soft to receivers: an unknown
    # code is valid on the wire and treated as `rejected`, so no enum check.
    _as_str(m["code"], "code")
    if "message" in m:
        _as_str(m["message"], "message")
    if "sig" in m:
        sig = _hex(m["sig"], _HEX128, "sig")
        _verify_signature(m, "error", pubkey, sig)


# --- dispatch ---------------------------------------------------------------

_VALIDATORS = {
    "peer_list_request": _validate_peer_list_request,
    "peer_list_response": _validate_peer_list_response,
    "corpus_statistics_request": _validate_corpus_statistics_request,
    "corpus_statistics_response": _validate_corpus_statistics_response,
    "query_request": _validate_query_request,
    "payment_challenge": _validate_payment_challenge,
    "result_set": _validate_result_set,
    "error": _validate_error,
}


def validate(m: dict, message_type: str):
    """Validate an already-parsed message of a stated type.

    Returns the list of unknown top-level fields (only ever non-empty for an
    announcement, whose top level is open). Raises ValidationError otherwise.
    """
    if message_type not in _MESSAGE_TYPES:
        raise ValidationError("wrong_type", f"unknown message type {message_type!r}")
    if message_type == "announcement":
        return _validate_announcement(m)
    _VALIDATORS[message_type](m)
    return []


def accept(raw: bytes, message_type: str):
    """Run the whole pipeline on raw bytes: strict parse, then validation.

    The message type is a parameter, never inferred from content: on the wire
    it is known from the endpoint and direction. Returns (message, unknown).
    """
    m = parse(raw)
    unknown = validate(m, message_type)
    return m, unknown
