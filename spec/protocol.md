# DITTA Federated Web Search Protocol

**Version:** 0
**Status:** draft

Version 0 is unstable. Any part of this document may change without notice, and implementations built against it should expect to be broken by later revisions.

This is a living document, revised in place. Every revision is recorded in the change log below, which states whether the wire contract changed. The protocol version integer moves only when a revision breaks the meaning of something already defined; advisory guidance, non-normative text, and operational numbers are revised without a version change (see Extensibility).

## Change log

- 2026-09-13: initial publication of version 0. Wire contract: established.

## Abstract

This document specifies a protocol for a federated search network. Independent operators maintain their own document corpora and answer queries against them, optionally for payment. Other participants collect queries, distribute them across a set of operators of their choosing, and return merged results.

The protocol defines how participants announce themselves, how they describe their corpora, how queries and results are exchanged, and how those exchanges are paid for. It does not define how documents are indexed, how results are ranked, or how a query is routed.

## Terminology

The key words MUST, MUST NOT, REQUIRED, SHALL, SHALL NOT, SHOULD, SHOULD NOT, RECOMMENDED, MAY, and OPTIONAL in this document are to be interpreted as described in RFC 2119.

**Node**: any participant in the network.

**Broker**: a role a node plays when it distributes queries to indexers and merges their results. On the wire a broker announces, serves peer lists, and sends queries; it does not answer them.

**Indexer**: a role a node plays when it answers queries against a corpus it maintains.

**Corpus**: the set of documents an indexer has indexed.

**Client**: software that submits a query to a broker. Clients are outside the scope of this document.

Roles are self-declared and are not verifiable on the wire.

## Architecture

*This section is non-normative.*

Every node may run both roles. There is no central registry and no privileged node. Nodes learn about each other from announcements, which any node may relay to its peers, and by asking a peer directly for the announcements it holds (see Peer list request). How a node finds its first peer, the bootstrap problem, is out of scope for this document; the protocol begins once a node has someone to announce to.

Entry points to the network are brokers. A client submits a query to a broker; the broker decides which indexers to ask, pays them, merges what comes back, and returns results to the client. What a broker sells is judgment: which indexers to ask, and how to combine their answers. The protocol does not attempt to verify that judgment.

Discovery runs in both directions. A broker collects announcements to decide where to route; an indexer that wants a broker's traffic announces itself to that broker directly, or asks it for the announcements it holds. A broker's announcement exists so that indexers can find it: it publishes the pubkey and endpoint that make announcing to it possible. A declared role is a claim the node makes about itself, checked only by experience and reputation (see RATIONALE.md, "Architecture: self-declared roles").

## Roles

A node declares its roles in an announcement. Two values are defined:

- `broker`: the node distributes queries to indexers.
- `indexer`: the node answers queries against a corpus it maintains.

A node MAY declare both. It MUST declare at least one, and MUST NOT declare the same role twice. This list is closed for version 0; a node receiving an unrecognized role value MUST reject the message.

A node declaring `indexer` MUST include a corpus summary in its announcement, and MUST advertise at least one pricing option (see Pricing types). A node not declaring `indexer` MUST NOT include a corpus summary and MUST carry an empty pricing array. The pricing array is present in every announcement; the roles decide whether it is populated.

## Pricing types

An indexer advertises one or more pricing options. Each option names a type and a price in integer millisatoshis. A query names one of them (see Query request).

A price of zero is conformant: an indexer serving queries free of charge advertises a price of 0, never an empty pricing array. A query the indexer serves free of charge is answered without payment: the indexer MUST NOT issue a payment challenge for it, and no credential is involved (see Transport). A `per_batch` option priced at zero is conformant but buys nothing a free `per_query` option does not, since there is no credential under which to count queries.

- `per_query`: the price is charged once per query, regardless of how many results are returned.
- `per_result`: the price is charged per result. The invoiced amount is the per-result price multiplied by the query's max result count, which also bounds the results returned (see Result set). The indexer invoices before searching; a response containing fewer results than were paid for, including zero, is conformant, and what becomes of the shortfall is operator policy, not specified here. An indexer MAY reject a query whose max result count exceeds a local limit it is willing to serve.
- `per_batch`: a single price buys a stated number of queries. An option of this type MUST also carry a query count, which MUST be an integer greater than one; the field MUST be absent on every other type. An indexer MAY advertise several `per_batch` options; a query selects one by naming its query count (see Query request). Each query in a batch is charged once, as a `per_query` query would be, regardless of how many results it returns, and carries its own max result count. A query under a batch that is answered with an Error does not count against it (see Payment profile).

This list is closed for version 0. A node receiving an unrecognized pricing type MUST reject the message rather than infer a meaning.

An announcement MUST NOT carry `per_query` or `per_result` more than once, and MUST NOT carry two `per_batch` options with the same query count. Every option in an announcement is therefore selectable by type and, for `per_batch`, by count.

An advertised price is what the indexer expects to charge, not a contract. Announcements go stale, and what a query costs is stated by the invoice in the payment challenge; the max price on the query is the requester's guard against paying more than it intended. An indexer SHOULD invoice at its advertised price, and a requester SHOULD compare the invoice with the announcement it routed on, but a mismatch is a matter for routing and reputation, not conformance.

In a query request, the max price field is the maximum for whatever is being purchased. For a batch, that is the price of the whole batch of the count the query names, not the price per query within it. For `per_result`, it is the total, the per-result price multiplied by the max result count, not the price of any single result.

A batch credential expires like any other, and a repeated request identifier under it is not counted again (see Payment profile), so a broker MAY retry a query without risk to the batch. A query presenting an expired or exhausted credential receives a fresh 402 challenge, exactly as a query carrying no authorization would. Unused queries in an expired batch are forfeit. Elsewhere request identifiers are correlation only.

*Non-normative.* A batch is bought and spent through L402 without any additional message type. The indexer's challenge covers the whole batch, and the macaroon it issues is valid for the number of queries the query named. The broker retries subsequent queries with the same macaroon and pays nothing further until the batch is exhausted, at which point the indexer issues a fresh challenge. A batch needs more state than any other credential, the set of request identifiers served under it rather than a single spent mark; an indexer that does not want to carry that set can decline to advertise `per_batch`.

## Message types

All message bodies are JSON. The shapes in this section name fields descriptively; the exact JSON keys for every message are defined in Appendix A, and only those keys are conformant.

### Announcement

