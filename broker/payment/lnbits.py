"""LNbits backend: receive and pay against one LNbits wallet.

The deployment story this exists for: the operator's LND node runs LNbits,
the broker gets ONE LNbits wallet (an account), funded with its working
budget, and two keys scoped to that wallet alone —

- the **invoice key** can create invoices and read their status: the
  receive credential, safe anywhere (it cannot move money);
- the **admin key** can additionally pay invoices FROM THAT WALLET ONLY:
  the spend credential, whose blast radius is the wallet's balance, not
  the node.

That wallet-level scoping is the point of using LNbits here rather than a
node-wide macaroon. On top of it this provider enforces the same local
discipline as the NWC backend: park-before-pay ledger, per-payment and
per-day caps, amountless invoices refused.

One LNbits quirk: invoices are minted in whole satoshis, so a fractional
msat amount rounds UP to the next sat. The 402's signed payment challenge
carries the invoice as minted, and the web client already displays
quote-vs-invoice differences rather than hiding them.

API surface used (LNbits v0/v1 both answered): POST /api/v1/payments to
mint (out: false) and pay (out: true); GET /api/v1/payments/<hash> for
status and preimage. httpx is imported lazily like the LND minter's.
"""

from __future__ import annotations

import math
import re
import sqlite3
import time
from pathlib import Path

from .invoice import InvoiceMinter
from .nwc import bolt11_amount_msat
from .pay import PayProvider, PaymentCapExceeded, PaymentError

_PREIMAGE_RE = re.compile(r"^[0-9a-f]{64}$")


def _payment_fields(data: dict) -> dict:
    """Flatten the version-dependent shapes LNbits answers with."""
    out = dict(data.get("details") or {})
    out.update({k: v for k, v in data.items() if k != "details"})
    return out


def _is_settled(data: dict) -> bool:
    fields = _payment_fields(data)
    return data.get("paid") is True or fields.get("paid") is True \
        or fields.get("status") == "success"


class LnbitsInvoiceMinter(InvoiceMinter):
    """Mint (and watch) invoices with a wallet-scoped LNbits invoice key."""

    def __init__(self, url: str, invoice_key: str,
                 invoice_expiry: int = 3600, timeout: float = 15):
        self._url = url.rstrip("/")
        self._key = invoice_key
        self._expiry = self.invoice_expiry = invoice_expiry
        self._timeout = timeout

    def _request(self, method: str, path: str, **kwargs):
        import httpx

        resp = httpx.request(
            method, self._url + path,
            headers={"X-Api-Key": self._key}, timeout=self._timeout,
            **kwargs)
        resp.raise_for_status()
        return resp.json()

    def mint(self, amount_msat: int, memo: str) -> tuple[str, str]:
        sats = max(1, math.ceil(amount_msat / 1000))  # LNbits mints whole sats
        data = self._request(
            "POST", "/api/v1/payments",
            json={"out": False, "amount": sats, "unit": "sat",
                  "memo": memo, "expiry": self._expiry})
        bolt11 = data.get("bolt11") or data.get("payment_request") or ""
        payment_hash = data.get("payment_hash") or ""
        if not bolt11 or not payment_hash:
            raise PaymentError("LNbits minted no usable invoice")
        return payment_hash, bolt11

    def settled(self, payment_hash: str) -> bool:
        """Status lookup — what the head start and witnessed redemption use."""
        try:
            return _is_settled(
                self._request("GET", f"/api/v1/payments/{payment_hash}"))
        except Exception:
            return False  # a flaky lookup only delays the head start


_LEDGER_SCHEMA = """
CREATE TABLE IF NOT EXISTS lnbits_spends (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    day TEXT NOT NULL,
    amount_msat INTEGER NOT NULL,
    invoice TEXT NOT NULL,
    preimage TEXT,
    status TEXT NOT NULL  -- 'paid' | 'unconfirmed'
);
CREATE INDEX IF NOT EXISTS idx_lnbits_spends_day ON lnbits_spends(day);
"""


