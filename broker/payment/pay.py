"""Pay side: settling the invoices indexers challenge the broker with.

This is the spend-capable half of the broker, and the credential it needs is
the crown jewel — an NWC connection secret or an LND payment macaroon can move
real money. So the pay side is a swappable provider, DISABLED BY DEFAULT.

- ``NullPayProvider`` — the default. Every attempt to pay is refused. A broker
  running this can still be paid, route to free (price-0) indexers, and serve
  everything but paid downstream queries. This is what runs on a development
  box, or anywhere the spend credential must not live.
- ``MockPayProvider`` — settles ``MockInvoiceMinter`` invoices in-process, for
  tests and offline development. Moves no real money.
- ``NwcPayProvider`` / ``LndPayProvider`` — real spend backends. Deliberately
  not implemented in this module: they belong to a deployment that holds the
  spend credential, wired in where that credential lives, never in the repo.

A provider's ``pay`` returns the payment preimage on success, which the client
turns into an ``Authorization: L402 <macaroon>:<preimage>`` retry.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from .invoice import MockInvoiceMinter


class PaymentError(Exception):
    """A payment could not be completed."""


class PaymentDisabled(PaymentError):
    """The pay capability is intentionally not available on this host."""


class PaymentCapExceeded(PaymentError):
    """A local spending cap would be exceeded by this payment."""


class PayProvider(ABC):
    #: Whether this provider can actually move money. Routing consults it to
    #: decide whether to attempt paid indexers at all.
    can_pay: bool = False

    @abstractmethod
    def pay(self, bolt11: str, max_amount_msat: int) -> str:
        """Pay a BOLT11 invoice, returning the preimage hex.

        ``max_amount_msat`` is the caller's ceiling for this payment (the
        query's max price); a provider MUST refuse to pay more. Raises a
        ``PaymentError`` subclass on any failure.
        """


class NullPayProvider(PayProvider):
    can_pay = False

    def pay(self, bolt11: str, max_amount_msat: int) -> str:
        raise PaymentDisabled(
            "pay capability is disabled on this host; configure a pay "
            "provider on a spend-capable deployment to route to paid indexers")


class MockPayProvider(PayProvider):
    """Settles mock invoices in-process. Test/dev only — moves no real money.

    Enforces an optional total spend cap so tests can exercise cap handling.
    """

    can_pay = True

    def __init__(self, max_total_msat: int | None = None):
        self._max_total = max_total_msat
        self.total_spent_msat = 0

    def pay(self, bolt11: str, max_amount_msat: int) -> str:
        preimage = MockInvoiceMinter.preimage_of(bolt11)
        if preimage is None:
            raise PaymentError("not a mock invoice this provider can settle")
        amount = MockInvoiceMinter.amount_of(bolt11) or 0
        if amount > max_amount_msat:
            raise PaymentCapExceeded(
                f"invoice is {amount} msat, over the {max_amount_msat} ceiling")
        if self._max_total is not None \
                and self.total_spent_msat + amount > self._max_total:
            raise PaymentCapExceeded("provider total spend cap exceeded")
        self.total_spent_msat += amount
        return preimage