```
Announcement
  node pubkey
  protocol version (see Extensibility)
  endpoint (see Transport)
  announcement timestamp
  declared role array (see Roles)
  pricing options array
    type (see Pricing types)
    price in integer millisatoshis
    query count (per_batch only)
  corpus summary (if any)
    total document count
    total distinct terms
    last updated timestamp
    languages array (BCP-47 language tags)
    supported statistics format versions array
  signature
```

Announcements MUST be signed, and a receiver MUST verify the signature before acting on an announcement or relaying it. An announcement that fails verification MUST be discarded and MUST NOT be relayed.

An announcement supersedes all prior announcements from the same node pubkey, in full. A receiver holding several announcements for the same pubkey MUST act only on the one with the greatest announcement timestamp and MUST NOT merge fields across announcements.

A node MUST NOT sign two differing announcements carrying the same timestamp, and each announcement it signs SHOULD carry a timestamp strictly greater than its previous one. A node SHOULD reannounce periodically, and whenever its endpoint, roles, pricing, or corpus summary change.

A receiver holding two identical announcements is holding one announcement it heard twice, and keeps one copy. A receiver holding two differing announcements from the same pubkey at the same timestamp MAY retain either: announcements are hints checked against reality at the endpoint, so nodes need not agree on which was current.

A receiver SHOULD ignore an announcement whose timestamp lies more than 3600 seconds ahead of its own clock. The number is advisory (see Extensibility); the check is not. Under mandatory supersession, a single far-future timestamp, from a bad clock or a briefly compromised key, would otherwise pin the pubkey to that announcement beyond correction.

The corpus summary's language tags are advisory routing input. A receiver MUST NOT reject an announcement for the form of a tag.

The announcement carries only the corpus summary, not per-term detail. Per-term statistics are fetched on demand from the `/corpus` endpoint by any node that wants them.

Any node MAY relay an announcement it receives, all of them, some, or none. An announcement is relayed with its content unchanged; any change to its content invalidates the signature. An announcement SHOULD stay small enough to relay widely; no threshold is stated in this revision, and a sender's safest assumption is that smaller propagates further.

*Non-normative.* The announcement timestamp is the originating node's own, and is distinct from the corpus last updated timestamp. It lets a receiver holding two announcements from the same node tell which is newer, whichever peers delivered them. An announcement carrying a new endpoint under an existing node pubkey is a legitimate update: the public key is the durable identity, and the endpoint is expected to change.

### Peer list request

```
Peer list request
  node pubkey
  request identifier
  optional max peer count
  signature
```

A peer list request asks a node for announcements it holds. Every node serves the `/peers` path (see Transport). The response contents are wholly discretionary, and a response containing an empty array is conformant, so serving the path obliges a node to answer, never to disclose.

Peer list requests MUST be signed, and a receiver MUST verify the signature before answering, for the same reason as corpus statistics requests (see RATIONALE.md, "Corpus statistics request and peer list request: why signed").

The max peer count, if present, MUST be an integer greater than zero. A responder MUST NOT return more entries than requested, and chooses which to include. If the field is absent, the responder returns as many as its own policy allows.

### Peer list response

```
Peer list response
  node pubkey
  request identifier
  peers array (each element a complete announcement)
  optional signature
```

Each element of the peers array is a complete announcement as defined in this document, with its content unchanged and carrying its originator's signature. The array MAY be empty. The responder MAY include its own current announcement. The order of the array carries no meaning.

Peer list responses MAY be signed; the node pubkey is REQUIRED either way. A receiver MUST validate each entry as an announcement, its signature included, and MUST discard entries that fail, without invalidating the rest of the response. Surviving entries are then ordinary announcements: supersession, tolerance for unknown fields, and the relay-versus-route rule for unknown protocol versions all apply exactly as if each had arrived by relay.

### Corpus statistics request

```
Corpus statistics request
  node pubkey
  request identifier
  requested format version
  optional max term count
  signature
```

A corpus statistics request asks an indexer for per-term statistics about its corpus, in a named statistics format version (see Statistics formats). A request naming a version the indexer does not serve is declined with `unsupported_version`.

Corpus statistics requests MUST be signed, and a receiver MUST verify the signature before answering (see RATIONALE.md, "Corpus statistics request and peer list request: why signed").

The max term count, if present, MUST be an integer greater than zero. A responder MUST NOT return more terms than requested. It chooses which terms to include; a responder honoring a limit SHOULD prefer the terms most useful to a router, which will usually mean its most distinctive or best-covered terms rather than a random sample. If the field is absent, the responder returns as many terms as its own policy allows.

### Corpus statistics response

```
Corpus statistics response
  node pubkey
  request identifier
  format version
  corpus last updated timestamp
  per term array (element shape defined by the format version; see Statistics formats)
  signature
```

A corpus statistics response answers a corpus statistics request with a snapshot of the indexer's per-term statistics. The format version MUST equal the version the request named, and the per-term array conforms to that format (see Statistics formats).

Corpus statistics responses MUST be signed, and a requester MUST verify the signature before routing on the contents.

The terms array MAY name the same term more than once; the last occurrence governs, and a receiver MUST take it as the one reported.

The corpus last updated timestamp in a statistics response is authoritative for that snapshot. The copy carried in an announcement is a hint; the corpus can have changed since the node last announced.

*Non-normative.* Reporting a partial term list is expected, not a deficiency.

### Query request

```
Query request
  optional node pubkey
  query string
  max result count
  request identifier
  pricing type (see Pricing types)
  query count (per_batch only)
  max price in millisatoshis
  optional signature
```

A query request asks an indexer to search its corpus. The query string reaches the indexer exactly as sent; a receiver MUST NOT normalize or rewrite it before interpreting it. How it is interpreted, as a phrase, a vector, or an unordered bag of terms, is the indexer's choice and out of scope for this document. The query string MAY be empty: whether an empty query is serviceable is the indexer's policy, expressed through an error, not a matter of conformance. The max result count MUST be an integer greater than zero; the protocol imposes no upper bound, leaving practical limits to the indexer.

The pricing type names the option being bought (see Pricing types). A query naming `per_batch` MUST be signed (see Payment profile) and MUST also carry a query count, an integer greater than one naming the batch option; the field MUST be absent for every other pricing type. A pricing option the indexer does not offer, by type or by count, MUST be declined with `unsupported_pricing_type`. The max price is the most the requester will pay for what that option buys (see Pricing types) and MAY be zero; an indexer whose price is higher declines with `price_exceeded`, before minting an invoice nobody intends to pay. No enforcement is needed: an indexer that ignores the field wastes only its own effort.

