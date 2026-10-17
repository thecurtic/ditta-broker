# DITTA-broker

An independent broker for [DITTA](spec/protocol.md), the federated web
search protocol with Lightning payment (version 0, published at
[ditta-protocol/ditta-spec](https://github.com/ditta-protocol/ditta-spec)).

In DITTA, **indexers** hold corpora and answer queries for a price, and
**brokers** are the entry points people search through. A broker takes a
query, decides which indexers to ask, **pays them**, merges what comes back,
and returns one ranked list. What a broker sells is judgment: which indexers
to ask and how to combine their answers.

This broker:

- speaks the DITTA v0 wire protocol, built from the specification alone and
  passing the full conformance vector set;
- **takes payments** at its own door (L402 over Lightning) and **makes
  payments** to the indexers it routes to, with the two halves configured
  separately and spending off by default;
- learns which indexers deliver on which terms, from what they actually
  returned;
- ships a web client: a search page that shows the payment flow and the
  route each query took, and a `/protocol` page that explains DITTA.

Everything runs in free mode with no Lightning node at all.

## Quick start

Python 3.10 or newer.

```sh
git clone https://github.com/thecurtic/ditta-broker.git && cd ditta-broker
python3 -m venv .venv && . .venv/bin/activate
pip install -e '.[dev]'
python3 -m pytest -q
```

### See it working (no network, no Lightning)

```sh
python3 tests/demo_stack.py            # free entry
python3 tests/demo_stack.py --paid     # with a mock Lightning paywall
```

This starts a broker and the web client on loopback, wired to four
in-process indexers with mixed health (one Tor-only, one unreachable). It
prints the page URL and serves until interrupted. In `--paid` mode the
invoices are mock invoices (`lnmock<msat>_<preimage>`): nothing can pay
them, so to complete a search, copy the part after the underscore and paste
it as the preimage under "Paid, but nothing happened?".

### Run a real broker

```sh
pip install -e '.[node,web]'           # add ,nwc and/or ,tor as needed
cp broker.yaml.example broker.yaml     # then edit: endpoint, seeds
python3 -m broker broker.yaml          # the broker (protocol + /client API)
python3 -m broker.webclient broker.yaml   # the web client, a second process
```

The broker listens on `127.0.0.1:8410` and the web client on
`127.0.0.1:8420` by default. On first start the broker creates `data/`,
holding its identity key (`broker.seckey`), its L402 token secret, and its
SQLite stores. Keep that directory: the key *is* the broker's identity on
the network.

To join a network, set `endpoint` to the public `https://` URL other nodes
reach you at (the spec requires TLS on the public internet, so put a reverse
proxy in front) and list at least one existing node under `seeds`. The
broker announces itself, pulls announcements, and learns the rest of the
network by gossip.

### Install extras

| Extra | Adds | Needed for |
|---|---|---|
| `node` | httpx, PyYAML, coincurve | running the broker |
| `web` | httpx, PyYAML, segno, coincurve | running the web client (segno draws invoice QR codes) |
| `nwc` | websockets, cryptography | the Nostr Wallet Connect pay backend |
| `tor` | httpx[socks] | reaching `.onion` indexers |
| `dev` | pytest and everything above | the test suite |

The wire layer itself uses only the standard library. `coincurve` is an
optional fast path for signatures; the pure-Python BIP-340 implementation is
the reference and the fallback.

## Configuration

Everything lives in one YAML file. [`broker.yaml.example`](broker.yaml.example)
documents every section; copy it and uncomment what you need. Credentials
appear only in that file (gitignored), never in the repository.

| Section | Absent means | Present means |
|---|---|---|
| `receive` | searches are free | searches can be paid: L402 invoices minted on your LND or LNbits, `static` or `quoted` pricing. Every price and margin defaults to 0, so only prices you set are charged |
| `pay` | only free (price 0) indexers are used | paid indexers are used, paid from LNbits or NWC under local caps |
| `tor` | `.onion` indexers are refused, never resolved | `.onion` traffic goes through your Tor SOCKS proxy |
| `naming` | indexers are shown by pubkey | an OpenAI-compatible endpoint names and describes each indexer |
| `routing` | learned routing with default tuning | tuning, or `mode: coverage` for the simpler baseline |
| `client` | the per-route outcome record stays private | responses carry it, so the page can draw the route map and receipts |
| `webclient` | the web client uses its defaults | listen address, the broker's URL, page title, and the shared `site_secret` |

Naming settings can also come from the environment
(`BROKER_NAMING_ENDPOINT`, `BROKER_NAMING_MODEL`, `BROKER_NAMING_API_KEY`);
see [`.env.example`](.env.example).

## Endpoints

**The broker** (default port 8410):

| Path | |
|---|---|
| `GET /` | a JSON description of this broker: pubkey, pricing, and every path below |
| `POST /announce`, `POST /peers` | DITTA protocol paths: gossip between nodes |
| `POST /query`, `POST /corpus` | DITTA protocol paths, answered `rejected`: on the wire a broker sends queries, it does not answer them |
| `POST /client/search` | search. Body: `{query, request_id, n?, max_msat?, exclude_tor?, pubkeys?}` |
| `GET /client/quote` | what a search would cost and which indexers it would ask, before it is sent |
| `GET /client/peers` | every indexer: pubkey, name, description, pricing, reachability, characteristic terms |
| `GET /client/announcements` | the signed indexer announcements this broker holds, as received |
| `GET /client/status` | whether a challenged invoice has settled on this broker's node |

The `/client` API is this broker's product API, for the web client and for
agents. It is deliberately not a DITTA protocol surface, since the spec
leaves the client-to-broker hop out of scope; keeping it off the protocol
paths is what keeps those paths byte-for-byte conformant.

A paid search works like this: an unpaid `POST /client/search` gets HTTP 402
with an L402 challenge header and a body that is a **signed payment
challenge** (invoice, macaroon and expiry, signed with the broker's identity
key, so you can check who you are paying). Pay the invoice, then retry the
byte-identical body with `Authorization: L402 <macaroon>:<preimage>`. Every
result carries `sources[]` naming the indexers that returned it, and every
response states the scope actually queried and what was charged.

**The web client** (default port 8420) serves the search page at `/` and the
protocol explainer at `/protocol`, and talks to the broker's `/client` API.
People use the web client; the broker's own port is for other nodes and
for agents.

## How it works

### Independence

The broker is built against the **protocol specification and its
conformance vectors alone**, sharing no code with any other implementation.
The spec and vectors are vendored under [`spec/`](spec/) at the revision they
were taken from (`e5aa7f8`, the initial publication of version 0), and the
broker reaches into no other project at build or run time.

The spec's own architecture notes name *"an independent implementation from
the document alone, checked against these vectors"* as the highest-value
step remaining for the protocol. This broker's wire layer is that, and it
passes the full vector set:

```
python3 -m pytest tests/test_conformance.py -q
# 31 valid vectors accepted and re-derived (canonical bytes, tagged hash, signature)
# 59 invalid vectors rejected, each with its declared code
```

Interoperability has also been checked live against an independent
implementation: this broker pulled the peer's announcement, exchanged signed
messages in both directions, and routed a paid query end to end.

### The pipeline

A query passes through three stages, each behind a one-sentence contract:

- **Routing** takes a query and corpus statistics and returns a ranked list
  of indexers. It ranks on merit and never reads a price. This is the seam
  where a broker's own judgment lives.
- **Economics** takes that list, per-indexer prices and a spending ceiling,
  and returns who to ask and how many results from each. It sets the cutoff
  but does not reorder merit.
- **Merge** takes the result sets and returns one ordered list. It owns
  de-duplication (by normalized URL, across indexers) and never sees a price.

The coupling direction is enforced by test, not convention:
`tests/test_modules.py` swaps each stage for a deliberately stupid
alternative and proves nothing else degrades, and mutates prices to prove
routing's ranking is byte-identical.

Outside the stages: **payment** wraps the entrance and executes each spend
economics authorized; **discovery** (`peers.py`, `gossip.py`) is a shared
producer both routing and economics read; and the **outcome store** records
what every route did (one row per indexer per query: who was asked, what
they returned, what it cost, what survived the merge). The learner and the
indexer cards are read-models over that store, not pipeline stages.

### Learned routing

By default (`routing.mode: learned`) routing scores each indexer by
query likelihood over its term densities, from the `/corpus` statistics it
publishes, smoothed by a general-English prior. The learner then adjusts
those densities by what the outcome store shows each indexer actually
delivered for each term, and by its calibration and reliability.

Selection is a threshold, not a quota: an indexer trailing the best score by
more than `skip_odds` is not asked, so a niche query can go to the niche
corpus alone. Randomness (Thompson sampling, the exploration floor) is
seeded per query and hour, so a quote and its invoice always agree. Results
are merged by evidence: source affinity, query terms present in the result
itself, and agreement across sources. Everything paid for is returned, with
a confidence divider where the broker stops believing.

Clicks on the web page feed the learner in aggregate only. A click records
the request id and URL and nothing that identifies the person.

### Indexer names and selection

An indexer's identity is its pubkey. With `naming` configured, the broker
also gives each indexer a short human name, a factual description, and its
own assessment, generated from the indexer's statistics and delivery history
by a local OpenAI-compatible model. Names are display labels only: they
never enter a DITTA message and never influence routing, and the broker
keeps each pubkey's name history.

Searchers may narrow the set of indexers a search is sent to, by pubkey
(`pubkeys` on `/client/search` and `/client/quote`, or "Search this" on the
page). Within that set the broker alone routes, ranks and merges. A scope is
never widened: if nothing in it can be asked, the search is refused before
any invoice is minted, with each indexer's reason.

### Payments: receive and pay are separate on purpose

The two halves need very different credentials and carry very different
risk, so they are independent and the risky one is off by default.

| | Credential | Can it move money? | Default |
|---|---|---|---|
| **Receive** (`L402Receiver` + invoice minter) | invoice-only LND macaroon, or an LNbits invoice key | **No**, it only mints invoices | off (free) |
| **Pay** (`PayProvider`) | an LNbits admin key or an NWC connection URI | **Yes** | off |

**Receive.** An unpaid search gets a 402 carrying a freshly minted invoice
and a token (an HMAC-authenticated macaroon binding the payment hash, the
price, an expiry, and the sha256 of the exact request bytes). The retry
redeems once the preimage proves payment, so a rebuilt request cannot
redeem. Credentials are consumed by service, not presentation: if no
indexer answers, the credential survives for a retry. With `head_start`,
the broker watches its own node and starts routing the moment the invoice
settles, before the payer comes back. Pricing is `static` (a configured
price) or `quoted` (routing evaluated over the cached network, plus your
margin; `/client/quote` runs the same computation). Unset prices and
margins are 0, and a search that totals 0 is served without an invoice.

The broker's own web client may redeem without the preimage: it shares a
`site_secret` with the broker, and a request carrying that secret can leave
the preimage slot empty and redeem once the broker's node reports the
invoice settled, so a person pays and the results simply arrive. Agents and
protocol peers always present the preimage. See
[DECISIONS.md](DECISIONS.md) for why this is safe.

**Pay.** The broker pays the invoices indexers challenge it with, and only
after verifying the 402's signed payment-challenge body against the
announcement it routed on. An unsigned, mismatched or impostor challenge is
never paid, and the route continues without that indexer. The default
provider refuses every payment: the broker still gets paid, still routes to
free indexers, and never holds a spend credential. Two real backends ship:

- **LNbits** (recommended): the broker gets one wallet. The admin key spends
  from that wallet's pre-funded balance alone, so the blast radius of the
  spend credential is one wallet, not your node.
- **NWC** (Nostr Wallet Connect, NIP-47): one connection URI, with the
  wallet's own budget enforced on its side.

Both apply local per-payment and per-day caps and write every attempt to a
ledger before contacting the wallet.

**Where the spend credential lives.** Keep it off any host where untrusted
or autonomous code runs. The code is identical everywhere; only the `pay`
section differs. A development or test host runs with no `pay` section (it
can still develop, test, receive and route to free indexers), and a separate
host that is allowed to spend holds the one credential.

### The web client

The search page is built to show the protocol's machinery. As you type,
the button shows a live quote in sats. A paid search shows the invoice as a
QR code; once it is paid the page replays the original request bytes and the
results appear. Results can be viewed as a ranked list or as a route map
(the broker, every indexer it considered, and which results came from
whom), with a receipt of what was paid. Indexer cards show each indexer's
name, description, reachability, characteristic vocabulary, and which of
its claimed terms it has actually delivered on.

Answered searches are kept in the browser's localStorage only (the last 20,
deletable). The URL names a search by an opaque id, never the query text.
The broker keeps no searcher-facing log.

Each page is assembled at startup from real files in `broker/web/` into a
single response, with no build step and a strict Content Security Policy.
Nothing loads at runtime from a third party, and every figure is a labeled
diagram drawn by `figures.js`.

## Repository layout

```
spec/                 vendored DITTA v0 specification + conformance vectors (CC0)
broker/
  wire/               the protocol wire format: clean-room, stdlib only
    parse.py            strict JSON parsing
    canonical.py        canonical serialization (UTF-8 byte-order keys)
    bip340.py           BIP-340 Schnorr signatures + tagged hash
    validate.py         per-message validation, closed shapes, signature policy
  identity.py         the node keypair (x-only secp256k1) and message signing
  messages.py         builders for the messages the broker originates
  peers.py            the announcement store: supersession, future-skew
  gossip.py           announce to seeds, pull peers and corpus statistics
  reachability.py     "searchable" means recently answered
  stats.py            cached per-term /corpus statistics; card vocabulary
  routing.py          stage 1: merit rank (price-blind)
  economics.py        stage 2: who to ask and how many, within the ceiling
  merge.py            stage 3: result sets -> one ordered list (owns dedupe)
  learner.py          learned densities over the outcome store (deletable)
  outcomes.py         the outcome record and its store
  naming.py           indexer names and descriptions via a local model
  prefetch.py         the head start: route as soon as the invoice settles
  client.py           outbound: the L402 402 -> verify -> pay -> retry loop
  node.py             the pipeline caller; payment wraps the entrance
  server.py           the broker's HTTP face: protocol paths + /client API
  config.py           YAML config and payment factories
  webclient.py        the web client process: search page and /protocol
  web/                the pages' HTML/CSS/JS, figures.js, IBM Plex fonts
  payment/
    invoice.py          invoice minters: LND (invoice-only) | mock
    l402.py             L402Receiver: challenge and redeem
    lnbits.py           LNbits minter and pay provider
    nwc.py              NWC pay provider
    pay.py              PayProvider: null (default) | mock
tests/                conformance, pipeline, payments, server, web client
  demo_stack.py         a local demo network (not a test module)
docs/                 feature specs (below)
DECISIONS.md          design decisions not visible from the code
AGENTS.md             instructions for coding agents (CLAUDE.md links here)
```

## Documentation

- [`spec/OVERVIEW.md`](spec/OVERVIEW.md): DITTA in plain terms. Start here
  if the protocol is new to you; [`spec/protocol.md`](spec/protocol.md) is
  the normative text.
- [`DECISIONS.md`](DECISIONS.md): why the broker is built the way it is,
  including choices that were reversed and why.
- [`docs/search-and-selection.md`](docs/search-and-selection.md): search
  behaviour, indexer selection, spend, every loading and failure state, and
  the agent-facing API.
- [`docs/indexer-naming-spec.md`](docs/indexer-naming-spec.md): how indexers
  are named and described.
- [`docs/explainer-page.md`](docs/explainer-page.md): the `/protocol` page.
- [`docs/DESIGN.md`](docs/DESIGN.md): the web front end's visual design
  contract.

## Security notes

- Every string in a result set is attacker-controlled display content.
  Escape it for its render context and treat result URLs as data, never as
  something to dereference. The routing layer keeps them opaque.
- The broker never pays a 402 whose signed body does not verify against the
  announcement it routed on.
- `.onion` names are never resolved or dialed without a Tor proxy
  configured, since a DNS lookup would leak them.
- `data/` holds the identity key and L402 secret with owner-only
  permissions. Back it up; do not commit it.

## Status

Version 0.0.1. Built and tested: the conformant wire layer, identity and
signing, discovery and gossip, the three-stage pipeline with learned routing
and evidence-based merge, the outcome store, reachability, indexer naming,
scoped search, the receive side (LND and LNbits minters, quoted and static
pricing, the head start), the pay side (LNbits and NWC), Tor support, and the
web client with the `/protocol` page.

The test suite covers conformance, supersession, the module swap and
direction probes, the HTTP faces over real sockets (including the paid
entrance: 402, pay, byte-identical retry, single-use token), and the NWC
backend end to end against an in-process wallet over a real websocket relay.

## License

MIT; see [LICENSE](LICENSE). The vendored specification under `spec/` is
CC0. The IBM Plex fonts under `broker/web/fonts/` are under the SIL Open Font
License, included beside them.
