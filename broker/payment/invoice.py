"""Invoice minting for the receive side.

The broker's ``/query`` endpoint is paid: an unauthorized query is answered
with a 402 L402 challenge carrying a freshly minted Lightning invoice. Minting
needs only an *invoice-only* credential — a capability that can create
invoices but cannot move money — which is why the receive side is safe to run
on a box the pay side is not (see the deployment notes in the README).

``InvoiceMinter`` is the seam. ``LndInvoiceMinter`` mints against LND's REST
API with an invoice-only macaroon; ``MockInvoiceMinter`` mints deterministic
fake invoices for tests and for local development without a node.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
from abc import ABC, abstractmethod
from pathlib import Path


class InvoiceMinter(ABC):
    @abstractmethod
    def mint(self, amount_msat: int, memo: str) -> tuple[str, str]:
        """Return (payment_hash_hex, bolt11) for a new invoice."""

    def settled(self, payment_hash: str) -> bool:
        """Whether an invoice this minter minted has settled.

        Two things rest on this: the head start (the broker learns of
        payment before the payer presents the preimage) and preimage-less
        redemption (a token we minted redeems on our node's word alone, so
        the web page collects results without asking a person to paste
        anything). Optional — a minter that cannot know returns False, the
        head start never fires, and only a presented preimage redeems.
        """
        return False


class MockInvoiceMinter(InvoiceMinter):
    """A stand-in that mints fake invoices with a recoverable preimage.

    INSECURE BY DESIGN — for tests and offline development only. The preimage
    is embedded in the invoice string so an in-process ``MockPayProvider`` can
    "settle" it without a Lightning network. Never expose this on a real
    endpoint: anyone could read the preimage and forge authorization.
    """

    PREFIX = "lnmock"

    def __init__(self):
        self._settled: set[str] = set()

    def mint(self, amount_msat: int, memo: str) -> tuple[str, str]:
        preimage = secrets.token_bytes(32)
        payment_hash = hashlib.sha256(preimage).hexdigest()
        bolt11 = f"{self.PREFIX}{amount_msat}_{preimage.hex()}"
        return payment_hash, bolt11

    def mark_settled(self, payment_hash: str) -> None:
        """Tests play the Lightning network: this is the settlement event."""
        self._settled.add(payment_hash)

    def settled(self, payment_hash: str) -> bool:
        return payment_hash in self._settled

    @classmethod
    def preimage_of(cls, bolt11: str) -> str | None:
        """Recover the embedded preimage from a mock invoice, or None."""
        if not bolt11.startswith(cls.PREFIX):
            return None
        _, _, tail = bolt11.partition("_")
        return tail or None

    @classmethod
    def amount_of(cls, bolt11: str) -> int | None:
        if not bolt11.startswith(cls.PREFIX):
            return None
        body = bolt11[len(cls.PREFIX):]
        amount, _, _ = body.partition("_")
        return int(amount) if amount.isdigit() else None


class LndInvoiceMinter(InvoiceMinter):
    """Mint invoices against LND's REST API with an invoice-only macaroon.

    The macaroon should be LND's ``invoice.macaroon`` — it can create and
    settle invoices but cannot send payments, so holding it does not put funds
    at spend risk. httpx is imported lazily so the dependency is only needed
    when a real node is configured.
    """

    def __init__(self, rest_url: str, macaroon_path: str,
                 tls_cert_path: str = "", invoice_expiry: int = 3600):
        self._rest_url = rest_url.rstrip("/")
        self._macaroon_hex = Path(macaroon_path).read_bytes().hex()
        self._verify = tls_cert_path or False
        self._expiry = invoice_expiry

    def mint(self, amount_msat: int, memo: str) -> tuple[str, str]:
        import httpx

        resp = httpx.post(
            f"{self._rest_url}/v1/invoices",
            json={"value_msat": str(amount_msat), "memo": memo,
                  "expiry": str(self._expiry)},
            headers={"Grpc-Metadata-macaroon": self._macaroon_hex},
            verify=self._verify, timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        payment_hash = base64.b64decode(data["r_hash"]).hex()
        return payment_hash, data["payment_request"]

    def settled(self, payment_hash: str) -> bool:
        """Look the invoice up on our own node: invoices:read suffices."""
        import httpx

        try:
            resp = httpx.get(
                f"{self._rest_url}/v1/invoice/{payment_hash}",
                headers={"Grpc-Metadata-macaroon": self._macaroon_hex},
                verify=self._verify, timeout=10,
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception:
            return False  # a flaky lookup only delays the head start
        return data.get("state") == "SETTLED" or data.get("settled") is True
