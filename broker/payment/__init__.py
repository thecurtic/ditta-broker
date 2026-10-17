"""Payment layer: receive (take payments) and pay (make payments).

The two halves need very different credentials and carry very different risk,
so they are cleanly separated:

- receive: ``L402Receiver`` + an ``InvoiceMinter`` (invoice-only credential —
  cannot move money; safe on a development box).
- pay: a ``PayProvider`` (spend credential — the crown jewel; disabled by
  default). The repository ships every backend, including the real NWC one
  (``broker.payment.nwc``) and the wallet-scoped LNbits one
  (``broker.payment.lnbits``); credentials appear only in a deployment's
  config.
"""

from .invoice import InvoiceMinter, LndInvoiceMinter, MockInvoiceMinter
from .l402 import L402Receiver
from .pay import (
    MockPayProvider,
    NullPayProvider,
    PayProvider,
    PaymentCapExceeded,
    PaymentDisabled,
    PaymentError,
)

__all__ = [
    "InvoiceMinter",
    "LndInvoiceMinter",
    "MockInvoiceMinter",
    "L402Receiver",
    "PayProvider",
    "NullPayProvider",
    "MockPayProvider",
    "PaymentError",
    "PaymentDisabled",
    "PaymentCapExceeded",
]