The node pubkey and signature fields are all-or-nothing: a message containing one without the other MUST be rejected as `malformed` (see Error). The node pubkey identifies the immediate requester, usually a broker, and says nothing about the client on whose behalf the query is made; this protocol carries no end-user identity anywhere. Anonymous requests are permitted, and an indexer MAY refuse them as a matter of local policy. An indexer serving anonymous requests can rate-limit only by network address, not by identity.

### Payment challenge

```
Payment challenge
  node pubkey
  request identifier
  invoice
  macaroon
  expiry
  signature
```

A payment challenge is the body of an indexer's 402 response to a query request (see Transport). The node pubkey is the indexer's protocol identity and MUST equal the pubkey in the indexer's announcement; it has no relation to any key carried in the invoice. The request identifier MUST equal the identifier of the query being challenged, and the broker's retry carries that same identifier.

The invoice is the BOLT-11 invoice string and the macaroon is the base64 macaroon, both exactly as they appear in the accompanying `WWW-Authenticate` header. The expiry is the Unix time after which the indexer will no longer honour the macaroon, and MUST equal the expiry recorded in the macaroon (see Payment profile); the macaroon is opaque to the broker, so this field is where the payer learns when the credential ends. The invoice carries its own, separate expiry, which bounds payment rather than the credential.

A payment challenge MUST be signed. The body is authoritative: a broker MUST NOT pay from a 402 response whose body is missing or unsigned, whose signature does not verify against the pubkey in the announcement the broker routed on, whose request identifier does not match its query, or whose header disagrees with its body.

### Result set

```
Result set
  node pubkey
  request identifier
  results array (ordered by the responding node's relevance, best first)
    document identifier
    URL
    title
    snippet
  optional signature
```

A result set is an indexer's answer to a query request. Ordering is carried by the position of elements in the array, best first by the responding indexer's relevance; there is no rank or score field, and this protocol does not define how relevance is computed. A result set MUST NOT contain more results than the query's max result count. Every string in a result is chosen by the responding indexer (see Security considerations).

The document identifier is an opaque string chosen by the responding indexer, stable and unique within that indexer's corpus. It is comparable only among results from the same indexer; a broker deduplicating across indexers compares URLs, not identifiers.

Result sets MAY be signed. A requester MAY require signed result sets as a matter of local policy, and MAY refuse to route to nodes that do not sign. Individual results are not signed (see Known limitations): a result set's signature covers the set as one message from one node, proving that the node sent those bytes and nothing about their quality. Once a broker merges sets from several indexers, that signature does not survive into what the broker's client sees. Version 0 defines no per-result signature and no way to ask for one.

*Non-normative.* Returning fewer results than the requested maximum, including none, is normal (see Pricing types). A broker is expected to re-rank across responses using its own criteria.

### Error

```
Error
  node pubkey
  request identifier (if the failed request carried one)
  code (see below)
  message (optional, human-readable)
  optional signature
```

An error is the response a node sends when it cannot or will not answer a request. The node pubkey identifies the responder, the node refusing, never the requester. Errors MAY be signed; the pubkey is REQUIRED either way, so a requester is always told which node refused it, including when the request itself was too malformed to identify its sender. The request identifier MUST equal the failed request's identifier when the responder could read one, and MUST be absent otherwise.

The following codes are defined:

- `malformed`: the request could not be parsed or failed validation.
- `invalid_signature`: the request carried a signature that did not verify.
- `unsupported_version`: the statistics format version named in the request is not served by this node. The code concerns statistics formats only; the protocol version is never negotiated (see Extensibility).
- `unsupported_pricing_type`: the pricing option named in the request, by type and, for `per_batch`, by query count, is not offered by this node.
- `price_exceeded`: the node declines to serve under the stated maximum price.
- `rate_limited`: the node is refusing temporarily. The requester SHOULD retry later rather than route elsewhere permanently.
- `rejected`: the node refuses as a matter of local policy. It is not obliged to explain further.

This list is closed for version 0: a node MUST NOT send a code not defined here. Unlike the other closed lists, an unrecognized code does not invalidate the message: a node receiving one MUST treat it as `rejected`, and SHOULD log the original code string so that codes emerging in the wild are visible to operators rather than silently swallowed.

The `message` field is for people. A requester MUST NOT vary its behaviour based on its contents; `code` alone drives behaviour.

The `code` field is authoritative. The HTTP status code accompanying an error response is advisory only. Successful responses SHOULD use HTTP 200; error responses SHOULD use a 4xx or 5xx status appropriate to the failure.

## Statistics formats

The per-term payload of a corpus statistics response is versioned independently of the protocol. An indexer advertises the statistics format versions it can serve; a requester asks for one it understands. An indexer MAY serve several versions concurrently. A request for an unsupported version MUST be rejected with `unsupported_version` rather than answered in a different version.

Each format is a subsection of this section, headed by its integer. This section grows by addition: a new format is a new subsection under a new integer, and a version, once defined, is never redefined.

### Format version 0

Each element of the per term array carries:

```
term
document frequency (how many documents contain the term)
freshness marker (newest document date for that term)
```

This protocol does not define tokenization, normalization, stemming, or term extraction. A term is an opaque UTF-8 string whose semantics are determined by the indexer that reports it. Whether `Bitcoin` and `bitcoin` are one term or two is the indexer's choice.

The freshness marker is the newest source-document timestamp known to the indexer among documents containing the term. Where a source document carries no usable timestamp, the indexer substitutes its own judgment; the marker is a claim by the indexer, not a verifiable property of the documents.

## Transport

Nodes communicate over HTTP/1.1 or later. Every request MUST be sent as the body of an HTTP POST to its path. Protocol traffic MUST NOT travel in plaintext over any link the endpoint operator does not control. For an `https` endpoint this means TLS. An onion endpoint satisfies this without TLS, since Tor already encrypts and authenticates the connection end to end.

Request and response bodies are messages as Encoding defines them. A sender SHOULD set `Content-Type: application/json`. A receiver MUST NOT reject a request solely for its Content-Type header: conformance of a body is judged from its bytes, not from metadata about them.

TLS provides confidentiality on the link, and link-level integrity for whatever the message signatures do not cover: unsigned result sets and errors, and the L402 authorization header. It provides no identity. Node identity comes from the node pubkey and signatures, never from a certificate, so certificate validation policy belongs to the operator and is out of scope for this document. A self-signed certificate is conformant.

