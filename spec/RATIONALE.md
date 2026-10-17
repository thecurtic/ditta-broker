# DITTA design rationale

*Non-normative.* This document records why the protocol is the way it is, one entry per decision, headed by the section of [`protocol.md`](protocol.md) it explains. Nothing here is a requirement; where this document and the specification disagree, the specification governs. Entries are kept so that a proposal to change a rule can be weighed against the reason the rule was chosen.

## Architecture: self-declared roles

Because roles are self-declared, a node may claim to be an indexer without maintaining a corpus. This is self-correcting: such a node must publish a corpus summary, will be queried against it, will return nothing, and will stop being queried. A node claiming `broker` without routing is harmless in the same way: it costs the network nothing beyond the announcements sent to it, and indexers that get no traffic from it stop announcing to it. The role is a weaker signal than a corpus summary, since a node that collects announcements can begin routing on them without reannouncing, but the reannouncement rule asks it to.

## Announcement: who fetches corpus statistics

The announcement carries only a corpus summary so that it stays small enough to relay widely; per-term statistics are fetched on demand rather than announced. Brokers fetch them to route, but the endpoint is not reserved for brokers; an indexer might fetch a peer's statistics to find what the network does not yet cover.

## Announcement: no corpus description

The announcement carries term statistics and language tags, not a description of what a corpus is: its scope, provenance, curation, or quality. Three consumers want that information. Automatic routing needs comparable numbers and gets them from the statistics. Human operators hand-picking indexers need prose. Agent-operated brokers read prose the way humans do, at every-new-peer scale. Version 0 serves the first and leaves the other two to the `/corpus` round trip.

No topic vocabulary is standardized because a vocabulary fixed before deployment constrains every future operator. Free-form tags were considered and rejected for now: before deployment there are no operators to learn from, and early tags harden on the broker side, since routing code matches a string and indexers then conform to it, so an accidental vocabulary is stickier than it looks. If tags are added, they go in a future statistics format version, which versions independently, never in the announcement, which should stay small and rarely versioned; a bad vocabulary in the announcement would need a protocol version bump to fix.

The announcement's job is a "should I call `/corpus`" filter. Without a vocabulary, machines compare only numbers and exact-match strings, and `languages` works only because BCP-47 is standardized externally. The cost is one extra round trip per candidate indexer, a scale problem the protocol does not have yet.

## Announcement: periodic reannouncement

A node reannounces periodically even when nothing has changed so that receivers can distinguish a quiet node from a departed one. Supersession makes frequent reannouncement harmless, so its only cost is bandwidth, and the interval is left to operators.

## Announcement: why language tags are not validated

A receiver does not reject an announcement for the form of a language tag because a bad tag costs a broker a slightly worse routing guess, while rejecting over it silently partitions the network on a parsing detail. The tags are routing input, not a contract, and BCP-47 is enforced by nobody on the wire.

## Pricing types: `per_result` binds to the requested count

Binding the `per_result` invoice to the requested count is what keeps the indexer stateless between challenge and payment: it commits to a quantity, is paid, and only then performs the search, holding nothing in the interval. A corpus smaller than the requested count is expected to be rare in practice, which is why shortfall handling is left to operators rather than built into the wire format. A shortfall cannot be refunded on the wire, since a settled Lightning payment has no return path, and a credit needs a durable identity to attach to, which only a signed query's pubkey provides; an unsigned requester's shortfall can only be ignored. The protocol specifies neither, because the requester chose whether to be identifiable.

## Pricing types: why options are unique

Two options of the same single-query type, or two batch options at the same count, are not ambiguous on the wire: the query names a type and a count, and the invoice states the price. The rule exists for the announcement's other reader, the router. An option a query can never distinguish from its twin is noise in the one place the protocol asks to be small and comparable, and a duplicate is more often a bug in the announcing node than a choice.

## Peer list request: why it exists

Without this message, a node learns about peers only when their announcements happen to be relayed to it while it is listening. The peer list request lets a node ask instead of wait, which is the difference between a network that grows through gossip and one that grows through out-of-band lists. How a node finds its *first* peer remains out of scope.

