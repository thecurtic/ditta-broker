"""NWC pay backend: crypto, caps, ledger, and a fake wallet over a real
websocket relay.

The ECDH is cross-checked against coincurve (an independent secp256k1
implementation), the NIP-04/NIP-44 payloads round-trip and reject tampering,
and the integration tests drive NwcPayProvider end to end against an
in-process relay+wallet — real websockets, real encryption, no real money.
"""

import asyncio
import json
import socket
import threading

import pytest

from broker.payment.nwc import (
    NwcPayProvider,
    PaymentCapExceeded,
    PaymentError,
    _decrypt,
    _encrypt,
    _signed_event,
    bolt11_amount_msat,
    nip04_decrypt,
    nip04_encrypt,
    nip44_decrypt,
    nip44_encrypt,
    parse_nwc_uri,
)
from broker.wire import bip340

CLIENT_SEC = "11" * 32
WALLET_SEC = "22" * 32
CLIENT_PUB = bip340.pubkey_from_seckey(bytes.fromhex(CLIENT_SEC)).hex()
WALLET_PUB = bip340.pubkey_from_seckey(bytes.fromhex(WALLET_SEC)).hex()
PREIMAGE = "ab" * 32


def _uri(relay=f"wss://relay.test", wallet=WALLET_PUB, secret=CLIENT_SEC):
    return f"nostr+walletconnect://{wallet}?relay={relay}&secret={secret}"


# --- crypto -----------------------------------------------------------------

def test_ecdh_matches_coincurve_and_is_symmetric():
    from coincurve import PublicKey

    ours = bip340.ecdh_x(bytes.fromhex(CLIENT_SEC), bytes.fromhex(WALLET_PUB))
    point = PublicKey(bytes.fromhex("02" + WALLET_PUB)).multiply(
        bytes.fromhex(CLIENT_SEC))
    x, _y = point.point()
    assert ours == x.to_bytes(32, "big")
    # X-only ECDH is symmetric: the wallet derives the same shared X.
    theirs = bip340.ecdh_x(bytes.fromhex(WALLET_SEC), bytes.fromhex(CLIENT_PUB))
    assert ours == theirs


def test_nip04_round_trip():
    ct = nip04_encrypt(CLIENT_SEC, WALLET_PUB, "hello — привет")
    assert "?iv=" in ct
    assert nip04_decrypt(WALLET_SEC, CLIENT_PUB, ct) == "hello — привет"


def test_nip44_round_trip_and_tamper_detection():
    ct = nip44_encrypt(CLIENT_SEC, WALLET_PUB, "pay_invoice please")
    assert nip44_decrypt(WALLET_SEC, CLIENT_PUB, ct) == "pay_invoice please"
    import base64
    blob = bytearray(base64.b64decode(ct))
    blob[40] ^= 1  # flip one ciphertext bit
    with pytest.raises(PaymentError):
        nip44_decrypt(WALLET_SEC, CLIENT_PUB,
                      base64.b64encode(bytes(blob)).decode())


def test_parse_uri_rejects_garbage():
    parsed = parse_nwc_uri(_uri())
    assert parsed["wallet_pubkey"] == WALLET_PUB
    for bad in ("", "https://x", _uri(secret="xyz"),
                f"nostr+walletconnect://{WALLET_PUB}?secret={CLIENT_SEC}"):
        with pytest.raises(PaymentError):
            parse_nwc_uri(bad)


def test_bolt11_amounts():
    assert bolt11_amount_msat("lnbc25u1pjunk") == 2_500_000   # 2500 sats
    assert bolt11_amount_msat("lnbc10n1pjunk") == 1_000       # 1 sat
    assert bolt11_amount_msat("lnbc1pjunk") is None           # amountless
    with pytest.raises(PaymentError):
        bolt11_amount_msat("not an invoice")


# --- caps and ledger (wallet faked at the call boundary) --------------------

def _provider(tmp_path, **kwargs):
    return NwcPayProvider(_uri(), tmp_path, **kwargs)


