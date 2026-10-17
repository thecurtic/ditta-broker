# Decisions

Design decisions that are not visible from the code's structure alone, so
they don't get silently reversed later.

## Every figure is ours: labeled diagrams of DITTA's own parts

(Oct 10 2026.) The redesign first used stock figures from a third-party
illustration library. They were dropped: generic metaphors that need
decoding, not pictures of what DITTA does, and not ours. Every figure is now a labeled diagram drawn
by `broker/web/figures.js` with one cast across both pages — you, the
broker, indexers named by what they cover, the query pill, the invoice card
(amount in sats, QR, signature), results, the signed announcement. Money is
a lightning bolt and sats, never a punch card. Words inside the drawing are
labels; nothing third-party draws on the page and nothing loads at runtime.

## Searchers may narrow the set; the broker still decides within it

(Oct 10 2026, search-and-selection.md; replaces the naming spec's "the
searcher never controls routing".) `/client/search` and `/client/quote`
take an optional `pubkeys` scope. It filters the CANDIDATE set before rank
— alongside Tor exclusion and reachability — and nothing downstream
changes: the price-blind rank, the threshold, the fanout cut and merge run
as before within the scope, so a scope larger than the fanout is still cut
by the broker (reason `fanout`). Pubkeys are the only identity: an unknown
or delisted one is an `unknown_pubkey` error naming it, never dropped; a
scope nothing in which can be asked (Tor, unreachable, priced out) is an
`empty_scope` error with each pubkey's reason, refused before an invoice is
minted — never widened to the full network. The scope rides inside the
envelope, so the L402 body binding covers it and the head start routes on
exactly what was paid for.

Every search response now carries the effective scope (`queried`,
`failed`, and for a scoped ask `skipped` with reasons) and `charged_msat`,
regardless of `expose_outcome`. This is a deliberate, small slice of the
outcome record — who was asked and who failed, by pubkey, no errors,
latencies, prices or raw sets — published because the spec's partial-
results and never-widen states cannot be shown without it.

## Cumulative delivery history: observed content, cited as the basis

(Oct 8 2026.) The outcome store keeps 90 days of the actual result URLs
each indexer returned; nothing read them. Now a read-model over the store
(OutcomeStore.content_profile) projects, per indexer, its observed result
DOMAINS and counts — observed delivery, the unforgeable half of a
corpus's character (an indexer can lie in /corpus, not in what it hands
back). It feeds three things: naming's description may cite where results
actually point; routing's scorer gains a mild cumulative-uniqueness
multiplier (favor indexers whose merged results nobody else returned —
they bring something new); and every name/description now records and
shows its EVIDENCE BASE — reported documents, results observed, queries
routed — so a name from thin data is transparently a name from thin data
(name on little data, not zero, but say what it rests on). The
per-term posteriors remain routing's primary delivery-history signal;
domain-based ReDDE routing is the documented next step this projection
lays the ground for.

Also fixed here: naming produced NO names for 15h because three
validator failures held every run — "15 languages" (a list COUNT never
in the allowed numbers), "105 million" (magnitude words the number
parser ignored), and reasoning blocks (<think>) ahead of the JSON. All
three are now handled; the real defence stays deterministic validation,
just not brittle against ordinary phrasing.

## Routing is learned density, selection is a threshold, merge is evidence

(Oct 7 2026, the demo-to-product turn.) Routing scores indexers by
query-likelihood over term DENSITIES (df/N smoothed by the general
prior) — presence-counting coverage could never send "zap relays" to the
nostr corpus past a 102M-doc generalist that technically "covers" every
word. The learner (learner.py, a deletable projection over the outcome
store) adjusts densities with per-(indexer, term) delivery posteriors
(Thompson-sampled; clicks weight successes), calibration, and
reliability. Selection drops any indexer trailing the best score by
skip_odds (fanout is a ceiling, not a quota): a niche query goes to the
niche corpus ALONE, and the receipt says "low_score" for the rest.

Thompson sampling and the exploration floor are SEEDED PER (query, hour)
— randomness must never be why a quote and its invoice disagree. RRF
alone was retired from the default path: merge_evidence ranks by source
affinity x query-term presence in the result's own title/snippet/URL x
cross-source agreement, every factor explainable. The full fetched tail
is always returned under a confidence divider — results were paid for,
so they are shown, ranked hard, never hidden (a divider, not a hard
cut). mode: coverage keeps the old pipeline for comparison.

Page interactions feed the learner in AGGREGATE ONLY — a click beacon
carries request_id + URL, splits credit equally across the URL's
sources, and records no user identifier of any kind. No personalization,
by decision.

## The client API is /client/*: knobs in, receipts out, flags on the seam

