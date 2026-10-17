# DESIGN.md — Broker front end

This is the visual design contract for the DITTA broker's web front end. Read it before any front-end change. It covers how the site looks and reads: concept, colour, type, spacing, figures, the appearance of components, UI writing, accessibility and responsive layout. How features behave (flows, states, scope, spend, refresh) lives in the feature specs, not here.

If a task prompt or spec contradicts this document, don't silently follow either one. Flag the conflict and ask which is intended. If the change is meant to last, update this document.

---

## 1. Context

A broker is one of many front pages to the internet. Each broker has a point of view: which indexers it knows, what it calls them, how it routes and ranks. A searcher chooses a broker the way a reader chooses a paper. The design should feel like that: an editorial, considered view of the network, not a generic search box.

The audience is technical but not necessarily expert: people who care where their results come from.

## 2. Design concept

**The page is a technical drawing of the network.** Thin, precise lines. Facts laid out cleanly. Nothing ornamental. The figures are labeled diagrams of DITTA's own parts (you, the broker, indexers, queries, invoices, results), drawn in the same thin hand as the page.

Spend boldness in one place: the indexer **names** and the **figures**. Everything else is quiet.

## 3. Design principles

1. **One loud thing per view.** Names on cards, the query on results, the figure on empty states. Everything else steps back.
2. **Lines, not boxes.** Structure comes from rules and spacing, not containers, shadows or fills.
3. **Identifiers look like identifiers.** Pubkeys and endpoints are set in mono and never shortened. Nothing else is set in mono.
4. **Status is words first.** Colour supports a status word; it never carries meaning alone.
5. **Quiet motion.** Motion answers an action. The only ambient motion on the search page is its one figure.
6. **Phone first.** Design at phone width. Desktop is the wider version of the phone layout, not the other way round.

## 4. Tokens

All colours, sizes and durations are CSS custom properties on `:root`. Components use tokens only, never raw values.

### 4.1 Colour

One accent, used only for things you can act on, selected states, the "Delivered" evidence mark, and in figures the one part that moves or matters most. Everything else is the grey hierarchy. Indexers are never distinguished by colour — they are told apart by name.

| Token | Light | Dark | Use |
|---|---|---|---|
| `--plate` | `#ffffff` | `#141417` | Page background. |
| `--surface` | `#fafafb` | `#1b1b1f` | Raised areas: open collapsibles, focused input. |
| `--ink` | `#232327` | `#ececf0` | Primary text, indexer names, the broker in figures. |
| `--ink-muted` | `#62626b` | `#a2a2ab` | Secondary text: stats, pubkeys, provenance. |
| `--edge` | `#a4a4ac` | `#6c6c75` | Outlines of interactive controls; lines and outlines in figures. |
| `--rule` | `#c3c3c9` | `#4a4a52` | Dividers between cards and rows. |
| `--faint` | `#e0e0e4` | `#2a2a30` | Receding strokes and hairlines. |
| `--accent` | `#1f57c9` | `#8fb0ff` | Links, primary buttons, focus ring, ticked boxes, the "Delivered" mark. |
| `--accent-ink` | `#ffffff` | `#141417` | Text on an accent fill. |
| `--warn` | `#9a5b00` | `#e3a74a` | Slow, degraded, near spend cap. |
| `--fail` | `#b3261e` | `#f2867d` | Unreachable, errors. |

Theme: follow `prefers-color-scheme` by default; set `data-theme="dark"` or `"light"` on `<html>` when the user overrides. Redefine tokens under `@media (prefers-color-scheme: dark)` guarded with `:root:not([data-theme="light"])`, and again under `:root[data-theme="dark"]`.

### 4.2 Typography

- **Sans:** IBM Plex Sans (400, 500, 600). Its engineering roots suit the drawing concept.
- **Mono:** IBM Plex Mono (400). Only for literal identifiers and machine values: pubkeys, endpoints, raw query strings in API docs. Never for labels or stats.
- Self-host both fonts from our own build. No font CDNs. Fallback stacks:
  - `"IBM Plex Sans", system-ui, -apple-system, "Segoe UI", Roboto, sans-serif`
  - `"IBM Plex Mono", ui-monospace, "SF Mono", Menlo, Consolas, monospace`

