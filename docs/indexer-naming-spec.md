# Indexer Naming — Broker Feature Spec

Status: implemented (`broker/naming.py`).
Scope: broker back end and broker front end. Not part of the DITTA wire protocol.

## 1. What this is

The broker gives every indexer it knows a name — a short, fun, human-memorable handle derived from what the broker knows about that indexer. The name sits on top of the full pubkey, a factual description, and the broker's own subjective assessment. The broker also keeps the history of every name it has given each pubkey.

Names are attribution, not choice. They tell the searcher *where the broker pulled these results from, and what the broker thinks of those places*. They do not let the searcher pick indexers.

The name is expected to drift. As an indexer's corpus changes, its name may change. The pubkey is the identity; the name is the broker's current opinion of it, and the history makes that drift legible ("I remember getting results from this one back when it was called X").

## 2. The hard rule

**The searcher never controls routing, ranking, or merging.**

> Amended by `search-and-selection.md` §3. The original rule forbade the searcher from selecting indexers at all. That is relaxed: a searcher may constrain the *set* of indexers a search is sent to (the "Search this" checkboxes and scope chips). Within whatever set is in play — full network or a chosen subset — the broker alone routes, ranks, and merges. The paragraphs below are updated to that boundary.

The broker routes on everything it knows about each indexer — its corpus statistics, its routing profile, its track record. That is what a broker does, and scoping does not restrict it. Names, descriptions, and assessments are how the broker *shows* the searcher that knowledge. They are never a lever the searcher can pull to change ordering or weighting.

- The searcher may narrow which indexers are asked, but cannot rank, weight, or pin indexers, and cannot influence routing or merging within the chosen set — not in the UI, and not through any parameter on the broker's customer-facing endpoints. Scoping is by pubkey (`search-and-selection.md`), never by name.

- Because a name is never a routing, ranking, or merging input, a misleading or unflattering name cannot redirect a query or a payment. Scope selection operates on pubkeys, not names, so it cannot either.

- The generated text is a rendering of the broker's knowledge, not a source of it. Routing reads the underlying data (announcements, `/corpus` statistics, the per-pubkey routing profile) directly, so the pipeline has no reason to read the naming store. Naming sits *beside* the query pipeline: it reads from discovery and reputation and writes only its own store, which the presentation layer reads.

Naming is entirely out of band with respect to the protocol. No name is ever placed in, signed into, or relayed through any DITTA message.

## 3. The indexer card

Every place the broker presents an indexer in full — the card opened from a result's attribution line, the group headers in the by-indexer results view, and the indexer list on the home page — uses the same card, top to bottom:

| # | Element | Content | Visual weight |
|---|---|---|---|
| 1 | **Name** | Fun, evocative handle, e.g. "The Polyglot Web Crawler", "Cybersecurity Index" | Largest, boldest. Front and center. |
| 2 | Pubkey | The full 64-character hex pubkey. **Never truncated or elided.** | Small, muted, monospace, not bold. Wraps on narrow screens; selectable and copyable. |
| 3 | Description | Brief, factual, statistics-driven. What the corpus *is*. | Body text. |
| 4 | Assessment | Brief, subjective. What the broker *thinks* of it. | Body text, visually distinct from the description (e.g. italic, or a label such as "Broker's take"). |
| 5 | Name history | Every name this pubkey has carried at this broker, with date ranges. | Collapsed by default; one tap to expand. |

The description and assessment must read as clearly different registers. A searcher should never confuse the broker's opinion with a measured fact.

### 3.1 Description (factual)

Grounded strictly in facts the broker holds about the indexer (see stage 1 for the starting set) — for example document count, languages, freshness, distinctive coverage, overlap with other indexers, transport, pricing. One to three sentences. No adjectives the data does not support. Every claim must be traceable to an input field.

Example: "~3,400 documents, English only, updated within the last week. Distinctive coverage in vulnerability disclosures and exploit write-ups. Vocabulary overlaps little with other indexers this broker knows."

The existing home-page indexer paragraphs ("reads as a broad web-scale corpus, multilingual, clean curated vocabulary…") are this element. Keep that capability; it becomes stage 1 below.

### 3.2 Assessment (subjective)

