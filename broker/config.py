"""Broker configuration: sane free-mode defaults, overrides via broker.yaml.

The contract with deployments: the repository ships every backend, the config
file activates them, and credentials appear ONLY in the config file (which is
gitignored) — a spend credential is one line of YAML on the host allowed to
spend, and nothing anywhere else.

Free mode needs no Lightning at all: with every entry price 0 (or no receive
section) the broker serves without challenges, and with no pay section it
routes only to free indexers. Turning on real money is a config change:

    receive:                       # take payments at the broker's /query
      pricing:
        - {type: per_query, price: 1000}
      lnd:                         # invoice-only macaroon — cannot spend
        rest_url: "https://<lnd-host>:8080"
        macaroon_path: "/etc/broker/invoice.macaroon"
        tls_cert_path: ""

    pay:                           # make payments to paid indexers
      provider: nwc                # none | mock | nwc
      nwc:
        connection: "nostr+walletconnect://<pubkey>?relay=wss://...&secret=<hex>"
        max_msat_per_payment: 100000
        max_msat_per_day: 500000
        encryption: auto           # auto | nip04 | nip44_v2
"""

from __future__ import annotations

import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class BrokerConfig:
    endpoint: str = "http://127.0.0.1:8410"   # announced base URL
    listen_host: str = "127.0.0.1"
    listen_port: int = 8410
    data_dir: Path = Path("data")
    seeds: tuple = ()             # peer base URLs to announce to and pull from
    fanout: int = 3
    max_results_limit: int = 50   # per-query cap on what the broker will serve
    max_peers_limit: int = 200    # /peers cap when the requester names none
    stats_max_terms: int = 5000   # per-indexer cap on cached /corpus terms
    future_skew: int = 3600
    reannounce_interval: int = 3600
    refresh_interval: int = 600   # how often to re-pull peers from seeds
    request_timeout: float = 20.0
    receive: dict = field(default_factory=dict)
    pay: dict = field(default_factory=dict)
    tor: dict = field(default_factory=dict)
    cards: dict = field(default_factory=dict)   # peer-card rendering policy
    outcomes: dict = field(default_factory=dict)  # outcome store settings
    reachability: dict = field(default_factory=dict)  # searchable policy
    routing: dict = field(default_factory=dict)  # learned-routing policy
    naming: dict = field(default_factory=dict)  # indexer naming (local LLM)
    client: dict = field(default_factory=dict)  # visibility flags, default off
    # The front ends: clients of the /client API, each off unless enabled.
    webclient: dict = field(default_factory=dict)  # the search page
    mcp: dict = field(default_factory=dict)        # the MCP server

    @property
    def web_enabled(self) -> bool:
        return self.webclient.get("enabled") is True

    @property
    def mcp_enabled(self) -> bool:
        return self.mcp.get("enabled") is True

    @property
    def mcp_url(self) -> str | None:
        """Where agents reach the MCP server, for the GET / description:
        ``mcp.public_url`` when set, else its local listen address. None
        when the MCP server is off."""
        if not self.mcp_enabled:
            return None
        public = str(self.mcp.get("public_url") or "")
        if public:
            return public
        host = self.mcp.get("listen_host", "127.0.0.1")
        port = self.mcp.get("listen_port", 8430)
        return f"http://{host}:{port}/mcp"

    @property
    def expose_outcome(self) -> bool:
        """Whether paid responses carry the full outcome record."""
        return self.client.get("expose_outcome") is True

    @property
    def expose_raw_sets(self) -> bool:
        """Whether the outcome includes raw per-indexer result sets."""
        return self.client.get("expose_raw_sets") is True

    def downstream_budget(self, max_msat: int | None) -> int | None:
        """The largest downstream spend whose margined total fits the
        customer's cap — the inverse of ``with_margin``, rounded down."""
        if max_msat is None:
            return None
        budget = max(0, (max_msat - self.margin_flat_msat) * 100
                     // (100 + self.margin_percent))
        while budget > 0 and self.with_margin(budget) > max_msat:
            budget -= 1
        return budget

    @property
    def tor_socks(self) -> str:
        """SOCKS proxy URL for .onion endpoints; empty = refuse onion routes."""
        return self.tor.get("socks") or ""

    @property
    def tor_timeout(self) -> float:
        return float(self.tor.get("timeout") or 60)

    @property
    def key_path(self) -> Path:
        return self.data_dir / "broker.seckey"

    @property
    def entry_pricing(self) -> list[dict]:
        return list(self.receive.get("pricing") or [])

    def entry_price(self, pricing_type: str) -> int | None:
        """The configured price for an entry type: None when the price list
        does not offer that type, and 0 when it does but sets no price —
        a price is never charged unless it is written down."""
        for opt in self.entry_pricing:
            if opt.get("type") == pricing_type:
                return int(opt.get("price") or 0)
        return None

    @property
    def pricing_mode(self) -> str:
        """``static`` prices entry from the config's pricing list; ``quoted``
        prices each query from routing over the cached network — the same
        computation the /quote endpoint shows before the button is pressed —
        plus the configured margin."""
        return (self.receive.get("pricing_mode") or "static").lower()

    @property
    def margin_flat_msat(self) -> int:
        return int(self.receive.get("margin_flat_msat", 0))

    @property
    def margin_percent(self) -> int:
        return int(self.receive.get("margin_percent", 0))

    def with_margin(self, downstream_msat: int) -> int:
        """The quoted entry total: downstream estimate plus margin."""
        return (downstream_msat
                + downstream_msat * self.margin_percent // 100
                + self.margin_flat_msat)

    @property
    def entry_charges(self) -> bool:
        if not self.receive:
            return False
        if self.pricing_mode == "quoted":
            return True  # any given query may price above zero
        return any(int(opt.get("price") or 0) > 0
                   for opt in self.entry_pricing)


def load_config(path: str | os.PathLike | None = None) -> BrokerConfig:
    import yaml

    path = path or os.environ.get("BROKER_CONFIG", "broker.yaml")
    p = Path(path)
    raw = (yaml.safe_load(p.read_text()) or {}) if p.exists() else {}
    kwargs = dict(raw)
    if "data_dir" in kwargs:
        kwargs["data_dir"] = Path(kwargs["data_dir"])
    if "seeds" in kwargs:
        kwargs["seeds"] = tuple(kwargs["seeds"])
    return BrokerConfig(**kwargs)


# --- factories: config -> payment machinery ---------------------------------

def _l402_secret(data_dir: Path) -> bytes:
    """The HMAC secret for minted L402 tokens, generated once and persisted.

    Deliberately not the node identity key: losing or rotating one must not
    affect the other.
    """
    path = data_dir / "l402.secret"
    if path.exists():
        return bytes.fromhex(path.read_text().strip())
    path.parent.mkdir(parents=True, exist_ok=True)
    secret = secrets.token_bytes(32)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(secret.hex())
    return secret


def build_receiver(cfg: BrokerConfig):
    """The entry L402 receiver, or None when the broker charges nothing."""
    if not cfg.entry_charges:
        return None
    from .payment import L402Receiver, LndInvoiceMinter, MockInvoiceMinter

    lnd = cfg.receive.get("lnd") or {}
    lnbits = cfg.receive.get("lnbits") or {}
    if cfg.receive.get("minter") == "mock":
        minter = MockInvoiceMinter()   # tests/dev only; forgeable on purpose
    elif lnbits.get("url") and lnbits.get("invoice_key"):
        from .payment.lnbits import LnbitsInvoiceMinter

        minter = LnbitsInvoiceMinter(
            lnbits["url"], lnbits["invoice_key"],
            invoice_expiry=lnbits.get("invoice_expiry", 3600))
    elif lnd.get("rest_url") and lnd.get("macaroon_path"):
        minter = LndInvoiceMinter(
            lnd["rest_url"], lnd["macaroon_path"],
            lnd.get("tls_cert_path", ""),
            invoice_expiry=lnd.get("invoice_expiry", 3600))
    else:
        raise ValueError(
            "entry pricing is enabled (non-zero static price, or "
            "pricing_mode: quoted) but no minter is configured — set "
            "receive.lnbits (url + invoice_key) or receive.lnd "
            "(rest_url + macaroon_path); a receive-only credential is "
            "required to charge at the door")
    return L402Receiver(
        secret=_l402_secret(cfg.data_dir),
        db_path=cfg.data_dir / "l402_tokens.db",
        minter=minter,
        token_ttl=cfg.receive.get("token_ttl", 86400))


def build_pay_provider(cfg: BrokerConfig):
    """The spend backend named by config; NullPayProvider when absent."""
    from .payment import MockPayProvider, NullPayProvider

    provider = (cfg.pay.get("provider") or "none").lower()
    if provider == "none":
        return NullPayProvider()
    if provider == "mock":
        return MockPayProvider()
    if provider == "nwc":
        from .payment.nwc import NwcPayProvider

        nwc = cfg.pay.get("nwc") or {}
        connection = (nwc.get("connection") or "").strip()
        if not connection:
            raise ValueError("pay.provider is nwc but pay.nwc.connection "
                             "is not set")
        return NwcPayProvider(
            connection, cfg.data_dir,
            max_msat_per_payment=nwc.get("max_msat_per_payment", 100_000),
            max_msat_per_day=nwc.get("max_msat_per_day", 500_000),
            encryption=str(nwc.get("encryption", "auto")),
            timeout=nwc.get("timeout", 30))
    if provider == "lnbits":
        from .payment.lnbits import LnbitsPayProvider

        lnbits = cfg.pay.get("lnbits") or {}
        if not (lnbits.get("url") and lnbits.get("admin_key")):
            raise ValueError("pay.provider is lnbits but pay.lnbits.url "
                             "and pay.lnbits.admin_key are not both set")
        return LnbitsPayProvider(
            lnbits["url"], lnbits["admin_key"], cfg.data_dir,
            max_msat_per_payment=lnbits.get("max_msat_per_payment", 100_000),
            max_msat_per_day=lnbits.get("max_msat_per_day", 500_000),
            timeout=lnbits.get("timeout", 30))
    raise ValueError(
        f"unknown pay.provider {provider!r} (none | mock | nwc | lnbits)")