Type scale (rem, base 16px):

| Token | Size / line-height | Weight | Use |
|---|---|---|---|
| `--t-display` | 2.0 / 1.15 | 600 | Explainer page title only |
| `--t-name` | 1.5 / 1.2 | 600 | Indexer name on a card |
| `--t-title` | 1.125 / 1.35 | 500 | Result titles, section headings |
| `--t-body` | 1.0 / 1.5 | 400 | Descriptions, snippets, body copy |
| `--t-small` | 0.875 / 1.45 | 400 | Stats, provenance, attribution, helper lines |
| `--t-mono` | 0.8125 / 1.5 | 400 | Pubkeys, endpoints |

Rules: sentence case everywhere. No all-caps labels. No eyebrow labels above headings. No single highlighted word in a heading. Body text blocks `max-width: 65ch`. Numbers in stats and tables use `font-variant-numeric: tabular-nums`.

### 4.3 Space and layout

- Spacing scale (px): `4, 8, 12, 16, 24, 32, 48, 64` as `--s-1` … `--s-8`.
- Page column: `max-width: 44rem`, centred, `padding-inline: var(--s-4)` on phones, `var(--s-6)` from 40rem up.
- Text is left-aligned. Only figures and empty-state blocks are centred.
- Use `viewport-fit=cover` and pad the page with `env(safe-area-inset-*)`.

### 4.4 Lines, corners, elevation

- `--stroke: 0.9px` for every border, rule and outline. Use `1px` where a browser drops sub-pixel borders; never thicker except the focus ring.
- `--r-control: 6px` for inputs, buttons and chips. Cards have no radius because they have no box.
- No shadows. No gradients. No decorative fills.

### 4.5 Motion

- `--d-quick: 120ms`, `--d-move: 200ms`, easing `cubic-bezier(.2, 0, 0, 1)`.
- Motion only answers an action: a collapsible opening, a chip appearing or being removed, a list arriving. No load-in animations, no hover animations on cards or results.
- The one ambient motion on the search page is its figure. On the explainer, the figures for the query's steps move too. Nothing moves under reduced motion.
- Under `prefers-reduced-motion: reduce`, keep opacity changes only.

## 5. Figures

Every figure is ours: a labeled diagram drawn as inline SVG by `broker/web/figures.js`. Nothing is drawn by a third-party library, and nothing is loaded at runtime.