The broker's own judgment, drawn from its experience routing to this indexer: how often it answers well, latency, value for money, how distinctive its results are in practice, whether it complements or duplicates others. First person from the broker's point of view, hedged as opinion. One to three sentences.

Example: "Our go-to for anything security-flavoured — rarely misses on those queries, but slow to answer and not much use outside its lane."

It may be opinionated and a little playful. It must not invent facts; anything factual it mentions must also be supported by inputs.

### 3.3 Name

Two to five words. Fun, evocative, clearly a nickname. Derived from the description and assessment together.

A name MUST NOT:

- claim trust, endorsement, or status ("Trusted", "Official", "Verified", "Certified", "Secure")

- impersonate a real organization, product, brand, or person

- look like an identifier (hex strings, pubkey fragments, version numbers)

- duplicate the current name of any other pubkey at this broker

- reuse a name previously held by a *different* pubkey at this broker (retired names stay retired, so history never points two identities at one name)

## 4. Pipeline

Runs on local inference only. Corpus analysis never leaves the broker's own infrastructure.

Four stages. Each is a separate model call with its own prompt and settings, so each fails independently and visibly. Stage outputs are stored, so a bad stage can be rerun alone.

### Stage 1 — Characterize (factual)

- Basis: any factual, checkable information the broker holds about the indexer. The list below is a starting point, not a limit — engineers should add sources as the broker learns more:

- the announcement (corpus summary, languages, roles, pricing, how recently it was updated)

- `/corpus` statistics (top and distinctive terms, freshness)

- overlap with other known indexers

- transport (e.g. reachable only over Tor)

- what its results actually point to (domains, content types, languages of returned pages)

- the broker's own measurements of it (how long it has been known, response times, uptime)

- The line between stage 1 and stage 2 is fact versus judgment, not which data source is used. A measurement can appear in stage 1 as a fact; what the broker *thinks* of it belongs in stage 2.

- Output: the description (§3.1), plus a structured list of the facts it relied on.

- Settings: low temperature. Grounded and literal.

- Validation: deterministic check that every number in the output matches an input value (within rounding). Fail → retry once → hold.

### Stage 2 — Assess (subjective)

- Input: stage 1 output, plus this broker's per-pubkey routing profile (terms answered well, latency, pay-to-useful ratio, answered/failed counts) and the overlap data.

- Output: the assessment (§3.2).

- Settings: moderate temperature.

- Validation: length bound; banned-word list (§3.3 trust words); no numbers that are not in the inputs.

### Stage 3 — Name

- Input: stage 1 and stage 2 outputs, and the pubkey's current name (if any).

- Output: a short list of candidate names (e.g. 5).

- Settings: higher temperature. This is the creative compression step.

### Stage 4 — Reconcile

- Input: candidates, the pubkey's name history, all current names at this broker, all retired names, and a summary of how much the characterization changed since the last name.

- Decision: **keep** the current name, or **rename** to one candidate. Default is keep.

- Rename only when the characterization has materially changed. Model nondeterminism alone must never rename an indexer — drift should track the corpus, not the sampler.

- Validation: deterministic enforcement of every §3.3 rule (uniqueness, retired names, banned words, length, not identifier-like). A model-chosen name that fails is discarded; fall back to the next candidate or keep.

Naming runs in the background. Nothing in the query path waits on inference.

### 4.1 Untrusted input

Everything an indexer controls — terms, languages, endpoint — is attacker-controllable text going into a model prompt.

- Pass indexer-supplied values as clearly delimited data, never as instructions.

- Do not feed the endpoint hostname into stages 3–4 (an operator could steer its own name via its domain). Transport type may be passed as a fact.

- Cap the number and length of terms passed in.

- Output validation (above) is the real defence; prompts are not.

### 4.2 Failure behaviour

- No name yet → card shows "Unnamed indexer" in the name slot, full pubkey beneath. Results are never delayed or hidden for want of a name.

- Stage failure after retry → keep the last good output for that element and mark the job held for operator review.

- Inference server down → naming work waits until it returns; existing names stay.

## 5. Local inference

The code is model-agnostic. It talks to any OpenAI-compatible local endpoint and knows nothing about which model sits behind it.

