"""Independent implementation of the federated search protocol wire format.

Built against the specification (see ``spec/protocol.md``, revision e5aa7f8)
and its conformance vectors alone — deliberately not against any other
implementation of the protocol. The broker imports no other implementation;
this package is the whole of the broker's protocol layer.

Pipeline: parse (strict bytes -> object) -> validate (conformant message of a
stated type, signatures verified) using canonical serialization and BIP-340.
"""

from . import bip340
from .canonical import canonical_bytes
from .parse import ParseError, parse
from .validate import (
    ValidationError,
    accept,
    validate,
    validate_peers,
)

__all__ = [
    "bip340",
    "canonical_bytes",
    "ParseError",
    "parse",
    "ValidationError",
    "accept",
    "validate",
    "validate_peers",
]