**One cast, the same in every figure:** *you* (a small box), the *broker* (a rounded pill in `--ink`), *indexers* (boxes named by what they cover, such as "forums", "news" and "code", with two lines standing for the corpus), the *query* (a pill with a magnifier and its words, outlined in `--accent`), the *invoice* (a card with the amount in sats beside a lightning bolt, a QR, and the indexer's signature), *results* (a card with numbered rows), and a signed *announcement* card.

**Money is a lightning bolt and sats.** A payment moving is a bolt; amounts are printed in sats. The example price is 21 sats, a nod to bitcoin's 21 million cap. No punch cards, coins or currency glyphs beyond these.

**Words inside the drawing are labels**, small (`--t-small` scale), in Plex, sentence case: the names of the cast, amounts, and short callouts ("asked", "not asked", "402 payment required", "same query, same bytes").

**Lines:** 1px, non-scaling; the grey hierarchy (`--edge` for links, `--rule` and dashed for links not used, `--fail` with a cross for a broken one). **The accent marks one thing per figure:** what moves (a message in transit, the payment) or what was chosen.

| Where | Figure | Shows |
|---|---|---|
| Search page, top of the page | `fanout` | You → broker → three indexers, one paid in sats → one ranked list back. Moves. At 75% of the column (max 22.5rem), then the "How DITTA works" link, then the page heading directly above the search bar. Hidden once results show. |
| No results | `noresults` | The broker returning an empty results card, "0 results". |
| No reachable indexers; a selection that can't run | `nonetwork` | The broker with every link to its indexers broken. |
| The broker isn't answering; a search that didn't reach it | `nobroker` | The break between you and the broker. |
| A refused search | `refused` | The query pill coming back, marked "refused". |
| Explainer: opening | `fanout` | As on the search page. |
| Explainer: two roles | `announce` | Three indexers announcing to each other and to a broker, which files each into a profile with its history. |
| Explainer: the six steps | `step1`–`step6` | Query to broker; broker picks (asked / not asked); 402 with invoice; pay ⚡ then the same query with proof; results, best first; three sets merged into one list. |

Figures are full column width on phones and capped at 30rem (22rem in empty and error blocks). **Figures never appear on indexer cards or result items.** The route map on the results page is drawn from each search's own data and follows the same lines and colours.

## 6. Components (appearance)

### 6.1 Search bar

- Text input and "Search" button side by side; the button wraps below the input under 22rem.
- Input: `--stroke` outline in `--edge`, `--r-control`, min height 48px. On focus the border itself turns bold `--accent` (2px, drawn inside) with a `--surface` background: one edge, never a second ring outside it.
- Scope chips sit directly above the input. Chip: `--stroke` outline in `--edge`, `--r-control`, indexer name in `--t-small` `--ink`, an X at the trailing end with a 44px hit area.
- "Clear all" is a text button after the chips.
- Helper lines under the bar ("Searching 2 of 5 indexers", cost, Tor notices) are `--t-small` `--ink-muted`; warnings use `--warn`.
- One row below the bar holds, in order: the "Exclude Tor indexers" checkbox (always visible), an "Options" disclosure, and a "History (n)" disclosure (shown once there is history). Disclosures are text buttons with a chevron and `aria-expanded`; each opens its panel under the row, separated by a `--rule`.
- "Options" holds Results and Spend cap. The button stays one word; any option not at its default is stated in the helper line under the bar ("Options: spend cap 50 sats."), so a closed panel never hides a setting that shapes the next search.

### 6.2 Results list

- Result: title in `--t-title` `--accent`; URL in `--t-small` `--ink-muted`; snippet in `--t-body`, clamped to three lines; attribution line in `--t-small` reading "from" plus the indexer name in `--ink` weight 500.
- Results are separated by `--rule` lines with `--s-5` vertical rhythm. No boxes.
- Progress and notice lines above the list use `--t-small`.

### 6.3 History

- The "History (n)" disclosure in the search bar's row (6.1). Its panel opens with one `--t-small` `--ink-muted` paragraph saying the history lives only in this browser (the last 20), that the broker keeps no history of the searcher's searches, and that the broker does keep per-indexer records of how each query was answered (its words and results included), not linked to the searcher.
- Entries are rows separated by `--rule`: query in `--t-body`, details as spaced groups in `--t-small` `--ink-muted`, actions (Open, Delete) as text links; "Clear history" is a destructive text link.

### 6.4 Indexer card

Cards are sections of the drawing, separated by a full-width `--rule`, with `--s-6` padding top and bottom. No box, no coloured side border, no background.

Top to bottom:

1. **Name** — `--t-name`, `--ink`. The loudest thing on the card.
2. **Status and endpoint** — a status word appears only when something is wrong: a small outlined pill, `--warn` Degraded, `--fail` Unreachable, neutral "Not contacted yet". Reachable is the normal case and shows no pill. "Tor" as a neutral outlined word when it applies, then the endpoint in `--t-mono` `--ink-muted`. An unreachable or degraded indexer's last error follows on its own line.
3. **Pubkey** — all 64 characters in `--t-mono` `--ink-muted`, `overflow-wrap: anywhere`. "Copy" as a text link beside it.
4. **Stats** — `--t-small`, labels in `--ink-muted` and values in `--ink`, laid out as spaced label–value groups (Documents, Languages, Ingested) that wrap as whole units. Price is not repeated here; it sits beside "Search this". Not a string joined with middle dots, and no term count on the card (machines can still read it from the agent API).
5. **Covers line** — `--t-body` `--ink`.
6. **Provenance** — `--t-small` `--ink-muted`, one sentence: "Described from N results read by the broker." Shown only when the broker has read any of the indexer's results.
7. **Search this** — native checkbox with its label; price beside it in `--t-small` when the indexer charges.
8. **Collapsibles** — "Name history (n)" and "Vocabulary" as text links in a row, with `aria-expanded`; the open one reads in `--ink` weight 500.

### 6.5 Vocabulary table

- Columns: term, share of documents (percentage only — no bars), evidence.
- Evidence: "Delivered" with a check in `--accent`; otherwise the cell is empty. Never repeat "untested" on every row.
- Rows separated by `--rule`; percentages right-aligned with tabular figures.
- Bars are not used anywhere; the percentage stands on its own. The table stays, as the card's Vocabulary view.

### 6.6 Name history

- Plain list, newest first, rows separated by `--rule`: name, then from–to dates in `--t-small` `--ink-muted`. The current name carries a "Current" tag.

### 6.7 Buttons and controls

- **Primary** (Search): `--accent` outline and text on `--plate` at rest; `--accent` fill with `--accent-ink` text on press. One primary per view.
- **Secondary** (New search, view tabs, empty-state actions): `--edge` outline, `--ink` text.
- **Text links** (Copy, Name history, Vocabulary, Open, Delete, Resume, Discard, Clear history, Clear all): no outline, `--t-small`, `--accent`, underline on hover; destructive ones in `--ink-muted`. Lines, not boxes: in-card and in-list actions are words.
- **Destructive** text turns `--fail` on hover and focus only.
- Every control: min 44px touch target (text links keep the height without the box); boxed controls use `--r-control` and a `--stroke` outline.
- Checkboxes are native inputs with `accent-color: var(--accent)`.
- Disabled controls: `--faint` outline, `--ink-muted` text, with the reason shown nearby in words.

### 6.8 Empty and error blocks

- Centred figure, one heading in `--t-title`, at most one line of body, one action. Nothing else.

## 7. Writing

- Name things by what searchers understand, not how the system works: "Search this", not "Add to routing set".
- An action keeps its name through a flow: the button is "Search", the progress line says "Searching".
- Errors say what happened and what to do. They don't apologise and are never vague. Never "Something went wrong".
- No filler: no "This is a search index that…", no "powerful", no "seamless".
- Thousands separators on numbers. Relative, short times: "5h ago", "26d ago".
- Indexer descriptions start with "Covers" and contain no opinions or statistics.

## 8. Accessibility

- Text contrast at least 4.5:1 against `--plate` in both themes.
- Visible focus on every interactive element: 2px `--accent` ring, 2px offset. Text inputs are the exception: their own border turns bold `--accent` instead (6.1), so a field never shows two rings.
- Keyboard: tab order follows reading order; chips can be removed with Backspace when the input is empty, and with Enter or Space on the X. Chip X label: "Remove [name] from search".
- Collapsibles use `<details>`/`<summary>` or a button with `aria-expanded`.
- Progress lines, the scope line and the results use `aria-live="polite"`.
- A figure that carries meaning gets a text alternative (the search page's figure and every explainer figure). A figure in an empty or error block is `aria-hidden`; its heading says the same thing in words.

## 9. Responsive

- Design at 390px wide first. Check at 360, 390, 768 and 1280px.
- One column at every width; wider screens get more margin, not more columns.
- Pubkeys and endpoints wrap; nothing scrolls the page sideways. Tables scroll inside their own container if needed.

## 10. Don't

- Don't use coloured side borders, shadows, gradients or decorative fills.
- Don't put figures on cards or results.
- Don't use monospace for anything that isn't an identifier.
- Don't use all-caps labels or middle-dot meta strings.
- Don't truncate pubkeys.
- Don't add a second accent colour.

## 11. Visual review before calling work done

1. Screenshot every changed page at 390px and 1280px, in light and dark themes, and review the screenshots.
2. Check every component in section 6 against its description.
3. Tab through each page with the keyboard only.
4. Check reduced motion.