The customer↔broker hop is out of the spec's scope, so it is a product
API, not a protocol one (Sept 17 2026). One
door for everyone — `/client/search` takes the knob envelope (query,
request_id, n, max_msat, exclude_tor); agents do not come in via protocol
shapes, which would only confuse the two surfaces. L402 wraps the
envelope exactly as it wrapped the old door: the credential binds the
envelope's exact bytes, replay is byte-identical, consume-on-service and
the head start unchanged. `min_indexers` and `prefer_fresh` were
considered and dropped.

Two earlier decisions reversed deliberately: indexer identities
(pubkey/endpoint/transport) ARE on the cards now — the card is the
anchor routing, payment, and results all point at, and a visitor can
copy a pubkey to seed their own broker. And the merged results carry
`sources[]` per result, always on, no flag: you paid, you may know who
served you. The full outcome record (every indexer
considered/routed/paid/responded/failed, coverage, payments, raw sets)
sits behind `client.expose_outcome` / `expose_raw_sets`, default OFF —
the reference broker should always PRODUCE the record (reputation and
caching need it) and only a deployment that wants to show its work
publishes it. Route-at-quote
was rejected outright: prediction and receipt must differ, and the
receipt is free to show because the buyer already paid.

## The site redeems on the broker's own witness; the preimage is the fallback

The web client used to ask the person to paste the preimage after paying —
pure demonstration, and the head start (Sept 16 2026) had already made it
redundant: the broker minted the invoice on its own node, polls that node
for settlement, and routes before the preimage ever arrives. So (Oct 1
2026) a credential presented with the preimage slot empty
(`L402 <macaroon>:`) redeems once the minter reports the invoice settled —
the HMAC proves we minted the token, the node proves it was paid, the body
binding proves it is the same request. The page polls `/client/status`
(which now asks the node directly when the prefetcher has no entry) and
replays by itself the moment settlement shows; the paste survives folded
under "Paid, but nothing happened?" for a site without the secret or a
node that has not witnessed settlement.

This is the site's privilege alone, not the client API's. The web client
process and the broker share `site_secret` (`receive.site_secret` /
`webclient.site_secret`); a replay carrying it as `X-Site-Secret` is
"witnessed", and only a witnessed presentation may leave the slot empty
(`L402Receiver.redeemable(..., witnessed=True)`). Agents at the same
`/client/search` door, protocol peers, and this broker's own outbound
client present the preimage as L402 says — to them an empty slot is a bad
credential and earns a fresh 402. With no secret configured nothing is
ever witnessed and the site pastes like everyone else.

What this widens, deliberately and only for the site: whoever holds the
macaroon, the exact request bytes, AND the site secret can collect once
the invoice is paid. The macaroon holder is the party who received the
402, over the same TLS channel the redemption travels, and the secret never
leaves the two server processes — the preimage added no protection against
anyone who could not already read that channel. The head start's "moves
work, never trust" line is therefore reversed for this one case and
recorded here.

## Cards say what the broker THINKS, and separately what it has SEEN

The card's "reads as:" line is a composed characterization — corpus
scale, vocabulary flavor (boilerplate share), strongest distinctive
coverage, overlap with other indexers — where every clause carries the
measurement it rests on (hover). It is labeled "inferred from reported
statistics — unverified" until the outcome store has delivery data, at
which point the line upgrades to "delivered on X of Y query terms routed
to it (7d)" and the vocabulary table marks each claimed term
✓ delivered / ✗ claimed-not-delivered / — untested. Claimed and
demonstrated are never blended: the inference layer and the evidence
layer are visually and structurally separate. A bundled topic taxonomy
(mapping vocabularies onto fixed category labels) was rejected — it
would be our labels, not the broker's knowledge, and it breaks on the
first vertical it lacks.

## Cards score against the fixed prior alone; mirrors are never cancelled

(Oct 6 2026, superseding the network half of the blended baseline below
FOR THE CARDS.) Scoring a card's vocabulary against the rest of the
network made the twin 102M-doc mirrors cancel each other to empty chip
rows. Mirrors shouldn't be cancelled: a mirror pair is one corpus with
content, and its description should display normally. So the
cards now rank terms by lift over the bundled general-English prior
alone (with the 2x margin and the full stopword list); identical corpora
simply show identical chips, and the mirror relationship is stated by
the separate overlap clause. The network-relative baseline survives in
stats.py (network_context / the network parameter, tested) for the
learner and routing to use later, where relative distinctiveness is the
right question.

## Chips rank term lift against a BLENDED baseline

