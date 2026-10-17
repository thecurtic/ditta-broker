"""Strict JSON parsing for the federated search protocol.

Enforces the Encoding-section rules that are invisible after ordinary JSON
parsing: a stock parser silently picks one of two duplicate keys, rounds
2^53 into a float, and accepts ``2.0`` and ``2e3``. By the time a message is
a Python dict the evidence is gone, so these rules are enforced on the raw
bytes using the parser's own hooks plus a pre-scan.

This module is written against the specification alone (protocol.md, revision
cb567bd), not against any other implementation. It knows nothing about field
names, message types, or signatures — it is auditable against the Encoding
section by itself. ``ParseError.code`` carries the conformance rejection code.
"""

from __future__ import annotations

import json

# Reference nesting limit from the Encoding section: 64 open containers.
MAX_DEPTH = 64

# Largest integer every IEEE-754 double represents exactly.
MAX_INT = 9007199254740991  # 2**53 - 1


class ParseError(Exception):
    """A byte- or structure-level violation, before the input is a message."""

    def __init__(self, code: str, message: str = ""):
        super().__init__(message or code)
        self.code = code


def _check_depth(text: str) -> None:
    """Reject nesting deeper than MAX_DEPTH without recursing into it.

    Scans outside of string literals so braces inside strings never count.
    """
    depth = 0
    in_string = False
    escape = False
    for ch in text:
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{" or ch == "[":
            depth += 1
            if depth > MAX_DEPTH:
                raise ParseError("too_deep", "nesting exceeds the depth limit")
        elif ch == "}" or ch == "]":
            depth -= 1


def _parse_int(s: str) -> int:
    # json hands the whole integer token, leading '-' included. The protocol
    # permits no negative integer anywhere (and -0 is rejected outright), and
    # no integer may exceed 2**53 - 1.
    if s[0] == "-":
        raise ParseError("integer_out_of_range",
                         "negative integers (and -0) are not permitted")
    value = int(s)
    if value > MAX_INT:
        raise ParseError("integer_out_of_range",
                         "integer exceeds 2**53 - 1")
    return value


def _parse_float(s: str) -> float:
    # Anything json routes here has a decimal point or an exponent.
    raise ParseError("non_integer_number",
                     "numbers must be integers: no decimal point or exponent")


def _parse_constant(s: str) -> None:
    # NaN / Infinity / -Infinity are not valid JSON under this protocol.
    raise ParseError("not_json", f"invalid literal {s!r}")


def _object_pairs(pairs):
    obj = {}
    for key, value in pairs:
        if key in obj:
            # Key equality is judged after escape processing, which json has
            # already applied, so "a" and "a" collide here.
            raise ParseError("duplicate_key", f"duplicate key {key!r}")
        obj[key] = value
    return obj


def _reject_bool_null_surrogate(value) -> None:
    """Walk the parsed value rejecting bool, null, and unpaired surrogates.

    bool -> wrong_type (no boolean field exists anywhere in the protocol);
    None -> null_value (absence is omission, never an explicit null);
    a string carrying a surrogate code point -> bad_string.
    """
    if isinstance(value, bool):
        raise ParseError("wrong_type", "boolean values are never used")
    if value is None:
        raise ParseError("null_value", "null is not permitted; omit instead")
    if isinstance(value, str):
        for ch in value:
            if 0xD800 <= ord(ch) <= 0xDFFF:
                raise ParseError("bad_string",
                                 "string contains an unpaired surrogate")
        return
    if isinstance(value, dict):
        for k, v in value.items():
            for ch in k:
                if 0xD800 <= ord(ch) <= 0xDFFF:
                    raise ParseError("bad_string",
                                     "key contains an unpaired surrogate")
            _reject_bool_null_surrogate(v)
        return
    if isinstance(value, list):
        for v in value:
            _reject_bool_null_surrogate(v)
        return
    # int and str are the only remaining scalar types, both fine.


def parse(raw: bytes):
    """Parse raw bytes into a JSON object under the protocol's strict rules.

    Returns the top-level object (a dict). Raises ParseError on any violation.
    """
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise ParseError("not_utf8", "bytes are not valid UTF-8")

    _check_depth(text)

    try:
        value = json.loads(
            text,
            parse_int=_parse_int,
            parse_float=_parse_float,
            parse_constant=_parse_constant,
            object_pairs_hook=_object_pairs,
        )
    except ParseError:
        raise
    except json.JSONDecodeError:
        raise ParseError("not_json", "bytes are not a complete JSON text")

    if not isinstance(value, dict):
        raise ParseError("not_object", "the top-level value is not an object")

    _reject_bool_null_surrogate(value)
    return value