The endpoint a node announces is a base URL: scheme, host, optional port, and optional path prefix, with no trailing slash. The host is a domain name, an IP address, or a Tor onion address; the protocol does not distinguish between them. An absent port carries the scheme's default. Each message has its own path, appended to the base URL verbatim, so an endpoint of `https://example.net/search` serves queries at `https://example.net/search/query`:

- Announcements are posted to `/announce`
- Peer list requests are posted to `/peers`
- Corpus statistics requests are posted to `/corpus`
- Queries are posted to `/query`

Every node MUST serve `/announce` and `/peers`. A node declaring `indexer` MUST also serve `/corpus` and `/query`; a node that does not declare it MUST answer both with an Error (`rejected`), so that an announced endpoint answers every path, if only to refuse.

`/announce`, `/peers`, and `/corpus` are free. A node accepting an announcement answers with an empty body (see Error for status codes); a rejected announcement receives an Error. `/query` is paid whenever the indexer charges for the pricing type the query names; at a price of zero the indexer answers directly, without a 402 (see Pricing types).

Paid endpoints use L402. The normative reference is the Lightning Labs specification, `protocol-specification.md` in the repository at `https://github.com/lightninglabs/L402`; Lightning Labs has also published L402 as a bLIP, and this document references the repository specification. The challenge travels as `WWW-Authenticate: L402 macaroon="<base64>", invoice="<bolt11>"` and authorization as `Authorization: L402 <base64(macaroon)>:<hex(preimage)>`.

An indexer responds to an unauthorized query it charges for with a 402 status, an L402 challenge in the header, and a payment challenge (see Payment profile) as the body. The broker retries the identical request body with the authorization header set as L402 defines. The macaroon and preimage travel in that header and never appear in a message body, so the retried body is byte-identical to the first attempt.

Payment-layer failures, such as an invalid macaroon, a bad preimage, an expired invoice, or a malformed authorization header, are handled by L402 and are not protocol errors. They produce a fresh 402 challenge, not an Error message. The Error type covers refusals of the request itself; L402 covers everything about paying for it.

## Payment profile

L402 provides payment proof and authorization; this protocol provides issuer authentication and credential binding. The `WWW-Authenticate` and `Authorization` headers travel exactly as L402 defines them. The rules below are what a conformant broker and indexer add on top.

**Challenge.** Every 402 response to a query MUST carry a payment challenge as its body, duplicating the invoice and macaroon from the header. The signature binds both to the indexer's protocol identity, so a broker paying an invoice knows that the indexer it routed to is the one being paid and that the macaroon it receives is the one that indexer will honour. Issuing a challenge accepts the query as stated: a credential issued for a query naming `per_batch` MUST authorize exactly the query count that query named.

**Binding.** A macaroon issued for a signed query MUST be bound to that query's node pubkey, whatever the pricing type, and a request presenting that macaroon that is not signed by the same key MUST be answered with a fresh 402, exactly as an expired credential is. How the binding is recorded in the macaroon is the indexer's business; the macaroon is opaque to the broker. A macaroon issued for an unsigned query is bound to no key and to no query content: an in-path attacker holding it can spend it on a query of its own choosing, so the payer's exposure is one query, possibly not the payer's. A query naming a pricing type that authorizes more than one query MUST be signed, so its credential is always bound; `per_batch` is the only such type in this revision.

**Single-use credentials.** An indexer MUST honour the semantics of the pricing type it sold: a credential that authorizes one query is spent once served, and a request presenting a spent credential MUST be answered with a fresh 402, exactly as an expired one is. Whether an indexer instead re-serves a byte-identical retry of the request it already served under that credential is its choice.

**Multi-use credentials.** A request is served when it is answered with a result set. Under a credential that authorizes more than one query, an indexer MUST NOT count a request answered with an Error, and MUST NOT count a request whose identifier it has already served under that credential; whether it answers a repeated identifier with the earlier result or with an error is its choice, and the credential's remaining count is unchanged either way.

**Expiry.** Every credential has an expiry, stated in the payment challenge. The expiry MUST be recorded in the macaroon, so that the indexer can enforce it without a record of its own; how it is recorded is the indexer's business.

**State.** An indexer holds nothing between issuing a challenge and receiving its redemption. After first redemption it holds, per credential, a spent mark for a single-use credential or the set of served request identifiers for a multi-use one, both bounded by the credential's count and discarded at its expiry.

*Non-normative.* Who issues the invoice and how it is paid are outside the protocol. The indexer's protocol identity has no relation to the Lightning node that receives the funds, so an indexer may have an LSP, a hosted node, or an ecash mint issue its invoices, and a broker may pay from any source that hands back the preimage. What this profile fixes is the proof: a BOLT-11 invoice in a signed challenge, and its preimage in the authorization header.

*Non-normative.* This protocol states no timeouts. A broker computes its own settlement window from the expiries of the invoices it has been given, less whatever window it has promised its own customer.

## Encoding

Every message is a single JSON object, encoded as UTF-8; any other top-level value MUST be rejected.

Every string, after escape processing, is a sequence of Unicode scalar values; an escape sequence that decodes to an unpaired surrogate MUST be rejected.

Every number in this protocol is a non-negative integer. A number with a decimal point or exponent MUST be rejected wherever it appears, as MUST a string where an integer belongs. Integers MUST NOT exceed 2^53 − 1 (9007199254740991), the largest integer every IEEE 754 double represents exactly; implementations MUST parse integers without loss of precision and MUST reject values outside this range. The literal `-0` MUST be rejected: it is a sign this protocol never permits, not an alternative spelling of zero. Amounts are integer millisatoshis. Timestamps are Unix time in whole seconds.

No field is null or boolean. The literal `null` MUST be rejected wherever it appears; an absent optional field is omitted, never null (see Canonical serialization). The literals `true` and `false` MUST be rejected wherever they appear.

An object MUST NOT contain duplicate keys. Key equality is judged after escape processing: `"a"` and `"\u0061"` are the same key, and a message containing both MUST be rejected. A receiver MUST reject a message containing duplicate keys rather than letting its JSON parser silently pick one, since parsers in different languages pick differently.

These rules apply inside unknown announcement fields as well; a future field is built from the same four types, object, array, string, and integer (see Extensibility).

No shape defined by this document nests containers more than five levels deep; the deepest is a peer list response, whose embedded announcements carry arrays of pricing objects. The only place arbitrary nesting can appear is inside an unknown top-level field of an announcement (see Extensibility). A receiver MAY enforce a local limit on nesting depth. A message exceeding that limit MUST be rejected cleanly, answered with `malformed` like any other unparseable input. An implementation MUST NOT crash or exhaust its stack on deeply nested input, however deep. The reference implementation and the conformance vectors use a limit of 64 open containers.

