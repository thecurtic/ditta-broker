"""Node identity: a secp256k1 keypair and message signing.

A node's durable identity is its x-only secp256k1 public key (Keys and
signatures). The secret key is generated once and persisted to a file with
owner-only permissions; the public key, 64 lowercase hex, is what the node
announces and signs under.

Generating a fresh key for this protocol costs nothing and is recommended
(Security considerations): do not reuse a Nostr or Lightning key here.
"""

from __future__ import annotations

import os
import secrets
from pathlib import Path

from .wire import bip340
from .wire.canonical import canonical_bytes
from .wire.validate import TAG_PREFIX


class Identity:
    def __init__(self, seckey: bytes):
        if len(seckey) != 32:
            raise ValueError("secret key must be 32 bytes")
        self._seckey = seckey
        self.pubkey = bip340.pubkey_from_seckey(seckey).hex()

    @classmethod
    def generate(cls) -> "Identity":
        return cls(secrets.token_bytes(32))

    @classmethod
    def load_or_create(cls, path: str | os.PathLike) -> "Identity":
        p = Path(path)
        if p.exists():
            return cls(bytes.fromhex(p.read_text().strip()))
        p.parent.mkdir(parents=True, exist_ok=True)
        ident = cls.generate()
        # Owner-only before writing the secret.
        fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(ident._seckey.hex())
        return ident

    def sign_message(self, message: dict, message_type: str) -> str:
        """Return the hex BIP-340 signature over a message of the given type.

        The message must already carry this node's ``pubkey`` and must not
        carry a ``sig`` field; the caller adds the returned signature as
        ``sig``.
        """
        if message.get("pubkey") != self.pubkey:
            raise ValueError("message pubkey does not match this identity")
        body = {k: v for k, v in message.items() if k != "sig"}
        digest = bip340.tagged_hash(
            TAG_PREFIX + message_type, canonical_bytes(body))
        return bip340.sign(digest, self._seckey).hex()

    def signed(self, message: dict, message_type: str) -> dict:
        """Return a copy of ``message`` with a valid ``sig`` field added."""
        out = dict(message)
        out["sig"] = self.sign_message(out, message_type)
        return out