The denominator for a term's lift is max(network-wide share, general-
English prior): the two baselines fail differently, and a term must beat
both. The general prior alone missed web boilerplate ("copyright",
"facebook" — rare in prose, everywhere on the web); the network share
alone re-opens the tokenizer trap (whatever only one indexer's tokenizer
kept looks maximally distinctive). The network baseline is the REST of
the network — self excluded, though the roadmap formula sums over all j —
because at small N self-inclusion degenerates (a lone indexer becomes
its own baseline and every lift is exactly zero). And "distinctive"
requires a MARGIN — at least 2x the baseline (MIN_LIFT = ln 2) — because
near-identical corpora give every shared term a lift epsilon above zero
(the rest-of-network total is slightly diluted by the small indexers),
at which point ranking collapses to raw document frequency and surfaces
boilerplate; observed live on the twin 102M-doc crawls, whose chips were
"whatsapp, sitemap, datenschutz". With the margin, true mirrors cancel
to an empty chip row and the card says so in words ("no vocabulary that
stands out from the rest of the network").

## Chips rank against a general-English prior, not each other

Cross-corpus contrast (the previous fix) rewarded whatever tokens the
OTHER corpus's tokenizer filtered out — a forum corpus that kept
stopwords made "while/where/over/who" look maximally distinctive.
Chips now score log(df_here / df_general) against a small bundled
general-English document-frequency table plus a stopword list: common
here and common everywhere cancels, common here and rare in general
English (the technical vocabulary) wins. A fixed external prior cannot
be gamed by any peer's tokenizer.

## The client door is off the protocol's /query (historical: /entry)

DITTA v0 (the published spec this repo tracks at `spec/`) is explicit that
on the wire a broker sends queries and does not answer them: a node not
declaring `indexer` MUST answer `/query` and `/corpus` with `rejected`.
This broker's paid entrance used to live on `/query`; it moved to
`/entry`, an off-protocol path speaking the protocol's own message shapes
— since superseded by `/client/search` and the knob envelope (see above),
but the placement rule stands.

The alternative — declaring the `indexer` role so `/query` may serve — was
rejected: the broker maintains no corpus, its quoted entry pricing cannot
be honestly expressed as a static announcement price, and claiming a role
to dodge a MUST is exactly the kind of gaming the spec's self-declared-role
model expects reputation to punish. The broker-client interface is out of
the spec's scope; putting it on a clearly non-protocol path is what keeps
the four protocol paths byte-for-byte conformant.

## Entry credentials are consumed by service, never by presentation

A paid credential at the broker's door is spent only when the route it
authorized actually produced answers — at least one indexer served. The
check (`redeemable`) and the spend (`consume`) are separate operations;
routing runs between them. A route where nothing answered returns the
empty result set with an off-protocol `X-Credential-Retained: 1` header so
the client keeps its pending payment and retries, and the credential
survives untouched. This replaced an earlier design that consumed on
presentation and re-served a stored response to a byte-identical retry
until the credential's expiry — rejected as a patch over the wrong
primitive: the payer should never need a recovery mechanism for a
credential that should not have been spent in the first place. It also
matches the spec's own batch rule (a request answered with an Error is
not counted) more honestly than consume-then-compensate did.

## The broker defends against spec SHOULDs instead of trusting them

The spec says an indexer answering a term-less `/corpus` request SHOULD
prefer routing-useful terms over a random sample. Nothing on the wire
enforces a SHOULD, and a live indexer promptly demonstrated it: ~52 terms
per document — stopwords and numerics included — versus ~4 from a
better-behaved indexer. Naive
contrast scoring across the two corpuses then rewarded exactly the tokens
the cleaner corpus had filtered out, because zero-versus-anything is
maximal contrast; the homepage cards were rendering tokenizer differences,
not corpus differences.

So term selection for the `/network` cards (`stats.py`) filters and bands
each corpus's vocabulary itself — token filter, coverage ceiling — before
any cross-corpus scoring, under configurable rendering policy (`cards` in
the config). The general rule: where the spec says SHOULD, the broker
verifies or compensates; only MUSTs backed by validation are trusted.

## Answered searches survive navigation; the URL names them by id only

The web page used to treat every load as a fresh visit: no URL state, so
Back from a result link, a reload, or a browser restart lost the results
that had been paid for. Now every answered search (free ones included,
outcome record or not) is kept in localStorage — the last 20, this browser
only — and `history.pushState` names the one on screen as `?r=<id>`.
Back/Forward, reload and session restore re-render it from storage; nothing
is re-requested or re-paid. A bare visit reopens the newest results when
they are under 30 minutes old and "New search" was not pressed since.

The URL carries an opaque id, never the query: the browser's own history
is out of the page's reach, and "Recent searches" promises deletion (one
entry, or all) that a query string in that history would quietly outlive.
For the same reason the search box keeps `autocomplete="off"` — browser
form history is equally undeletable from here — and an unsent query is
kept in sessionStorage instead, cleared with the history.

The rule that a fresh load never resumes an *unredeemed* invoice stands:
only finished result sets reopen; invoices still wait in their own list.