def test_caps_enforced_before_any_wallet_contact(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr("broker.payment.nwc.nwc_call",
                        lambda *a, **k: calls.append(1) or {})
    p = _provider(tmp_path, max_msat_per_payment=1_000_000,
                  max_msat_per_day=2_000_000)
    with pytest.raises(PaymentCapExceeded):   # over the per-ask ceiling
        p.pay("lnbc25u1pjunk", max_amount_msat=1_000_000)  # 2.5M msat
    with pytest.raises(PaymentError):          # amountless refused
        p.pay("lnbc1pjunk", max_amount_msat=10)
    assert calls == []                         # wallet never contacted


def test_success_records_paid_and_daily_cap_accumulates(tmp_path, monkeypatch):
    monkeypatch.setattr("broker.payment.nwc.nwc_call",
                        lambda *a, **k: {"preimage": PREIMAGE})
    p = _provider(tmp_path, max_msat_per_payment=5_000_000,
                  max_msat_per_day=4_000_000)
    assert p.pay("lnbc25u1pjunk", 5_000_000) == PREIMAGE   # 2.5M
    with pytest.raises(PaymentCapExceeded):                # 2.5M + 2.5M > 4M
        p.pay("lnbc25u1pjunk", 5_000_000)
    assert p._spent_today() == 2_500_000


def test_definitive_wallet_failure_frees_the_cap(tmp_path, monkeypatch):
    def refuse(*a, **k):
        raise PaymentError("wallet error INSUFFICIENT_BALANCE:")
    monkeypatch.setattr("broker.payment.nwc.nwc_call", refuse)
    p = _provider(tmp_path)
    with pytest.raises(PaymentError):
        p.pay("lnbc10n1pjunk", 10_000)
    assert p._spent_today() == 0   # a definitive failure spent nothing


def test_timeout_keeps_the_unconfirmed_spend(tmp_path, monkeypatch):
    from broker.payment.nwc import _Timeout

    def hang(*a, **k):
        raise _Timeout("no answer")
    monkeypatch.setattr("broker.payment.nwc.nwc_call", hang)
    p = _provider(tmp_path)
    with pytest.raises(PaymentError, match="UNCONFIRMED"):
        p.pay("lnbc10n1pjunk", 10_000)
    assert p._spent_today() == 1_000   # may have settled: cap holds the spend


# --- integration: a fake wallet behind a real websocket relay ---------------

class FakeWalletRelay:
    """Relay and wallet in one in-process websocket server.

    Decrypts NIP-47 requests with the wallet key, answers get_info and
    pay_invoice, and replies in whichever encryption the request used.
    """

    def __init__(self, fail_payment=False):
        self.fail_payment = fail_payment
        self.port = self._free_port()
        self._stop = None
        self._ready = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        assert self._ready.wait(5), "fake relay failed to start"

    @staticmethod
    def _free_port():
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        return port

    @property
    def url(self):
        return f"ws://127.0.0.1:{self.port}"

    def _run(self):
        asyncio.run(self._serve())

    async def _serve(self):
        import websockets

        self._stop = asyncio.Event()
        async with websockets.serve(self._handle, "127.0.0.1", self.port):
            self._ready.set()
            await self._stop.wait()

    def close(self):
        if self._stop is not None:
            self._stop.set()

    async def _handle(self, ws, path=None):
        last_sub = None
        async for raw in ws:
            msg = json.loads(raw)
            if msg[0] == "REQ":
                last_sub = msg[1]
            elif msg[0] == "EVENT":
                event = msg[1]
                body = json.loads(
                    _decrypt(WALLET_SEC, event["pubkey"], event["content"]))
                if body["method"] == "pay_invoice" and self.fail_payment:
                    reply = {"error": {"code": "INSUFFICIENT_BALANCE",
                                       "message": "no funds"}}
                elif body["method"] == "pay_invoice":
                    reply = {"result_type": "pay_invoice",
                             "result": {"preimage": PREIMAGE}}
                else:
                    reply = {"result_type": body["method"], "result": {}}
                scheme = "nip04" if "?iv=" in event["content"] else "nip44_v2"
                content = _encrypt(scheme, WALLET_SEC, event["pubkey"],
                                   json.dumps(reply))
                response = _signed_event(
                    WALLET_SEC, 23195,
                    [["p", event["pubkey"]], ["e", event["id"]]], content)
                await ws.send(json.dumps(["OK", event["id"], True, ""]))
                await ws.send(json.dumps(["EVENT", last_sub, response]))


@pytest.fixture
def relay():
    r = FakeWalletRelay()
    yield r
    r.close()


def test_pay_end_to_end_pinned_nip44(relay, tmp_path):
    p = NwcPayProvider(_uri(relay=relay.url), tmp_path,
                       encryption="nip44_v2", timeout=10)
    assert p.pay("lnbc10n1pjunk", 10_000) == PREIMAGE


def test_pay_end_to_end_auto_probes_and_persists_scheme(relay, tmp_path):
    p = NwcPayProvider(_uri(relay=relay.url), tmp_path, timeout=10)
    assert p.pay("lnbc10n1pjunk", 10_000) == PREIMAGE
    # The probe-confirmed scheme is persisted for the next restart.
    assert json.loads(
        (tmp_path / "nwc_scheme.json").read_text())["scheme"] == "nip44_v2"


def test_wallet_refusal_travels_back_as_payment_error(tmp_path):
    r = FakeWalletRelay(fail_payment=True)
    try:
        p = NwcPayProvider(_uri(relay=r.url), tmp_path,
                           encryption="nip44_v2", timeout=10)
        with pytest.raises(PaymentError, match="INSUFFICIENT_BALANCE"):
            p.pay("lnbc10n1pjunk", 10_000)
        assert p._spent_today() == 0
    finally:
        r.close()