class LnbitsPayProvider(PayProvider):
    """Pay invoices from the broker's own LNbits wallet, with local caps.

    The admin key is the spend credential; it can empty exactly one
    wallet. The caps here are on top of that balance bound, and the
    park-before-pay ledger means a crash or timeout always leaves a trace
    that keeps counting against the daily cap — the money may be gone
    either way.
    """

    can_pay = True

    def __init__(self, url: str, admin_key: str, data_dir: str | Path, *,
                 max_msat_per_payment: int = 100_000,
                 max_msat_per_day: int = 500_000, timeout: float = 30):
        self._url = url.rstrip("/")
        self._key = admin_key
        self._timeout = timeout
        self._cap_payment = max_msat_per_payment
        self._cap_day = max_msat_per_day
        data_dir = Path(data_dir)
        data_dir.mkdir(parents=True, exist_ok=True)
        self._con = sqlite3.connect(data_dir / "lnbits_ledger.db",
                                    check_same_thread=False)
        self._con.executescript(_LEDGER_SCHEMA)
        self._con.commit()

    def _spent_today(self) -> int:
        row = self._con.execute(
            "SELECT COALESCE(SUM(amount_msat), 0) FROM lnbits_spends "
            "WHERE day = ?", (time.strftime("%Y-%m-%d"),)).fetchone()
        return int(row[0] or 0)

    def pay(self, bolt11: str, max_amount_msat: int) -> str:
        import httpx

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
            "INSERT INTO lnbits_spends (created_at, day, amount_msat, "
            "invoice, status) VALUES (?, ?, ?, ?, 'unconfirmed')",
            (now, time.strftime("%Y-%m-%d"), amount, bolt11))
        row_id = cur.lastrowid
        self._con.commit()

        try:
            resp = httpx.post(
                self._url + "/api/v1/payments",
                headers={"X-Api-Key": self._key},
                json={"out": True, "bolt11": bolt11},
                timeout=self._timeout)
        except httpx.TimeoutException:
            raise PaymentError(
                "LNbits did not answer in time — the payment is "
                "UNCONFIRMED and may still settle; it remains counted "
                "against the daily cap and is recorded in the ledger")
        if resp.status_code >= 400:
            # A definitive refusal (bad key, insufficient wallet balance,
            # unroutable): nothing was spent, the row goes.
            self._con.execute(
                "DELETE FROM lnbits_spends WHERE id = ?", (row_id,))
            self._con.commit()
            detail = ""
            try:
                detail = str(resp.json().get("detail", ""))[:200]
            except Exception:
                pass
            raise PaymentError(
                f"LNbits refused the payment (HTTP {resp.status_code})"
                + (f": {detail}" if detail else ""))
        payment_hash = resp.json().get("payment_hash", "")

        preimage = self._await_preimage(payment_hash)
        if preimage == "FAILED":
            # The wallet reported the payment definitively failed after
            # accepting it: nothing was spent, the row goes.
            self._con.execute(
                "DELETE FROM lnbits_spends WHERE id = ?", (row_id,))
            self._con.commit()
            raise PaymentError("LNbits reported the payment failed")
        if preimage is None:
            raise PaymentError(
                "LNbits accepted the payment but no preimage is available "
                "yet — it is UNCONFIRMED, recorded in the ledger, and "
                "counted against the daily cap")
        self._con.execute(
            "UPDATE lnbits_spends SET status = 'paid', preimage = ? "
            "WHERE id = ?", (preimage, row_id))
        self._con.commit()
        return preimage

    def _await_preimage(self, payment_hash: str) -> str | None:
        """The preimage once settled, "FAILED" on a definitive failure, or
        None (still unconfirmed) when the deadline passes."""
        import httpx

        if not payment_hash:
            return None
        deadline = time.monotonic() + self._timeout
        while time.monotonic() < deadline:
            try:
                resp = httpx.get(
                    self._url + f"/api/v1/payments/{payment_hash}",
                    headers={"X-Api-Key": self._key}, timeout=10)
                resp.raise_for_status()
                data = resp.json()
            except Exception:
                time.sleep(0.5)
                continue
            fields = _payment_fields(data)
            preimage = str(data.get("preimage")
                           or fields.get("preimage") or "").strip()
            if _is_settled(data) and _PREIMAGE_RE.match(preimage) \
                    and preimage != "0" * 64:
                return preimage
            if fields.get("status") == "failed":
                return "FAILED"
            time.sleep(0.5)
        return None
