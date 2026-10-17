# DITTA in plain terms

*This document is non-normative. Where it and `protocol.md` disagree,
the specification governs.*

## The problem

Indexing is expensive, and the web has one way to pay for it: sell the
searcher's attention. That leaves no room for someone who could index a
niche well, whether a forum, a field of research, a language, or an
archive, because they cannot charge for answering queries against it, and
nobody knows where to send them. DITTA is a wire protocol for that
exchange: a query goes in, a payment goes with it if the indexer charges
one, results come back.

## Two roles

An **indexer** maintains a corpus and answers queries against it.

A **broker** takes a query from a client, decides which indexers to ask,
pays each of them, merges what comes back, and returns results. What a
broker sells is judgment: which indexers are worth paying and how to
combine their answers. The protocol does not check that judgment; the
market does.

Every node publishes a small signed announcement saying where it can be
reached. An indexer's also says what its corpus looks like and what a
query costs; a broker's exists so that indexers can find it. A node can
be both. There is no registry and no privileged node; nodes learn about
each other from announcements, which any node may pass along, or by
asking a peer for the announcements it holds.

## How a paid query flows

1. A client sends a query to a broker.
2. The broker picks indexers from the announcements it holds, by
   corpus, price, language, freshness, reputation, whatever it likes,
   and posts the query to each one.
3. Each indexer that charges responds with HTTP 402 and a Lightning
   invoice; a free indexer answers without one.
4. The broker pays the invoices it is willing to pay and retries each
   query, byte for byte identical, with proof of payment in a header.
5. Each indexer searches and returns a result set.
6. The broker merges, ranks, and returns results to the client.

Payment uses L402, an existing convention for paying for HTTP requests
over Lightning. DITTA adds no payment rail of its own; what it adds is a
signed challenge, so a broker knows which indexer it is paying and that
the credential it gets back is the one that indexer will honour. An
indexer can also sell queries in batches: one payment buys a stated
number of queries, and the broker reuses the credential until it is
spent or expires.

## What holds it together

**Identity is a key, not a certificate.** Every node has a keypair.
Announcements and most other messages are signed, and a node's
reputation attaches to its key. Encryption on the wire is required, but
it only hides traffic; it never says who anyone is.

**One message, one byte sequence.** Every message has a single canonical
serialization, so two implementations in different languages produce and
verify identical signatures. The published test vectors exist to prove
this.

**Strict by default.** Every operational message is rejected on any
unknown field, wrong type, or bad signature. The one exception is the
free announcement, which tolerates unknown fields so the protocol can
grow without a version bump.

**Indexers hold almost no state.** An indexer commits to a price, gets
paid if it charges one, then searches. It holds nothing between the
challenge and the payment; afterwards it remembers only that a credential
was used, or which queries a batch has already served, for as long as
that credential lasts.

## What the protocol leaves alone

DITTA does not say how to index, how to rank, how a broker chooses
indexers, how a node finds its first peer, whether an indexer's claims
about its corpus are true, or what happens when an indexer is paid and
returns nothing. Those are the places operators compete. Version 0 also
makes no attempt to let a client verify that a broker returned what its
indexers sent; the client's trust in its broker is, for now, the same as
its trust in any search engine.

## Where to go next

`protocol.md` is the specification. `vectors/` is how an implementation
checks itself against it. `ditta-wire-py` is the reference implementation
of the wire format.
