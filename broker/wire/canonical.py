"""Canonical serialization: one and only one byte sequence per message.

Implements rules 2-10 of the protocol's Canonical serialization section.
Rule 1 — excluding the signature field — is the caller's job, because the
serializer should not need to know which field is special.

The property every signature depends on: two conformant implementations given
the same message produce identical bytes. Written directly against the spec
text rather than delegating to an off-the-shelf JSON canonicalizer, because
RFC 8785 (JCS) sorts object keys by UTF-16 code units while this protocol
sorts by UTF-8 byte value, and the two disagree for supplementary-plane keys.
"""

from __future__ import annotations

MAX_INT = 9007199254740991

# Two-character escapes mandated by rule 8 for these control characters.
_SHORT_ESCAPE = {
    0x08: "\\b",
    0x09: "\\t",
    0x0A: "\\n",
    0x0C: "\\f",
    0x0D: "\\r",
}


def _escape_string(s: str) -> str:
    out = ['"']
    for ch in s:
        code = ord(ch)
        if ch == '"':
            out.append('\\"')
        elif ch == "\\":
            out.append("\\\\")
        elif code <= 0x1F:
            out.append(_SHORT_ESCAPE.get(code, f"\\u{code:04x}"))
        else:
            # Rule 9: '/' is not escaped. Rule 10: non-ASCII stays literal.
            out.append(ch)
    out.append('"')
    return "".join(out)


def _serialize(value) -> str:
    if isinstance(value, bool):
        raise ValueError("boolean is not representable in this protocol")
    if value is None:
        raise ValueError("null is not representable in this protocol")
    if isinstance(value, int):
        if value < 0 or value > MAX_INT:
            raise ValueError(f"integer out of range: {value}")
        return str(value)
    if isinstance(value, str):
        return _escape_string(value)
    if isinstance(value, dict):
        items = sorted(value.items(), key=lambda kv: kv[0].encode("utf-8"))
        body = ",".join(
            f"{_escape_string(k)}:{_serialize(v)}" for k, v in items)
        return "{" + body + "}"
    if isinstance(value, list):
        return "[" + ",".join(_serialize(v) for v in value) + "]"
    raise ValueError(f"cannot serialize {type(value).__name__}")


def canonical_bytes(message: dict) -> bytes:
    """Canonical UTF-8 byte serialization of a message.

    The caller has already removed the ``sig`` field (rule 1) if excluding it.
    """
    return _serialize(message).encode("utf-8")