A request identifier is generated by the requester. It is an opaque JSON string whose UTF-8 encoding is between 1 and 64 bytes inclusive; the limit is on encoded bytes, not code points. A responder MUST NOT interpret its contents; it echoes the identifier back and compares identifiers only by exact byte equality. A requester MUST NOT reuse an identifier while a request bearing it is outstanding to the same node; beyond that, uniqueness is the requester's own concern.

*Non-normative.* A UUID or a hash of anything convenient both work. The identifier exists so a requester can match responses to requests when several are in flight; it carries no meaning to anyone else.

### Keys and signatures

Node identifiers are secp256k1 public keys, serialized as 32-byte x-only public keys as BIP-340 defines them and represented in JSON as 64 lowercase hexadecimal characters. Signatures are BIP-340 Schnorr signatures over secp256k1, 64 bytes, represented in JSON as 128 lowercase hexadecimal characters. The normative reference for both is BIP-340.

A pubkey or signature that is not exactly the stated length of lowercase hexadecimal characters MUST be rejected; uppercase hexadecimal is malformed. Validation checks only the form of these fields. Whether a pubkey names a point on the curve is established by signature verification, never by inspection, so an unverified pubkey is a claim of identity and nothing more.

### Canonical serialization

Announcements, peer list requests, corpus statistics requests, corpus statistics responses, and payment challenges MUST be signed. Peer list responses, query requests, result sets, and errors MAY be signed.

Every signature in this protocol is produced the same way. The signature covers the canonical serialization of every field in the message except the signature itself, and is verified against the node pubkey carried in that same message. Every message that can be signed carries a node pubkey field, so no signed message depends on out-of-band knowledge of who sent it.

To produce or verify a signature, an implementation MUST serialize the message using the following rules. Two conformant implementations given the same message MUST produce identical bytes.

