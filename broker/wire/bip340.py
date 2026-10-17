"""BIP-340 Schnorr signatures over secp256k1, and the tagged hash the
protocol signs with.

Ported from the BIP-340 reference pseudocode (public domain). Pure Python:
fine for signing this node's own low-volume control messages and for the
conformance vectors, deliberately not constant-time or production-speed.

The protocol invents no cryptography. A message's canonical bytes are hashed
with a tagged SHA-256 whose tag is ``ditta/v0/<message type>``, and the
BIP-340 signature is taken over that 32-byte hash.
"""

from __future__ import annotations

import hashlib

# secp256k1 domain parameters.
_p = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEFFFFFC2F
_n = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141
_G = (0x79BE667EF9DCBBAC55A06295CE870B07029BFCDB2DCE28D959F2815B16F81798,
      0x483ADA7726A3C4655DA4FBFC0E1108A8FD17B448A68554199C47D08FFB10D4B8)


def tagged_hash(tag: str, msg: bytes) -> bytes:
    tag_hash = hashlib.sha256(tag.encode("utf-8")).digest()
    return hashlib.sha256(tag_hash + tag_hash + msg).digest()


def _point_add(p1, p2):
    if p1 is None:
        return p2
    if p2 is None:
        return p1
    if p1[0] == p2[0] and p1[1] != p2[1]:
        return None
    if p1 == p2:
        lam = (3 * p1[0] * p1[0] * pow(2 * p1[1], _p - 2, _p)) % _p
    else:
        lam = ((p2[1] - p1[1]) * pow(p2[0] - p1[0], _p - 2, _p)) % _p
    x3 = (lam * lam - p1[0] - p2[0]) % _p
    return (x3, (lam * (p1[0] - x3) - p1[1]) % _p)


def _point_mul(p, n):
    r = None
    for i in range(256):
        if (n >> i) & 1:
            r = _point_add(r, p)
        p = _point_add(p, p)
    return r


def _has_even_y(p) -> bool:
    return p[1] % 2 == 0


def _lift_x(x: int):
    if x >= _p:
        return None
    y_sq = (pow(x, 3, _p) + 7) % _p
    y = pow(y_sq, (_p + 1) // 4, _p)
    if pow(y, 2, _p) != y_sq:
        return None
    return (x, y if y % 2 == 0 else _p - y)


def _int(b: bytes) -> int:
    return int.from_bytes(b, "big")


def _bytes(n: int) -> bytes:
    return n.to_bytes(32, "big")


def ecdh_x(seckey: bytes, xonly_pub: bytes) -> bytes:
    """The raw X coordinate of the ECDH shared point, 32 bytes.

    The peer key is x-only with even Y assumed (the Nostr convention: a
    64-hex pubkey is the point lifted with even Y). Used by the NWC payment
    backend for NIP-04/NIP-44 key derivation; the protocol wire format itself
    never does ECDH.
    """
    d = _int(seckey)
    if not 1 <= d <= _n - 1:
        raise ValueError("secret key out of range")
    P = _lift_x(_int(xonly_pub))
    if P is None:
        raise ValueError("peer public key is not on the curve")
    S = _point_mul(P, d)
    if S is None:
        raise ValueError("ECDH produced the point at infinity")
    return _bytes(S[0])


def pubkey_from_seckey(seckey: bytes) -> bytes:
    """32-byte x-only public key for a 32-byte secret key."""
    d0 = _int(seckey)
    if not 1 <= d0 <= _n - 1:
        raise ValueError("secret key out of range")
    return _bytes(_point_mul(_G, d0)[0])


# Optional fast path: libsecp256k1 via coincurve. BIP-340 signing is fully
# deterministic given (key, message, aux), so both paths produce identical
# bytes — pinned by test — and the pure-Python implementation above remains
# the reference and the fallback. At ~0.7s per pure sign, the C path is
# what makes a multi-indexer route's crypto cost invisible.
try:
    from coincurve import PrivateKey as _CCPrivateKey
    from coincurve.keys import PublicKeyXOnly as _CCPublicKeyXOnly
except ImportError:  # pure Python only — correct, just slower
    _CCPrivateKey = None
    _CCPublicKeyXOnly = None


def sign(msg32: bytes, seckey: bytes, aux_rand: bytes = b"\x00" * 32) -> bytes:
    """BIP-340 signature over a 32-byte message with a 32-byte secret key."""
    if _CCPrivateKey is not None:
        return _CCPrivateKey(seckey).sign_schnorr(msg32, aux_rand)
    return _sign_pure(msg32, seckey, aux_rand)


def verify(msg32: bytes, pubkey32: bytes, sig64: bytes) -> bool:
    """Verify a 64-byte BIP-340 signature against a 32-byte x-only pubkey."""
    if len(pubkey32) != 32 or len(sig64) != 64 or len(msg32) != 32:
        return False
    if _CCPublicKeyXOnly is not None:
        try:
            return _CCPublicKeyXOnly(pubkey32).verify(sig64, msg32)
        except Exception:
            return False  # e.g. pubkey not a curve point
    return _verify_pure(msg32, pubkey32, sig64)


def _sign_pure(msg32: bytes, seckey: bytes,
               aux_rand: bytes = b"\x00" * 32) -> bytes:
    d0 = _int(seckey)
    if not 1 <= d0 <= _n - 1:
        raise ValueError("secret key out of range")
    P = _point_mul(_G, d0)
    d = d0 if _has_even_y(P) else _n - d0
    t = d ^ _int(tagged_hash("BIP0340/aux", aux_rand))
    k0 = _int(tagged_hash(
        "BIP0340/nonce", _bytes(t) + _bytes(P[0]) + msg32)) % _n
    if k0 == 0:
        raise RuntimeError("nonce is zero (astronomically unlikely)")
    R = _point_mul(_G, k0)
    k = k0 if _has_even_y(R) else _n - k0
    e = _int(tagged_hash(
        "BIP0340/challenge", _bytes(R[0]) + _bytes(P[0]) + msg32)) % _n
    sig = _bytes(R[0]) + _bytes((k + e * d) % _n)
    if not _verify_pure(msg32, _bytes(P[0]), sig):
        raise RuntimeError("produced signature failed self-verification")
    return sig


def _verify_pure(msg32: bytes, pubkey32: bytes, sig64: bytes) -> bool:
    if len(pubkey32) != 32 or len(sig64) != 64 or len(msg32) != 32:
        return False
    P = _lift_x(_int(pubkey32))
    if P is None:
        return False
    r = _int(sig64[:32])
    s = _int(sig64[32:])
    if r >= _p or s >= _n:
        return False
    e = _int(tagged_hash(
        "BIP0340/challenge", sig64[:32] + pubkey32 + msg32)) % _n
    R = _point_add(_point_mul(_G, s), _point_mul(P, _n - e))
    if R is None or not _has_even_y(R) or R[0] != r:
        return False
    return True