## Peer list response: why the envelope signature is optional

The envelope signature is optional where the corpus statistics response's is mandatory because the contents differ in kind: a corpus claim can be checked against nothing, while every entry here certifies itself against its originator's pubkey. A responder cannot forge an entry. The worst it can do is omit peers, which discretionary relaying already permits, or serve stale announcements, which supersession and reality at the endpoint correct.

## Corpus statistics request and peer list request: why signed

Unlike a query, a corpus statistics request or a peer list request costs the requester nothing, so payment cannot throttle abuse. A signature does not prevent abuse either: public keys are free to generate, so a determined spammer rotates them. What it provides is a durable identity: a node can recognize requesters it has served before, serve them preferentially, and throttle unfamiliar ones. That is a reputation mechanism rather than a defence, and nodes are expected to rate-limit regardless.

## Corpus statistics response: why signed

This response and the corpus summary in an announcement are the only contents a recipient cannot check against anything else. A result set can be judged on its merits and a price is checked against the invoice, but a corpus claim is taken on trust and routing decisions rest on it. Signing makes a false claim durable evidence rather than an unattributable assertion, lets a broker share a snapshot with another broker or an auditor without the recipient having to re-request it, and gives a bootstrapping node something verifiable to start from.

## Query request: why queries need not be signed

Announcements must be signed; query requests need not be. The rule is to sign what an intermediary could profitably alter. An announcement passes through peers who could rewrite an endpoint or inflate a rival's prices. A query's payment is protected by the signed challenge whether or not the query is signed; what signing adds is a credential bound to the requester's key, which is why a batch must be bought that way. A signed query's pubkey is an identity the indexer may attach reputation to; because pubkey and signature are all-or-nothing, no query ever carries an identity that cannot be verified.

## Query request: why the batch count is on the query

A batch's price and its count are different kinds of number. The price is the indexer's: the announcement's price is a hint, and the invoice is what the indexer demands, so the invoice in the signed challenge is where the price has to appear. The count is the broker's: it picks one from the offered options and states it in a signed query, and the indexer either issues a challenge for that query or declines. Issuing the challenge is the acceptance, so restating the count in the challenge would only echo the broker back to itself.

## Result set: no relevance score

The protocol carries no relevance score because scores are not comparable across nodes, and a score is a number its sender fully controls, so it can be inflated at no cost. Ordering by array position gives the broker the indexer's ranking without a number to game.

## Statistics formats: independent versioning

The statistics payload is versioned independently of the protocol, and an indexer may serve several versions at once, so that it can adopt a new format without waiting for requesters to upgrade and without touching any other message.

## Statistics format version 0: document frequency

Document frequency counts documents containing the term rather than total occurrences, because occurrence counts can be inflated by repetition within a single document.

## Statistics format version 0: per-term freshness

Freshness is reported per term rather than corpus-wide because a query about a recent event needs recent coverage of *that* term, which a corpus-level timestamp cannot express.

## Error: the code set

The distinction that matters most to a broker is between `rate_limited` and everything else: rate limiting means try again later, while the other codes mean this request will never succeed as sent. The deliberately vague `rejected` exists so that a node declining for policy reasons, refusing anonymous requests for instance, need not disclose its policies to be conformant.

## Error: strict to send, tolerant to receive

The error code list is the one closed list whose unknown values do not invalidate the message. Every other list guards something a receiver will act on, so a value it cannot interpret must fail loudly. An error is already a refusal: rejecting it for its code gains the receiver nothing, and mapping an unknown code to `rejected` is the same outcome a conformant node would have sent. The sender side stays strict so that the list cannot drift open by practice, and the rule that receivers log unknown codes makes any drift visible to operators rather than silent.

## Error: HTTP status is advisory

Status codes are too coarse to carry the distinctions the code set draws, and intermediaries may alter them, so the `code` field is authoritative and the HTTP status advisory.

## Payment profile: two tiers with different security properties

