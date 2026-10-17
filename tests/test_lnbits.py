"""The LNbits backend: one wallet, two keys, both halves of the money.

An in-process fake LNbits (real HTTP, wallet-scoped key auth) exercises
the minter (invoice key: mint, settlement watch) and the pay provider
(admin key: pay with caps and the park-before-pay ledger) — including the
property the whole design exists for: the invoice key cannot spend.
"""

import hashlib
import json
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from broker.payment.lnbits import LnbitsInvoiceMinter, LnbitsPayProvider
from broker.payment.pay import PaymentCapExceeded, PaymentError

INVOICE_KEY = "ik_" + "a" * 29
ADMIN_KEY = "ak_" + "b" * 29


class FakeLnbits:
    """One wallet: mint with either key, pay with the admin key only."""

    def __init__(self):
        self.payments = {}   # payment_hash -> {bolt11, amount_msat, paid, preimage}
        self.paid_out = []   # bolt11s paid from this wallet
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, fmt, *args):
                pass

            def _json(self, status, data):
                out = json.dumps(data).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)

            def do_POST(self):
                if self.path != "/api/v1/payments":
                    return self._json(404, {"detail": "no such path"})
                key = self.headers.get("X-Api-Key", "")
                body = json.loads(self.rfile.read(
                    int(self.headers.get("Content-Length", 0))))
                if body.get("out"):
                    if key != ADMIN_KEY:
                        return self._json(
                            401, {"detail": "invoice key cannot pay"})
                    bolt11 = body.get("bolt11", "")
                    entry = next((e for e in outer.payments.values()
                                  if e["bolt11"] == bolt11), None)
                    if entry is None:   # an external invoice: just "pay" it
                        preimage = secrets.token_bytes(32)
                        payment_hash = hashlib.sha256(preimage).hexdigest()
                        entry = {"bolt11": bolt11, "paid": True,
                                 "preimage": preimage.hex()}
                        outer.payments[payment_hash] = entry
                    else:
                        payment_hash = next(
                            h for h, e in outer.payments.items()
                            if e is entry)
                        entry["paid"] = True
                    outer.paid_out.append(bolt11)
                    return self._json(201, {"payment_hash": payment_hash})
                if key not in (INVOICE_KEY, ADMIN_KEY):
                    return self._json(401, {"detail": "bad key"})
                sats = int(body["amount"])
                preimage = secrets.token_bytes(32)
                payment_hash = hashlib.sha256(preimage).hexdigest()
                bolt11 = f"lnbc{sats * 10}n1fake{payment_hash[:12]}"
                outer.payments[payment_hash] = {
                    "bolt11": bolt11, "paid": False,
                    "preimage": preimage.hex()}
                return self._json(201, {"payment_hash": payment_hash,
                                        "bolt11": bolt11})

            def do_GET(self):
                key = self.headers.get("X-Api-Key", "")
                if key not in (INVOICE_KEY, ADMIN_KEY):
                    return self._json(401, {"detail": "bad key"})
                payment_hash = self.path.rsplit("/", 1)[-1]
                entry = outer.payments.get(payment_hash)
                if entry is None:
                    return self._json(404, {"detail": "unknown payment"})
                return self._json(200, {
                    "paid": entry["paid"],
                    "preimage": entry["preimage"] if entry["paid"] else None,
                    "details": {"status": "success" if entry["paid"]
                                else "pending"},
                })

        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self._httpd.serve_forever,
                         daemon=True).start()
        self.url = f"http://127.0.0.1:{self._httpd.server_address[1]}"

    def settle(self, payment_hash):
        self.payments[payment_hash]["paid"] = True

    def shutdown(self):
        self._httpd.shutdown()


@pytest.fixture
def lnbits():
    fake = FakeLnbits()
    yield fake
    fake.shutdown()


def test_minter_mints_whole_sats_and_watches_settlement(lnbits):
    minter = LnbitsInvoiceMinter(lnbits.url, INVOICE_KEY)
    payment_hash, bolt11 = minter.mint(1500, "test")   # 1.5 sat rounds up
    assert bolt11.startswith("lnbc20n1")               # 2 sats
    assert not minter.settled(payment_hash)
    lnbits.settle(payment_hash)
    assert minter.settled(payment_hash)                # the head start's cue
    assert not minter.settled("ff" * 32)               # unknown hash: False


def test_provider_pays_and_returns_the_preimage(lnbits, tmp_path):
    provider = LnbitsPayProvider(lnbits.url, ADMIN_KEY, tmp_path)
    bolt11 = "lnbc100n1fakeindexerinvoice"             # 10 sats
    preimage = provider.pay(bolt11, max_amount_msat=20_000)
    assert len(preimage) == 64
    assert lnbits.paid_out == [bolt11]


def test_invoice_key_cannot_spend(lnbits, tmp_path):
    """The wallet-scoping property the design rests on, verified end to
    end: a receive credential presented to the pay path is refused, and a
    refused payment leaves no ledger charge against the daily cap."""
    provider = LnbitsPayProvider(lnbits.url, INVOICE_KEY, tmp_path)
    with pytest.raises(PaymentError, match="refused"):
        provider.pay("lnbc100n1fake", max_amount_msat=20_000)
    assert lnbits.paid_out == []
    assert provider._spent_today() == 0


def test_caps_enforced_before_any_wallet_contact(lnbits, tmp_path):
    provider = LnbitsPayProvider(lnbits.url, ADMIN_KEY, tmp_path,
                                 max_msat_per_payment=5_000,
                                 max_msat_per_day=12_000)
    with pytest.raises(PaymentCapExceeded, match="per-payment"):
        provider.pay("lnbc100n1fake", 20_000)          # 10 sats > 5 cap
    with pytest.raises(PaymentCapExceeded, match="ceiling for this ask"):
        provider.pay("lnbc40n1fake", 1_000)            # over the ask ceiling
    provider.pay("lnbc40n1fakeone", 20_000)            # 4 sats ok
    provider.pay("lnbc40n1faketwo", 20_000)            # 8 total ok
    with pytest.raises(PaymentCapExceeded, match="daily cap"):
        provider.pay("lnbc50n1fakethree", 20_000)      # 8+5 > 12 daily cap
    assert len(lnbits.paid_out) == 2
    with pytest.raises(PaymentError, match="amountless"):
        provider.pay("lnbc1fakenoamount", 20_000)


def test_config_builders_wire_lnbits(tmp_path, lnbits):
    from broker.config import BrokerConfig, build_pay_provider, build_receiver

    cfg = BrokerConfig(
        data_dir=tmp_path,
        receive={"pricing": [{"type": "per_query", "price": 2000}],
                 "lnbits": {"url": lnbits.url, "invoice_key": INVOICE_KEY}},
        pay={"provider": "lnbits",
             "lnbits": {"url": lnbits.url, "admin_key": ADMIN_KEY}})
    receiver = build_receiver(cfg)
    ch = receiver.challenge(2000, "per_query", 1)
    assert ch["invoice"].startswith("lnbc20n1")        # minted via LNbits
    provider = build_pay_provider(cfg)
    assert provider.can_pay
    assert len(provider.pay("lnbc20n1fakex", 5_000)) == 64