1. The signature field is excluded from the serialization. All other fields are included, including the node pubkey.
2. Optional fields that are absent MUST be omitted entirely; null is not an encoding of absence (see Encoding).
3. Object keys MUST be sorted by ascending UTF-8 byte value, at every level of nesting. This is a byte comparison: not locale-aware, not case-insensitive, and not a UTF-16 code-unit comparison. RFC 8785 (JCS) sorts by UTF-16 code units, which disagrees with this rule for keys containing supplementary-plane characters.
4. Duplicate keys MUST NOT appear in an object (see Encoding).
5. Array element order MUST be preserved as given. Arrays MUST NOT be sorted.
6. No whitespace is emitted anywhere: none between tokens, none after `:` or `,`, no trailing newline.
7. Numbers are emitted as integers with no leading zeros, no leading `+`, no decimal point, and no exponent. The number zero is emitted as `0`.
8. Strings are escaped minimally. Only the following MUST be escaped: `"` as `\"`, `\` as `\\`, and the control characters U+0000 through U+001F. Where a two-character form exists it MUST be used: `\b`, `\t`, `\n`, `\f`, `\r`. Remaining control characters are emitted as `\u00xx` with lowercase hexadecimal digits.
9. The forward slash `/` MUST NOT be escaped.
10. Non-ASCII characters MUST be emitted literally as UTF-8. They MUST NOT be escaped as `\u` sequences.

The message hash is a tagged hash of the resulting byte string, using the construction BIP-340 itself uses: SHA256(SHA256(tag) || SHA256(tag) || canonical bytes). The tag is the ASCII string `ditta/v0/` followed by the message type: one of `announcement`, `peer_list_request`, `peer_list_response`, `corpus_statistics_request`, `corpus_statistics_response`, `query_request`, `payment_challenge`, `result_set`, or `error`. An announcement embedded in a peer list response is hashed as an announcement; the envelope, when signed, is hashed as a peer list response. The signature is the BIP-340 signature over the message hash.

The tag is an opaque protocol constant, fixed for the life of the protocol. `ditta` is the protocol name but carries no meaning the protocol depends on, and the `v0` is part of the constant rather than a reference to the protocol version integer; neither varies unless this document says so.

## Extensibility

A node's announcement declares the protocol version its endpoint speaks, as a single integer; this document specifies version 0. No other message carries a protocol version; the announcement speaks for the endpoint. The version identifies the dialect a node speaks. It is not a feature-negotiation mechanism, and no behaviour in this document is conditional on it.

The protocol evolves through four channels:

1. New optional fields in the announcement, built from the types Encoding permits. Unknown top-level fields in an announcement MUST be ignored. An implementation SHOULD log fields it ignores rather than dropping them silently, so that fields emerging in the wild are visible.
2. New values in a capability array, of which there are three today: roles, pricing types, and statistics format versions. A new array of the same kind is added when a new axis of variation is found and a real need is identified, not in advance of one.
3. New statistics format versions, which allow the statistics payload to change shape without touching any other message.
4. A new protocol version, when the wire contract itself changes.

A new protocol version marks a change that breaks the meaning of something already defined. Purely additive changes do not bump the version; they travel through the first three channels. Advisory numbers, such as relay size guidance and reannouncement frequency, are operational policy rather than wire contract, and are revised in this document without a version change. A node encountering an announcement whose version it does not recognize MAY relay it if the signature verifies, but MUST NOT route on it: a later version may have redefined fields the node believes it understands, and propagation and use are separate privileges.

The canonical serialization rules and the hash tags (see Canonical serialization) are fixed across protocol versions. A node can verify, and therefore relay, a message from a version it does not otherwise speak only if hashing and signing work identically.

Extensions are proposed as changes to this document. A field name means what this document says it means. If two implementations independently adopt the same field name for different purposes, the collision is resolved by adoption and amendment, not by any mechanism in the wire format; version 0 defines no collision-avoidance scheme and no general extension namespace.

A node MAY decline to relay announcements carrying fields it does not recognize, or announcements exceeding a size it is willing to carry: tolerance obliges a node not to break, never to carry.

Everywhere outside announcement top-level fields, the shapes in this document are closed. The operational messages are queries, payment challenges, result sets, corpus statistics requests and responses, peer list requests and responses, and errors. Implementations MUST reject an operational message that carries fields this document does not define, and MUST reject any message that is missing required fields, carries values of the wrong type, fails signature verification, or violates Encoding. A misspelled field on a query fails loudly rather than silently becoming a query without that field, because operational messages move money. The announcements embedded in a peer list response are the one nested exception: each follows announcement rules, including tolerance for unknown fields, because it is an announcement.

The lists in this document are closed as well: an unrecognized role or pricing type MUST be rejected (see Roles, Pricing types), and a statistics format version a node does not serve is declined with `unsupported_version` (see Statistics formats). The error code list is the one exception, closed to senders but tolerated by receivers, which map an unknown code to `rejected` (see Error).

Tolerance is a specified behaviour, not a property of a lenient parser. The announcement's tolerance for unknown fields is specified here, on one message type (see RATIONALE.md, "Extensibility: tolerating unknown announcement fields"); it does not extend anywhere else.

## Conformance

The test vectors in the specification repository are the executable definition of conformance for everything the wire format can express. The vectors are versioned with this document in the same repository, and a conformance claim refers to the vectors at the same revision as the document it claims conformance with: passing an older vector set claims conformance with the older document, nothing newer. An implementation is conformant in that scope when it accepts and re-derives every valid vector, meaning its canonical bytes, tagged message hash, and signature, and rejects every invalid vector for the reason its metadata declares. Rejecting for the wrong reason fails.

Each invalid vector carries `expect`, one or more machine-readable rejection codes; where more than one is listed, reporting any of them passes. These codes are the vocabulary of this section and are normative for conformance testing. They are not wire error codes: on the wire, a rejected request is answered with an Error message whose `code` comes from the Error section (usually `malformed`), and the rejection codes here name the *rule* that was violated, at whatever granularity an implementation needs to distinguish to pass the vectors.

Codes reported at the parsing layer, before the input is a message:

| Code | Violation | Vectors |
|---|---|---|
| `not_utf8` | bytes are not valid UTF-8 | `not_utf8_bytes` |
| `not_json` | bytes are not a complete JSON text | `truncated_json` |
| `not_object` | the top-level value is not an object | `top_level_array` |
| `too_deep` | nesting exceeds the depth limit | `nesting_too_deep` |
| `duplicate_key` | an object repeats a key, judged after escape processing | `duplicate_key`, `duplicate_key_escaped` |
| `non_integer_number` | a number with a decimal point or exponent | `floating_point`, `exponent_notation` |
| `integer_out_of_range` | an integer outside [0, 2^53 − 1], including `-0` | `integer_too_large`, `negative_integer`, `negative_zero` |
| `null_value` | null anywhere; absence is omission | `null_optional_field` |
| `wrong_type` | a JSON type the protocol never uses anywhere | `boolean_value` |
| `bad_string` | an escape decoding to an unpaired surrogate | `lone_surrogate` |

Codes reported at the validation layer, once the input is a well-formed message:

| Code | Violation | Vectors |
|---|---|---|
| `missing_field` | a required field is absent, including a missing mandatory signature | `missing_required_field`, `peer_list_response_missing_pubkey`, `error_missing_pubkey`, `announcement_unsigned`, `peer_list_request_unsigned`, `corpus_statistics_request_unsigned`, `corpus_statistics_response_unsigned`, `payment_challenge_unsigned` |
| `unknown_field` | a field this document does not define, outside announcement top level | `operational_unknown_field`, `corpus_unknown_field`, `result_entry_unknown_field`, `term_entry_unknown_field`, `peer_list_response_unknown_field` |
| `wrong_type` | the wrong JSON type for a known field | `integer_as_string`, `integer_as_string_count` |
| `out_of_range` | a value outside its stated bounds | `max_results_zero`, `max_terms_zero`, `max_peers_zero`, `query_count_one`, `query_count_one_request`, `empty_roles` |
| `bad_length` | a request identifier outside 1–64 encoded bytes | `request_id_empty`, `request_id_too_long` |
| `bad_hex` | malformed or uppercase hexadecimal | `uppercase_pubkey`, `uppercase_sig`, `malformed_pubkey`, `malformed_signature` |
| `bad_enum` | a value outside a closed list | `invalid_role`, `invalid_pricing_type` |
| `bad_combination` | fields present or absent in a forbidden combination | `pubkey_without_sig`, `sig_without_pubkey`, `per_batch_query_unsigned`, `query_count_on_per_query`, `query_count_absent_on_per_batch`, `query_count_on_per_query_request`, `query_count_absent_on_per_batch_request`, `indexer_without_corpus`, `corpus_without_indexer`, `indexer_empty_pricing`, `pricing_without_indexer` |
| `duplicate_value` | a repeated entry in an array that forbids repeats | `duplicate_role`, `duplicate_pricing_type`, `duplicate_batch_count` |
| `bad_signature` | a signature that does not verify | `invalid_signature`, `payment_challenge_tampered_invoice` |

When one message violates several rules, the reported code follows the vectors. Two precedences are stated here because several vectors depend on them. First, a missing required field is reported before an unknown field. Second, a defective field that is neither the pubkey nor the sig is reported before a violated pubkey/sig pairing; a pairing violation is otherwise reported as `bad_combination`, even when the present half of the pair is itself malformed.

Two caveats. `wrong_type` appears at both layers: a boolean is rejected at parsing because no boolean field exists, and a string where an integer belongs is rejected at validation. An implementation MAY report either layer's granularity so long as the code matches. And `nesting_too_deep` assumes the reference depth limit (see Encoding); an implementation enforcing a higher local limit parses that vector successfully and rejects it at validation as `missing_field`, by the first precedence above, since the input carries none of a query request's required fields. That vector's `expect` lists both codes.

## Security considerations

**Result fields are attacker-controlled display content.** Every string in a result set, whether identifier, URL, title, or snippet, is chosen freely by the responding indexer, and titles and snippets exist to be shown to people. A broker MUST treat them as untrusted input: escape them for whatever context renders them, and treat the URL as data rather than something to dereference or emit into markup. A conformant message can carry a `javascript:` or `data:` URL, and nothing in this protocol makes it safe. A client, outside this document's scope, has every reason to do the same. Control characters are legal string content once escaped, so log output built from these fields needs the same care.

**The query string is attacker-controlled input at the indexer.** It flows into search machinery, query parsers, and logs. An indexer MUST treat it as data, whatever its own stack considers dangerous.

**The announced endpoint is attacker-controlled.** The endpoint in an announcement is chosen freely by its signer, keys are free to generate, and every message this protocol defines is posted to an announced endpoint. That combination cuts two ways. Server-side request forgery: an announcement naming a loopback, private-range, or link-local address, cloud metadata endpoints included, turns any node that connects into a probe of its own internal network. And reflection: many announcements naming one victim endpoint make the gossip layer an amplifier, since each announcement certifies itself and costs nothing to produce. A node SHOULD refuse to connect to loopback, private, and link-local ranges unless explicitly configured otherwise, SHOULD bound its own outbound connection rate, and SHOULD treat connecting as its own decision rather than an obligation the announcement creates. That last is the same separation the Extensibility section draws between relaying a message and routing on it: propagation and use are separate privileges, and dereferencing an endpoint is use.

**Per-result arithmetic can overflow a double.** The per-result invoice amount is the per-result price multiplied by the query's max result count. Each factor is individually legal up to 2^53 − 1, so the product can far exceed the range in which floating point is exact. Implementations MUST compute and compare these totals without loss of precision, using arbitrary-precision integers or checked arithmetic, because a silently rounded total corrupts a payment amount.

**Resource limits are local policy; failing them is not.** This document bounds nesting depth (see Encoding) but sets no message size limit; announcements in particular are relayed and may carry unknown fields of any size. A node SHOULD bound the message size it accepts and relays as local policy. Whatever the limit, exceeding it MUST produce a clean rejection, never a crash. An implementation that can be knocked over by one message is a worse outcome than any it prevents.

**Keys should not be shared across protocols.** The tagged message hash guarantees that a signature produced for this protocol verifies nowhere else and that nothing signed elsewhere verifies here, so cross-protocol key reuse no longer enables cross-signing. It still links identities: a node key reused as, say, a Nostr key ties the two reputations and attack surfaces together. Generating a fresh key for this protocol costs nothing. A node SHOULD use a key generated for this protocol alone.

**Replaying a paid request buys nothing.** Replaying a signed message earns the replayer nothing that presenting it the first time did not. Under a multi-use credential it costs the payer nothing, by the deduplication rule in the Payment profile. Under a single-use credential it costs the payer nothing because the credential is spent once served; the one exception is the in-path substitution the Payment profile describes for unbound credentials, where the attacker's query arrives first and the payer's own becomes the replay. A replayed query without valid authorization receives a 402; preventing double payment is the payment layer's job.

**Signed free-path requests can be replayed.** Peer list and corpus statistics requests carry no timestamp or nonce, so a captured signed request can be replayed by whoever holds it, and a responder throttling by identity would attribute the replays to the original signer. Exposure is bounded: requests travel inside the encrypted link (see Transport), so only the responder itself sees one, and a responder can fabricate load it attributes to a requester anyway. Version 0 acknowledges this and adds no mechanism; a receiver drawing conclusions about a requester's behaviour SHOULD weigh that what it observed is possession of a signed request, not necessarily the keyholder's conduct.

## Known limitations

This section names what version 0 does not solve. Each is a gap left open deliberately, with the reason or a pointer to it in RATIONALE.md, so that a reader does not mistake silence for an oversight and an implementer does not mistake the protocol for a guarantee it does not make.

**Payment does not guarantee service.** An indexer is paid before it searches, and nothing in the protocol can force it to return relevant results, or any results, for money received. The Payment profile ensures that a broker pays the indexer it chose and receives the credential it paid for; what that indexer then does is beyond the wire format. The remedy is routing: a broker that is paid poorly stops routing to that indexer. A reputation layer is anticipated and not specified.

**Corpus claims are unverifiable.** A corpus summary and a corpus statistics response are signed, so a false claim is attributable to its signer, but nothing lets a receiver check either against the corpus it describes; the freshness marker in particular is the indexer's own judgment (see Statistics formats). Routing rests on these claims anyway. The remedy is the same as for poor service: routing and reputation.

**An announcement cannot say what a corpus is.** It carries a corpus summary and language tags, numbers and standardized strings a router can compare, and nothing describing scope, provenance, curation, or quality. Automatic routing gets what it needs; an operator or agent choosing indexers by description does not, and learns more only by calling `/corpus`, one round trip per candidate. No topic vocabulary is standardized and no free-form tags are defined, deliberately (see RATIONALE.md, "Announcement: no corpus description"). If tags are added, they arrive in a statistics format version, not in the announcement.

**Result substitution by a broker is undetectable.** A broker assembles a client's response from result sets it received, and a client cannot tell whether what it sees is what the indexers sent. Per-result signatures would make substitution detectable; they were considered and rejected for version 0 (see RATIONALE.md, "Known limitations: why results are not individually signed"). A client that does not trust its broker has no protocol-level recourse; its remedy is a different broker.

**An unsigned query buys an unbound credential.** The Payment profile states the exposure: an in-path attacker can spend such a credential on a query of its own, to a loss bounded at one query's price, and cannot redirect the payment itself. Version 0 leaves the choice with the requester: one that wants a bound credential signs its query (see RATIONALE.md, "Payment profile: two tiers with different security properties").

**Content is not the protocol's concern.** Result strings are attacker-controlled display content (see Security considerations), and nothing in the protocol classifies, filters, or rates what an indexer returns. Whether a result is safe, lawful, or fit to show a person is the broker's judgement, exercised through which indexers it admits and how it treats what they send.

## Appendix A: Field names and examples

*This appendix is normative.* The exact JSON keys for every message are defined here. Canonical serialization sorts keys by byte value, so a signature verifies only between implementations that use these names exactly. Field names are snake_case, matching the token style used elsewhere in this document.

Messages carry no type field. A message is identified by the endpoint it arrives on and its direction: a body posted to `/query` is a query request, and the response to it is a result set, a payment challenge, or an error, distinguished by shape: a result set has `results`, a payment challenge has `invoice`, an error has `code`.

The examples below are formatted with whitespace for readability. The canonical serialization of each contains none (see Encoding). Signature values shown are placeholders, not valid signatures; binding test vectors, each a message with its canonical bytes, hash, and signature under a published key, live in the specification repository.

### Announcement

| Field | Key | Type |
|---|---|---|
| node pubkey | `pubkey` | string, 64 hex |
| protocol version | `protocol_version` | integer |
| endpoint | `endpoint` | string |
| announcement timestamp | `timestamp` | integer |
| declared roles | `roles` | array of strings |
| pricing options | `pricing` | array of objects |
| pricing: type | `type` | string |
| pricing: price | `price` | integer millisatoshis |
| pricing: query count | `query_count` | integer greater than one, `per_batch` only |
| corpus summary | `corpus` | object, present exactly when roles include `indexer` |
| corpus: total document count | `document_count` | integer |
| corpus: total distinct terms | `term_count` | integer |
| corpus: last updated timestamp | `updated` | integer |
| corpus: languages | `languages` | array of strings, BCP-47 language tags |
| corpus: statistics format versions | `statistics_versions` | array of integers |
| signature | `sig` | string, 128 hex |

```json
{
  "pubkey": "e4b2…(64 hex)",
  "protocol_version": 0,
  "endpoint": "https://index.example.net",
  "timestamp": 1787000000,
  "roles": ["indexer"],
  "pricing": [
    {"type": "per_query", "price": 2000},
    {"type": "per_batch", "price": 150000, "query_count": 100}
  ],
  "corpus": {
    "document_count": 1204000,
    "term_count": 8300000,
    "updated": 1786990000,
    "languages": ["en", "fr"],
    "statistics_versions": [0]
  },
  "sig": "9f11…(128 hex)"
}
```

### Peer list request

| Field | Key | Type |
|---|---|---|
| node pubkey | `pubkey` | string, 64 hex |
| request identifier | `request_id` | string |
| max peer count | `max_peers` | integer, optional |
| signature | `sig` | string, 128 hex |

```json
{
  "pubkey": "b7c9…(64 hex)",
  "request_id": "a41e",
  "max_peers": 50,
  "sig": "c2d8…(128 hex)"
}
```

### Peer list response

| Field | Key | Type |
|---|---|---|
| node pubkey | `pubkey` | string, 64 hex |
| request identifier | `request_id` | string |
| peers | `peers` | array of announcement objects |
| signature | `sig` | string, 128 hex, optional |

```json
{
  "pubkey": "e4b2…(64 hex)",
  "request_id": "a41e",
  "peers": [
    {
      "pubkey": "77af…(64 hex)",
      "protocol_version": 0,
      "endpoint": "https://203.0.113.9:8443",
      "timestamp": 1786900000,
      "roles": ["indexer"],
      "pricing": [{"type": "per_result", "price": 250}],
      "corpus": {
        "document_count": 40100,
        "term_count": 610000,
        "updated": 1786890000,
        "languages": ["en"],
        "statistics_versions": [0]
      },
      "sig": "1be0…(128 hex)"
    }
  ],
  "sig": "77c1…(128 hex)"
}
```

### Corpus statistics request

| Field | Key | Type |
|---|---|---|
| node pubkey | `pubkey` | string, 64 hex |
| request identifier | `request_id` | string |
| requested format version | `statistics_version` | integer |
| max term count | `max_terms` | integer, optional |
| signature | `sig` | string, 128 hex |

```json
{
  "pubkey": "b7c9…(64 hex)",
  "request_id": "3f8a12cc",
  "statistics_version": 0,
  "max_terms": 5000,
  "sig": "44d0…(128 hex)"
}
```

### Corpus statistics response

| Field | Key | Type |
|---|---|---|
| node pubkey | `pubkey` | string, 64 hex |
| request identifier | `request_id` | string |
| format version | `statistics_version` | integer |
| corpus last updated timestamp | `updated` | integer |
| per-term statistics | `terms` | array of objects |
| terms: term | `term` | string, version 0 |
| terms: document frequency | `document_count` | integer, version 0 |
| terms: freshness marker | `newest` | integer, version 0 |
| signature | `sig` | string, 128 hex |

The per-term element keys shown are statistics format version 0 (see Statistics formats); a future format defines its own element keys.

```json
{
  "pubkey": "e4b2…(64 hex)",
  "request_id": "3f8a12cc",
  "statistics_version": 0,
  "updated": 1786990000,
  "terms": [
    {"term": "mempool", "document_count": 3120, "newest": 1786950000},
    {"term": "coinjoin", "document_count": 214, "newest": 1785200000}
  ],
  "sig": "a30c…(128 hex)"
}
```

### Query request

| Field | Key | Type |
|---|---|---|
| node pubkey | `pubkey` | string, 64 hex, optional |
| query string | `query` | string |
| max result count | `max_results` | integer, greater than zero |
| request identifier | `request_id` | string |
| pricing type | `pricing_type` | string |
| query count | `query_count` | integer greater than one, `per_batch` only |
| max price | `max_price` | integer millisatoshis |
| signature | `sig` | string, 128 hex, optional |

`pubkey` and `sig` are all-or-nothing (see Query request); both are REQUIRED when `pricing_type` is `per_batch` (see Payment profile), as is `query_count`, which MUST be absent otherwise.

```json
{
  "pubkey": "b7c9…(64 hex)",
  "query": "lightning channel jamming mitigation",
  "max_results": 8,
  "request_id": "c41e77b0",
  "pricing_type": "per_result",
  "max_price": 16000,
  "sig": "d215…(128 hex)"
}
```

### Payment challenge

| Field | Key | Type |
|---|---|---|
| node pubkey | `pubkey` | string, 64 hex |
| request identifier | `request_id` | string |
| invoice | `invoice` | string, BOLT-11 |
| macaroon | `macaroon` | string, base64 |
| expiry | `expiry` | integer, Unix seconds |
| signature | `sig` | string, 128 hex |

```json
{
  "pubkey": "e4b2…(64 hex)",
  "request_id": "c41e77b0",
  "invoice": "lnbc160n1…",
  "macaroon": "AgEEbGV0aGU…",
  "expiry": 1788307200,
  "sig": "9a3f…(128 hex)"
}
```

### Result set

| Field | Key | Type |
|---|---|---|
| node pubkey | `pubkey` | string, 64 hex |
| request identifier | `request_id` | string |
| results | `results` | array of objects, best first |
| results: document identifier | `id` | string |
| results: URL | `url` | string |
| results: title | `title` | string |
| results: snippet | `snippet` | string |
| signature | `sig` | string, 128 hex, optional |

```json
{
  "pubkey": "e4b2…(64 hex)",
  "request_id": "c41e77b0",
  "results": [
    {
      "id": "doc-8842",
      "url": "https://example.org/jamming-paper",
      "title": "Channel jamming mitigation strategies",
      "snippet": "…upfront fees and reputation-based…"
    }
  ],
  "sig": "77aa…(128 hex)"
}
```

### Error

| Field | Key | Type |
|---|---|---|
| node pubkey | `pubkey` | string, 64 hex |
| request identifier | `request_id` | string, present iff the failed request carried one |
| code | `code` | string |
| message | `message` | string, optional |
| signature | `sig` | string, 128 hex, optional |

```json
{
  "pubkey": "e4b2…(64 hex)",
  "request_id": "c41e77b0",
  "code": "price_exceeded",
  "message": "minimum per_result price is 2500 msat"
}
```
