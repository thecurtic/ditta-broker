"""Conformance harness for the broker's independent wire implementation.

Runs every vendored vector (``spec/vectors.json``) through the real pipeline
in both directions:

  every valid vector MUST be accepted, and its canonical bytes, tagged hash,
  and signature MUST re-derive exactly;

  every invalid vector MUST be rejected, and rejected with the specific code
  its metadata declares — a wrong-reason rejection still fails.

This is the executable meaning of "conformant" for everything the wire format
can express, and the independent cross-check the spec's own architecture notes
call the single highest-value remaining step. Behavioural rules — payment
flow, supersession, relay policy — need a running node and live elsewhere.
"""

import json
from pathlib import Path

import pytest

from broker.wire import bip340
from broker.wire.canonical import canonical_bytes
from broker.wire.parse import ParseError
from broker.wire.validate import (TAG_PREFIX, ValidationError, accept,
                                  validate_peers)

VECTORS = Path(__file__).resolve().parents[1] / "spec" / "vectors.json"
_data = json.loads(VECTORS.read_text(encoding="utf-8"))


@pytest.mark.parametrize("v", _data["valid"], ids=lambda v: v["name"])
def test_valid_vector_accepted_and_rederived(v):
    # Re-emit in the stored (non-canonical) key order: the parser must accept
    # any order, the serializer must impose one.
    raw = json.dumps(v["message"], ensure_ascii=False).encode("utf-8")
    m, _unknown = accept(raw, v["message_type"])

    body = {k: x for k, x in m.items() if k != "sig"}
    cb = canonical_bytes(body)
    assert cb.decode("utf-8") == v["canonical"], "canonical bytes differ"

    tagged = bip340.tagged_hash(TAG_PREFIX + v["message_type"], cb)
    assert tagged.hex() == v["sha256"], "tagged hash differs"

    assert v["signed"] == ("sig" in m), "signed flag disagrees with message"

    if v["message_type"] == "peer_list_response":
        accepted, discarded = validate_peers(m)
        assert not discarded and len(accepted) == len(m["peers"]), \
            "embedded announcement wrongly discarded"


@pytest.mark.parametrize("v", _data["invalid"], ids=lambda v: v["name"])
def test_invalid_vector_rejected_with_declared_code(v):
    if v.get("encoding") == "hex":
        raw = bytes.fromhex(v["input"])
    else:
        raw = v["input"].encode("utf-8")
    with pytest.raises((ParseError, ValidationError)) as exc:
        accept(raw, v["message_type"])
    expect = v["expect"] if isinstance(v["expect"], list) else [v["expect"]]
    assert exc.value.code in expect, (
        f"rejected with {exc.value.code!r}, expected one of {expect!r}")


def test_pure_python_and_libsecp_paths_agree():
    """When coincurve is installed the hot path is libsecp256k1; the pure
    implementation stays the reference. Same key, message, and aux MUST
    yield byte-identical signatures, and each path verifies the other's."""
    from broker.wire.bip340 import _sign_pure, _verify_pure

    for name, k in _data["keys"].items():
        seckey = bytes.fromhex(k["seckey"])
        msg = bip340.tagged_hash(TAG_PREFIX + "test", name.encode())
        fast, pure = bip340.sign(msg, seckey), _sign_pure(msg, seckey)
        assert fast == pure, name
        assert bip340.verify(msg, bytes.fromhex(k["pubkey"]), pure), name
        assert _verify_pure(msg, bytes.fromhex(k["pubkey"]), fast), name


def test_bip340_official_vectors():
    """A minimal BIP-340 self-check: sign/verify round-trips for each vector
    key, so trusting the signer doesn't rest on the protocol vectors alone."""
    for name, k in _data["keys"].items():
        seckey = bytes.fromhex(k["seckey"])
        assert bip340.pubkey_from_seckey(seckey).hex() == k["pubkey"], name
        msg = bip340.tagged_hash(TAG_PREFIX + "test", name.encode())
        sig = bip340.sign(msg, seckey)
        assert bip340.verify(msg, bytes.fromhex(k["pubkey"]), sig), name
        # A flipped message must not verify.
        bad = bytearray(msg)
        bad[0] ^= 1
        assert not bip340.verify(bytes(bad), bytes.fromhex(k["pubkey"]), sig)