- **No model name in the code.** Not in source, defaults, tests, or committed config. The endpoint URL and model ID come from environment variables, set in a config file that is git-ignored. Commit only an example file (e.g. `.env.example`) with placeholder values.

- **Target deployment:** Qwen3.6-35B-A3B, MLX 8-bit, served locally (e.g. `mlx_lm.server`). This is the model the prompts and settings are tuned against, and the one the feature is tested on.

- **Tuning lives in config, not code.** Per-stage sampling settings (temperature, max tokens), candidates per run, and the prompt texts are configuration, so they can be tuned for the target model and retuned for another without code changes.

- **Model-specific handling is isolated.** If the target model needs special treatment (for example, stripping a reasoning block from its output before parsing), put it behind a small, clearly named option or adapter that is enabled in config. Never branch on the model name.

- **Traceability:** the model ID in use is recorded on every stored output (§6), so any name, description, or assessment can be traced to the model that produced it.

## 6. Data model

Per pubkey (current state):

| Field | Notes |
|---|---|
| `pubkey` | 64-char hex, primary key |
| `name` | current name, nullable until first run |
| `description` | stage 1 output |
| `description_facts` | structured facts stage 1 relied on |
| `assessment` | stage 2 output |
| `named_at`, `described_at`, `assessed_at` | timestamps, updated independently |
| `model_id` | model that produced the current outputs |
| `inputs_hash` | hash of stage 1 inputs, to detect when they change |
| `status` | `ok` / `held` / `pending` |

Name history (append-only, one row per name):

| Field | Notes |
|---|---|
| `pubkey` |  |
| `name` |  |
| `from`, `to` | `to` null for the current name |
| `description_at_naming` | snapshot of the description the name was chosen from, so the history reads as a story |

Retired-name index: every name ever used, with the pubkey that held it, enforcing §3.3 reuse rules.

## 7. Front-end contract

### 7.1 Results

**Default: one ranked list.** Results appear in the broker's merge order — best result first, second-best second — regardless of which indexer served them. Consecutive results may come from different indexers; that is expected. Ranking is the broker's alone, and this feature does not change it.

- Each result shows the name of the indexer that served it, as a small attribution line. Tapping the name opens that indexer's card (§3).

- If a result was served by more than one indexer, show it once and list all of its sources' names.

**Optional: by-indexer view, one click away.** A toggle re-presents the same results grouped by source, each group headed by that indexer's card.

- Within a group, keep the broker's merge order.

- Groups are ordered by the merge rank of each group's best result.

- A result served by more than one indexer appears in the group of its highest-ranked source, with the other sources' names listed on it.

- This is a different view of the same results, not a filter. It changes nothing about which indexers were queried or how results were ranked.

The search result payload from the broker's customer-facing endpoint must carry the responding pubkey(s) on each result. The front end joins pubkeys to names at render time.

### 7.2 Indexer list (home page)

The existing indexer list becomes a list of cards (§3). Keep showing the extra transparency data already displayed; the card is added on top, not in place of it.

### 7.3 Endpoints

Extend the broker's off-protocol network endpoint (currently `/network`) to return, per pubkey: `name`, `description`, `assessment`, timestamps, `status`, and `history`. Optionally add `/network/{pubkey}/history` if history grows large. These endpoints are not DITTA messages and are not signed.

## 8. Sharing names between brokers

Another broker MAY look at this broker's names (via the public network endpoint) and copy them, and this broker MAY do the same. That is an out-of-band, manual operator decision. A copied name is presented as this broker's name — this broker is accountable for everything it displays.

Not built in v1: no import tool, no automatic syncing, no protocol support.

## 9. Non-goals

- Any searcher control over routing, ranking, or merging within the set of indexers a search runs against. Constraining that set by pubkey is allowed (`search-and-selection.md`); ranking, weighting, or pinning indexers, or selecting them by name, is not — it would turn names into decision inputs and reopen the spoofing concerns this design closes.

- Any change to the DITTA wire protocol.

- Cross-broker name syncing.

- Remote/hosted inference.

## 10. Open questions

- What counts as a material change worth a rename: to be settled from observation of real reannouncements.

- Whether the assessment should be suppressed until the broker has routed a minimum number of queries to an indexer (an opinion with no experience behind it is just a second description).

- Visual treatment that makes description vs. assessment unmistakable on mobile.
