"""NWC (Nostr Wallet Connect, NIP-47) pay backend.

Pays BOLT11 invoices through a wallet reached over a Nostr relay — e.g. a
Zeus/LDK node on a phone. The whole spend capability is one config value: the
``nostr+walletconnect://<wallet-pubkey>?relay=wss://...&secret=<hex>`` URI.
That URI is the crown jewel; it lives in the deployment's config file, never
in this repository.

Design notes, inherited from the operator's proven NWC setup:

- **Encryption is never guessed.** The wallet may speak NIP-04 (AES-CBC) or
  NIP-44 v2 (ChaCha20+HMAC), and a request under the wrong scheme hits dead
  air. Unless pinned in config, the wallet is probed with a harmless
  ``get_info`` and only a scheme it actually answered is used — then persisted,
  and dropped again if it ever stops answering, so a stale choice self-heals.
- **Subscribe before publish**, so the response can't be missed.
- **Park before paying.** Every attempt is recorded in a SQLite ledger before
  the wallet is contacted. A timeout is UNCONFIRMED, not failed — the payment
  may still settle after we stop waiting — so a timed-out attempt raises a
  ``PaymentError`` that names the invoice as possibly-settled rather than
  silently retrying, and the ledger row survives for the operator.
- **Caps on top of the wallet's own NWC budget**: a per-payment cap and a
  per-day cap, enforced against the ledger, in addition to the per-call
  ceiling economics passes in.

Requires ``websockets`` and ``cryptography`` (the ``[nwc]`` extra), imported
lazily so a broker that never pays needs neither.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac as hmac_mod
import json
import os
import re
import secrets
import sqlite3
import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from ..wire import bip340
from .pay import PayProvider, PaymentCapExceeded, PaymentError

_KIND_REQUEST = 23194
_KIND_RESPONSE = 23195
_PROBE_WAIT = 8  # seconds per encryption probe on first contact


# --- connection URI ---------------------------------------------------------

def parse_nwc_uri(uri: str) -> dict:
    uri = (uri or "").strip()
    if not uri.startswith("nostr+walletconnect://"):
        raise PaymentError("not a nostr+walletconnect:// URI")
    parsed = urlparse(uri)
    wallet_pubkey = (parsed.netloc or parsed.path.lstrip("/")).lower()
    q = parse_qs(parsed.query)
    relay = (q.get("relay") or [""])[0]
    secret = ((q.get("secret") or [""])[0]).lower()
    if not re.fullmatch(r"[0-9a-f]{64}", wallet_pubkey or ""):
        raise PaymentError("connection URI has no valid wallet pubkey")
    if not relay.startswith(("ws://", "wss://")):
        raise PaymentError("connection URI has no valid relay= websocket URL")
    if not re.fullmatch(r"[0-9a-f]{64}", secret or ""):
        raise PaymentError("connection URI has no valid secret=")
    return {"wallet_pubkey": wallet_pubkey, "relay": relay, "secret": secret}


# --- BOLT11 amount (hrp only — enough for cap enforcement) ------------------

_BOLT11_RE = re.compile(r"^ln(bcrt|tbs|tb|bc)(\d+)?([munp])?1", re.IGNORECASE)
_MULTIPLIER = {"": 1, "m": 10 ** 3, "u": 10 ** 6, "n": 10 ** 9, "p": 10 ** 12}


def bolt11_amount_msat(invoice: str) -> int | None:
    """Amount in msat from the invoice's human-readable part, or None when
    the invoice carries no amount. Raises PaymentError on non-bolt11 input."""
    inv = (invoice or "").strip().lower()
    m = _BOLT11_RE.match(inv)
    if not m:
        raise PaymentError("not a BOLT11 invoice")
    if not m.group(2):
        return None
    amount, unit = int(m.group(2)), (m.group(3) or "")
    msat_times_div = amount * 10 ** 11  # 1 BTC = 10^11 msat
    div = _MULTIPLIER[unit]
    if msat_times_div % div:
        raise PaymentError("invoice amount is not an integer msat value")
    return msat_times_div // div


# --- NIP-04 (ECDH -> AES-256-CBC) -------------------------------------------

def _ecdh(secret_hex: str, peer_pub_hex: str) -> bytes:
    return bip340.ecdh_x(bytes.fromhex(secret_hex), bytes.fromhex(peer_pub_hex))


def nip04_encrypt(secret_hex: str, peer_pub_hex: str, plaintext: str) -> str:
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    key = _ecdh(secret_hex, peer_pub_hex)
    iv = os.urandom(16)
    data = plaintext.encode("utf-8")
    pad = 16 - len(data) % 16
    data += bytes([pad]) * pad
    enc = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
    ct = enc.update(data) + enc.finalize()
    return (base64.b64encode(ct).decode() + "?iv="
            + base64.b64encode(iv).decode())


def nip04_decrypt(secret_hex: str, peer_pub_hex: str, content: str) -> str:
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    if "?iv=" not in (content or ""):
        raise PaymentError("malformed NIP-04 payload (no ?iv=)")
    ct_b64, iv_b64 = content.split("?iv=", 1)
    key = _ecdh(secret_hex, peer_pub_hex)
    dec = Cipher(algorithms.AES(key),
                 modes.CBC(base64.b64decode(iv_b64))).decryptor()
    data = dec.update(base64.b64decode(ct_b64)) + dec.finalize()
    pad = data[-1]
    if not 1 <= pad <= 16:
        raise PaymentError("malformed NIP-04 padding")
    return data[:-pad].decode("utf-8")


# --- NIP-44 v2 (ECDH -> HKDF -> ChaCha20 + HMAC-SHA256) ---------------------

def _nip44_conversation_key(secret_hex: str, peer_pub_hex: str) -> bytes:
    return hmac_mod.new(b"nip44-v2", _ecdh(secret_hex, peer_pub_hex),
                        hashlib.sha256).digest()


def _nip44_message_keys(conversation_key: bytes, nonce: bytes):
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.hkdf import HKDFExpand
    keys = HKDFExpand(algorithm=hashes.SHA256(), length=76,
                      info=nonce).derive(conversation_key)
    return keys[:32], keys[32:44], keys[44:76]  # chacha key, nonce, hmac key


def _nip44_padded_len(unpadded_len: int) -> int:
    if unpadded_len <= 32:
        return 32
    next_power = 1 << (unpadded_len - 1).bit_length()
    chunk = 32 if next_power <= 256 else next_power // 8
    return chunk * ((unpadded_len - 1) // chunk + 1)


def _chacha20(key: bytes, nonce12: bytes, data: bytes) -> bytes:
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms
    # cryptography's ChaCha20 nonce is 16 bytes: 4-byte little-endian initial
    # counter + the RFC 7539 12-byte nonce. NIP-44 uses counter 0.
    enc = Cipher(algorithms.ChaCha20(key, b"\x00" * 4 + nonce12),
                 mode=None).encryptor()
    return enc.update(data)


def nip44_encrypt(secret_hex: str, peer_pub_hex: str, plaintext: str) -> str:
    data = plaintext.encode("utf-8")
    if not 1 <= len(data) <= 65535:
        raise PaymentError("nip44 plaintext must be 1..65535 bytes")
    nonce = os.urandom(32)
    ck = _nip44_conversation_key(secret_hex, peer_pub_hex)
    chacha_key, chacha_nonce, hmac_key = _nip44_message_keys(ck, nonce)
    padded = (len(data).to_bytes(2, "big") + data
              + b"\x00" * (_nip44_padded_len(len(data)) - len(data)))
    ct = _chacha20(chacha_key, chacha_nonce, padded)
    mac = hmac_mod.new(hmac_key, nonce + ct, hashlib.sha256).digest()
    return base64.b64encode(b"\x02" + nonce + ct + mac).decode()


def nip44_decrypt(secret_hex: str, peer_pub_hex: str, payload: str) -> str:
    payload = payload or ""
    if payload.startswith("#"):
        raise PaymentError("peer used an unsupported nip44 version")
    try:
        data = base64.b64decode(payload, validate=True)
    except Exception:
        raise PaymentError("malformed nip44 payload (bad base64)")
    if len(data) < 1 + 32 + 32 + 32 or data[0] != 2:
        raise PaymentError("malformed nip44 payload")
    nonce, ct, mac = data[1:33], data[33:-32], data[-32:]
    ck = _nip44_conversation_key(secret_hex, peer_pub_hex)
    chacha_key, chacha_nonce, hmac_key = _nip44_message_keys(ck, nonce)
    if not hmac_mod.compare_digest(
            hmac_mod.new(hmac_key, nonce + ct, hashlib.sha256).digest(), mac):
        raise PaymentError("nip44 MAC check failed")
    padded = _chacha20(chacha_key, chacha_nonce, ct)
    unpadded_len = int.from_bytes(padded[:2], "big")
    if not 1 <= unpadded_len <= len(padded) - 2 \
            or len(padded) != 2 + _nip44_padded_len(unpadded_len):
        raise PaymentError("nip44 padding check failed")
    return padded[2:2 + unpadded_len].decode("utf-8")


def _encrypt(scheme: str, secret_hex: str, peer_pub_hex: str, text: str) -> str:
    if scheme == "nip44_v2":
        return nip44_encrypt(secret_hex, peer_pub_hex, text)
    return nip04_encrypt(secret_hex, peer_pub_hex, text)


def _decrypt(secret_hex: str, peer_pub_hex: str, content: str) -> str:
    # The payload shape identifies the scheme ("?iv=" is nip04-only), so a
    # wallet answering in either encryption decrypts correctly.
    if "?iv=" in (content or ""):
        return nip04_decrypt(secret_hex, peer_pub_hex, content)
    return nip44_decrypt(secret_hex, peer_pub_hex, content)


# --- Nostr events -----------------------------------------------------------

def _signed_event(secret_hex: str, kind: int, tags: list, content: str) -> dict:
    seckey = bytes.fromhex(secret_hex)
    pubkey = bip340.pubkey_from_seckey(seckey).hex()
    created_at = int(time.time())
    payload = json.dumps([0, pubkey, created_at, kind, tags, content],
                         separators=(",", ":"), ensure_ascii=False)
    event_id = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    sig = bip340.sign(bytes.fromhex(event_id), seckey,
                      aux_rand=secrets.token_bytes(32)).hex()
    return {"id": event_id, "pubkey": pubkey, "created_at": created_at,
            "kind": kind, "tags": tags, "content": content, "sig": sig}


# --- NIP-47 exchange --------------------------------------------------------

class _Timeout(PaymentError):
    """No response inside the deadline. The request MAY still be in flight."""


async def _roundtrip(ws, conn, scheme, method, params, loop, deadline):
    body = json.dumps({"method": method, "params": params or {}})
    content = _encrypt(scheme, conn["secret"], conn["wallet_pubkey"], body)
    tags = [["p", conn["wallet_pubkey"]]]
    if scheme == "nip44_v2":
        tags.append(["encryption", "nip44_v2"])
    event = _signed_event(conn["secret"], _KIND_REQUEST, tags, content)

    sub_id = secrets.token_hex(8)
    # Subscribe for the response BEFORE publishing, so it can't be missed.
    await ws.send(json.dumps(["REQ", sub_id, {
        "kinds": [_KIND_RESPONSE], "#e": [event["id"]],
        "authors": [conn["wallet_pubkey"]]}]))
    await ws.send(json.dumps(["EVENT", event]))
    while True:
        remaining = deadline - loop.time()
        if remaining <= 0:
            await ws.send(json.dumps(["CLOSE", sub_id]))
            raise _Timeout("timed out waiting for the wallet's response")
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
        except asyncio.TimeoutError:
            continue
        try:
            msg = json.loads(raw)
        except (ValueError, TypeError):
            continue
        if not isinstance(msg, list) or not msg:
            continue
        if msg[0] == "OK" and len(msg) >= 4 and msg[1] == event["id"] \
                and not msg[2]:
            raise PaymentError(f"relay rejected the request: {msg[3]}")
        if msg[0] == "EVENT" and len(msg) >= 3 and msg[1] == sub_id:
            return msg[2]
        if msg[0] == "CLOSED" and len(msg) >= 2 and msg[1] == sub_id:
            raise PaymentError("relay closed the subscription before the "
                               "wallet answered")


async def _session(conn, method, params, scheme_store, pinned, timeout):
    import websockets
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    async with websockets.connect(conn["relay"], open_timeout=min(timeout, 10),
                                  close_timeout=3) as ws:
        scheme = pinned or scheme_store.get()
        probed = False
        if scheme is None:
            # Probe with a harmless get_info; only a scheme the wallet
            # actually answered is ever used for the real request.
            for candidate in ("nip44_v2", "nip04"):
                try:
                    await _roundtrip(ws, conn, candidate, "get_info", {},
                                     loop, min(deadline,
                                               loop.time() + _PROBE_WAIT))
                    scheme = candidate
                    probed = True
                    break
                except _Timeout:
                    continue
            if scheme is None:
                raise PaymentError(
                    "the wallet answered neither a nip44 nor a nip04 probe — "
                    "it looks offline or unreachable via its relay; the "
                    "request was NOT sent and no payment can have been "
                    "attempted")
            scheme_store.put(scheme)
        try:
            resp = await _roundtrip(ws, conn, scheme, method, params, loop,
                                    deadline)
        except _Timeout:
            if not pinned and not probed:
                scheme_store.drop()  # stale stored choice; re-probe next call
            raise
        if not pinned:
            scheme_store.put(scheme)
        return resp


class _SchemeStore:
    """The wallet-confirmed encryption scheme, persisted across restarts."""

    def __init__(self, path: Path):
        self._path = path

    def get(self) -> str | None:
        try:
            value = json.loads(self._path.read_text()).get("scheme")
            return value if value in ("nip04", "nip44_v2") else None
        except (OSError, ValueError):
            return None

    def put(self, scheme: str) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(json.dumps({"scheme": scheme}))
        except OSError:
            pass  # persistence is best-effort

    def drop(self) -> None:
        try:
            self._path.unlink(missing_ok=True)
        except OSError:
            pass


def nwc_call(uri: str, method: str, params: dict, *, scheme_store,
             pinned: str | None = None, timeout: float = 30) -> dict:
    """One NIP-47 request: negotiate encryption, publish, await, decrypt."""
    conn = parse_nwc_uri(uri)
    try:
        resp = asyncio.run(_session(conn, method, params, scheme_store,
                                    pinned, timeout))
    except PaymentError:
        raise
    except ImportError:
        raise PaymentError("NWC needs the websockets + cryptography packages "
                           "(install the [nwc] extra)")
    except Exception as e:  # websocket/DNS/TLS failures
        raise PaymentError(f"relay connection failed: {e}")
    try:
        plain = _decrypt(conn["secret"], resp.get("pubkey", ""),
                         resp.get("content", ""))
        data = json.loads(plain)
    except PaymentError:
        raise
    except Exception:
        raise PaymentError("could not decrypt/parse the wallet's response")
    err = data.get("error")
    if err:
        raise PaymentError(f"wallet error {err.get('code', '?')}: "
                           f"{err.get('message', '')}")
    return data.get("result") or {}


# --- the provider -----------------------------------------------------------

_LEDGER_SCHEMA = """
CREATE TABLE IF NOT EXISTS nwc_spends (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    day TEXT NOT NULL,
    amount_msat INTEGER NOT NULL,
    invoice TEXT NOT NULL,
    preimage TEXT,
    status TEXT NOT NULL  -- 'paid' | 'unconfirmed'
);
CREATE INDEX IF NOT EXISTS idx_nwc_spends_day ON nwc_spends(day);
"""


class NwcPayProvider(PayProvider):
    """Pay invoices over Nostr Wallet Connect, with local spend caps.

    The connection URI comes from deployment config and is the spend
    credential. Caps are on top of the wallet's own NWC budget; both an
    unconfirmed (timed-out) and a confirmed payment count against the daily
    cap, because the money may be gone either way.
    """

    can_pay = True

    def __init__(self, uri: str, data_dir: str | Path, *,
                 max_msat_per_payment: int = 100_000,
                 max_msat_per_day: int = 500_000,
                 encryption: str = "auto", timeout: float = 30):
        parse_nwc_uri(uri)  # fail fast on a malformed credential
        self._uri = uri
        data_dir = Path(data_dir)
        data_dir.mkdir(parents=True, exist_ok=True)
        self._con = sqlite3.connect(data_dir / "nwc_ledger.db",
                                    check_same_thread=False)
        self._con.executescript(_LEDGER_SCHEMA)
        self._con.commit()
        self._scheme_store = _SchemeStore(data_dir / "nwc_scheme.json")
        pinned = encryption.lower()
        self._pinned = ("nip44_v2" if pinned.startswith("nip44") else "nip04") \
            if pinned in ("nip04", "nip44", "nip44_v2") else None
        self._cap_payment = max_msat_per_payment
        self._cap_day = max_msat_per_day
        self._timeout = timeout

    def _spent_today(self) -> int:
        row = self._con.execute(
            "SELECT COALESCE(SUM(amount_msat), 0) FROM nwc_spends "
            "WHERE day = ?", (time.strftime("%Y-%m-%d"),)).fetchone()
        return int(row[0] or 0)

    def pay(self, bolt11: str, max_amount_msat: int) -> str:
        amount = bolt11_amount_msat(bolt11)
        if amount is None:
            raise PaymentError("amountless invoice refused: spending caps "
                               "cannot be enforced")
        if amount > max_amount_msat:
            raise PaymentCapExceeded(
                f"invoice is {amount} msat, over the {max_amount_msat} "
                "ceiling for this ask")
        if amount > self._cap_payment:
            raise PaymentCapExceeded(
                f"invoice is {amount} msat, over the per-payment cap of "
                f"{self._cap_payment}")
        if self._spent_today() + amount > self._cap_day:
            raise PaymentCapExceeded(
                f"paying {amount} msat would exceed the daily cap of "
                f"{self._cap_day}")

        # Park the attempt BEFORE contacting the wallet, so a timeout or a
        # crash always leaves a trace — and counts against the daily cap.
        now = time.strftime("%Y-%m-%dT%H:%M:%S")
        cur = self._con.execute(
            "INSERT INTO nwc_spends (created_at, day, amount_msat, invoice, "
            "status) VALUES (?, ?, ?, ?, 'unconfirmed')",
            (now, time.strftime("%Y-%m-%d"), amount, bolt11))
        row_id = cur.lastrowid
        self._con.commit()
        try:
            result = nwc_call(self._uri, "pay_invoice", {"invoice": bolt11},
                              scheme_store=self._scheme_store,
                              pinned=self._pinned, timeout=self._timeout)
        except _Timeout:
            # Unconfirmed, not failed: the ledger row stays, the cap holds.
            raise PaymentError(
                "the wallet did not answer in time — the payment is "
                "UNCONFIRMED and may still settle; it remains counted "
                "against the daily cap and is recorded in the ledger")
        except PaymentError:
            # The wallet (or relay) answered definitively: nothing was spent.
            self._con.execute("DELETE FROM nwc_spends WHERE id = ?", (row_id,))
            self._con.commit()
            raise
        preimage = (result.get("preimage") or "").strip()
        if not re.fullmatch(r"[0-9a-f]{64}", preimage):
            raise PaymentError("wallet reported success but returned no "
                               "usable preimage")
        self._con.execute(
            "UPDATE nwc_spends SET status = 'paid', preimage = ? "
            "WHERE id = ?", (preimage, row_id))
        self._con.commit()
        return preimage
