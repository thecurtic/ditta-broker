# Protocol Explainer Page — Broker Feature Spec

**Status:** implemented at `/protocol` (October 2026). Claims checked against `spec/protocol.md` at e5aa7f8.
**Scope:** one page on the broker web front end. No back-end changes beyond read-only use of data the broker already holds.

This page shows and explains how the DITTA protocol works. It is what gets shared with people who might build against the protocol or use it. Its readers are deciding whether DITTA is worth their time, so it has to be accurate, plain and quick to understand.

How the page looks follows `DESIGN.md`. If this spec contradicts `DESIGN.md`, another spec, or a task prompt, flag the conflict and ask which is intended.

---

## 1. Principles

1. **The specification governs.** Every claim on the page must match `protocol.md` in the `ditta-spec` repo. If the page and the spec disagree, the spec is right and the page is wrong. When writing a section, check it against the spec, not against memory or this document.
2. **Explain, don't specify.** The page is non-normative. No MUST, SHOULD or MAY. It links to the spec for anything exact.
3. **Show the real network.** Where possible, illustrate with real data from this broker (a real announcement, real indexers) rather than invented examples.
4. **Honest about the stage.** Say plainly that version 0 is a draft and unstable, and say what the protocol does not do yet.
5. **Portable.** The explanation may later move to the `ditta-spec` repo. Keep the protocol explanation separate from anything specific to this broker, so it can be lifted out with the live panels dropped.
6. **No hype.** No "revolutionary", no comparisons to named search engines, no roadmap promises.

## 2. Where it lives

- Route: `/protocol`. Page title: "How DITTA works".
- Linked from the home page near the Relay figure, and from the site footer.
- One long page with in-page anchors for each section, so a link to a single section can be shared.

## 3. Content

Sections in this order. Each is short: a heading, a few sentences or a short list, and where noted a figure or a live panel. Wording below is guidance on what to cover, not final copy.

### 3.1 Opening

One or two sentences: DITTA is an open protocol for federated web search. Independent operators index what they know best and answer queries, optionally for payment over Lightning, with no central party. Note that version 0 is a draft and unstable. Relay figure.

### 3.2 The problem

Indexing is expensive and nobody pays the people who do it. Anyone can build a better index of a niche than a general engine, but there's no way to be paid for answering queries against it and no way for searchers to find it.

### 3.3 Two roles

- An **indexer** maintains a corpus, answers queries against it, and publishes a signed announcement: where it can be reached, what its corpus looks like, what a query costs.
- A **broker** takes a query, decides which indexers to ask, pays them, merges the results and returns them. What a broker sells is judgment.
- A node can be both. There is no registry and no privileged node; nodes learn about each other from announcements.

Make clear that this site is a broker, and that the reader is looking at one broker's view of the network, not the network.

### 3.4 How a query flows

The six steps: client sends query to broker; broker picks indexers from announcements it holds; a charging indexer replies with HTTP 402 and a signed payment challenge carrying a Lightning invoice, a free one just answers; broker pays and retries the identical query with proof of payment; indexer returns a result set; broker merges, ranks and returns results.

This section is the centrepiece. Illustrate it as a sequence of figures, one per step or per pair of steps. Payment uses L402, an existing convention; DITTA adds no payment mechanism of its own. Paying is optional: an indexer may charge nothing.

### 3.5 What holds it together

- **Identity is a key.** Every node has a keypair; messages are signed (BIP-340 Schnorr). Reputation attaches to the key. TLS encrypts but never says who anyone is.
- **One message, one byte sequence.** Every message has a single canonical serialization, so implementations in different languages produce and verify identical signatures. Published test vectors prove it.
- **Strict by default.** Messages that move money are rejected on any unknown field, wrong type or bad signature. Announcements tolerate unknown fields so the protocol can grow.
- **Indexers can be stateless.** An indexer commits to a price, gets paid, then searches.

### 3.6 What the protocol leaves alone

How to index, how to rank, how a broker chooses indexers, how a node finds its first peer, what happens when a paid indexer returns nothing. These are where operators compete. Also say that version 0 doesn't let a client verify that a broker returned what its indexers actually sent.

### 3.7 Live: this broker's view of the network

A live panel, clearly labelled as this broker's view:

- How many indexers this broker currently knows and how many are reachable, with a link to the network page.
- One real, current indexer announcement, shown as its JSON with the signature, pubkey in full. A short caption explains the main fields and links to the announcement section of the spec.
- If the broker can't load live data, the panel says so in one line. The rest of the page doesn't depend on it.

### 3.8 Build against it

For people who want to implement or use it:

- The specification (`protocol.md` in `github.com/ditta-protocol/ditta-spec`) and its change log.
- The test vectors, and what they're for: checking an implementation byte for byte.
- A Python implementation of the wire format, `github.com/ditta-protocol/ditta-wire-py`, usable as a reference for the serialization and signatures. Describe it as *a* wire implementation, not "the reference implementation", and don't present this broker as the reference: this broker is an independent, clean-room implementation built from the spec and vectors.
- This broker's agent API docs, for people who want to search the network programmatically rather than implement the protocol.
- How to run an indexer is out of scope for now; say that the spec is the starting point.

Links point to the `ditta-protocol` GitHub org: `ditta-protocol/ditta-spec` (spec + vectors) and `ditta-protocol/ditta-wire-py` (Python wire implementation). Verify both resolve before publishing.

## 4. Figures

This is the one page where figures carry the content. Every figure is ours (DESIGN.md §5): labeled diagrams with one cast. The opening uses the search page's `fanout`; two roles uses `announce` (indexers announcing to each other and to a broker, which keeps a profile of each); the six steps use `step1`–`step6`. Payments are a lightning bolt with an amount in sats; invoices are cards with the amount, a QR and the indexer's signature.

Every figure on this page gets a text alternative describing what it shows. Figures follow `DESIGN.md` for size, theming and motion. Only the opening and the step figures move, and none under reduced motion.

## 5. Non-goals

- Restating the specification. Anything exact (field names, encodings, error codes) links to the spec.
- Operator documentation: how to run a broker or indexer.
- Marketing, roadmaps, or comparisons to other search engines.
- Any change to the DITTA wire protocol.

## 6. Open questions

- Repo names and URLs are settled: `ditta-protocol/ditta-spec` and `ditta-protocol/ditta-wire-py`. Still verify both resolve at publish time. (Checked 2026-10-10: both resolve; the vectors live in the spec repo's `vectors/` directory, not a `vectors.json`.)
- The live panel needed one read-only broker endpoint, `GET /client/announcements` (the broker's own pubkey and endpoint, and the signed announcements it already holds). The broker verified each one when it arrived; the web client shows them unchanged and does no protocol work of its own.
- Whether a public contact or discussion channel should be linked from 3.8.

## 7. Acceptance criteria

1. Every section in 3 is present, in order, with its own anchor.
2. Each claim in 3.2–3.6 has been checked against the current `protocol.md`, and nothing on the page goes beyond it.
3. The page says version 0 is a draft and unstable.
4. The live panel shows a real announcement from this broker and degrades to a one-line notice when data is unavailable.
5. Every link in 3.8 resolves.
6. Every figure has a text alternative.
7. The page passes the visual review in `DESIGN.md` section 11.