An unsigned single-use credential is the probing tier: anonymous, unbound, one query, a small loss if an in-path attacker substitutes a query of its own, and no trust required on either side. A batch is the relationship tier: signed, bound to the requester's key, real money, and deduplicated by request identifier so a retry is safe. A signed single-use credential sits between them: bound, so not substitutable, but not deduplicated, because there is nothing to count. The tiers differ in what they protect on purpose. Making the probing tier as safe as the relationship tier would mean requiring signed queries everywhere, which ends anonymous querying; making the relationship tier as cheap as the probing tier would mean an unbound credential worth many queries, which is exactly what an in-path attacker wants.

## Payment profile: why the indexer cannot be fully stateless

L402 verification is stateless, but whether a credential has already been used is a fact about history that no token can carry. Expiry and identity caveats verify against a clock and a signature; a usage-count caveat states a limit but cannot enforce it. So single-use requires a spent mark and multi-use requires a served-identifier set, both held from first redemption until the credential expires. The state is bounded by the credential's own count and lifetime, which is what keeps it from becoming an attack surface.

## Payment profile: expiries and timeouts are left to self-interest

The protocol bounds no credential expiry and states no timeouts. Unused queries in an expired batch are forfeit, so an indexer that wants standing broker relationships has an obvious interest in generous expiries, and a broker facing a short invoice expiry can skip that indexer. Both numbers are therefore left to the indexer, which pays for choosing them badly.

## Canonical serialization: why hashes are tagged

The tag buys domain separation in two directions. Across message types: no two types can be confused at signing time, even if a future revision gives two shapes overlapping field sets. Today their required fields happen to differ, but the tag makes the guarantee structural rather than accidental. Across protocols: x-only secp256k1 keys with BIP-340 signatures are the key format of Nostr and others, and without a tag a key reused across protocols signs bare SHA-256 digests of attacker-influenceable JSON in each, so a message signed for one protocol could be crafted to verify in another. With the tag, a signature made for this protocol verifies nowhere else, and nothing signed elsewhere verifies here.

## Extensibility: tolerating unknown announcement fields

Announcements tolerate unknown fields because the announcement is descriptive and free: nothing paid rides on it, and a node that ignores a field it does not understand merely routes with less information. Tolerating them has a cost: it makes the announcement a broadcast channel for content that relaying nodes cannot evaluate. Three things bound it. Relaying is always discretionary, and a node may decline to relay what it cannot read or will not carry. Abuse is signed, so it is attributable, which is what a future reputation layer needs. And reputation for relaying is distinct from reputation for serving: relaying costs bandwidth while routing costs money, and a node may reasonably relay for a stranger it would not yet route to. Relaying is also unobservable in the negative: a node that declines to relay is indistinguishable from one that has heard nothing, so no reputation can be built on a failure to relay. A node that receives an announcement it cannot fully read from a peer it trusts may infer that the peer, who could read it, judged it worth passing on; that inference reaches only the last hop, and nothing in the message records who relayed it, so a peer's poor judgment is learned from experience rather than proven from evidence.

## Extensibility: serialization stability

A change to the serialization rules does not degrade the network gracefully; it partitions it, since a node can verify and relay a message only if hashing and signing work identically. That is why the rules and the hash tags are fixed across protocol versions rather than tracking the version integer: because the tags are as stable as the serialization rules themselves, tagged hashing preserves the cross-version verifiability that relaying depends on.

## Extensibility: no extension namespace

A namespace scheme designed before anyone has tried to extend anything is a guess, and a wrong guess would have to be maintained forever. Version 0 therefore defines none, and collisions are resolved by adoption and amendment.

## Extensibility: why tolerance is specified

Tolerance is specified on one message type rather than left to lenient parsing because a receiver that quietly accepts malformed messages allows non-conformant implementations to establish themselves, after which the protocol can no longer be corrected.

## Conformance: why rejecting for the wrong reason fails

Right answer, wrong rule is how two implementations drift apart while both appearing correct. The vectors therefore require one of the declared reasons, not merely a rejection.

## Known limitations: why results are not individually signed

Per-result signatures put signing work into the indexer's per-result hot path to protect against a party the client has already chosen to trust, and a broker merging duplicate URLs from several indexers makes per-result attribution ambiguous in any case. Version 0 therefore signs result sets, not results.
