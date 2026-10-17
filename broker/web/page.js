"use strict";
const $ = (id) => document.getElementById(id);
const STORE_PREFIX = "ditta-broker.pending.";
const RCPT_PREFIX = "ditta-broker.receipt.";
const VIEW_KEY = "ditta-broker.view";
const HIST_OPEN_KEY = "ditta-broker.historyopen";
const NEW_KEY = "ditta-broker.newsearch"; // ms: "New search" last pressed
const DRAFT_KEY = "ditta-broker.draft";   // sessionStorage: the unsent query
const REOPEN_MS = 30 * 60 * 1000;      // fresh visits reopen results this new
let peersData = null;      // /api/peers payload
let lastQuote = null;      // {quote_msat, indexers_considered, stats_age_s}
let pending = null;        // the active challenge payload
let currentOutcome = null; // outcome behind the rendered results
let currentResults = [];
let currentRequestId = null;
let shownId = null;        // id of the saved search on screen (URL ?r=)
let quoteTimer = null;
let view = "list";         // "list" | "map": how the results are shown
let mapSelected = null;    // pubkey whose results the map's panel lists
let mapAnimate = false;    // the draw-in plays once per result set
// The searcher's selection: pubkeys, in the order ticked. Empty = the full
// network. Held in memory only; a saved search carries its own.
let scope = [];
let shownAsk = null;       // the ask behind the results on screen
let renderedView = null;   // "list" | "group": what #results holds now
let scopeBlocked = null;   // why the next search can't run, or null
try { if (localStorage.getItem(VIEW_KEY) === "map") view = "map"; }
catch (e) {}

function fmtSats(msat) {
  if (msat == null) return "?";
  const s = (msat / 1000).toFixed(3).replace(/\.?0+$/, "");
  return s + " sat" + (s === "1" ? "" : "s");
}
function fmtAge(seconds) {
  if (seconds == null) return "prices not yet cached";
  if (seconds < 90) return "prices cached " + seconds + "s ago";
  return "prices cached " + Math.round(seconds / 60) + "m ago";
}
function fmtWhen(ts) {
  const s = Math.max(0, Math.round(Date.now() / 1000 - ts));
  if (s < 3600) return Math.max(1, Math.round(s / 60)) + "m ago";
  if (s < 86400) return Math.round(s / 3600) + "h ago";
  if (s < 31 * 86400) return Math.round(s / 86400) + "d ago";
  return new Date(ts * 1000).toISOString().slice(0, 10);
}
function fmtAgo(ts) {
  if (!ts) return "";
  const m = Math.round((Date.now() - ts) / 60000);
  if (m < 1) return "just now";
  if (m < 60) return m + "m ago";
  if (m < 1440) return Math.round(m / 60) + "h ago";
  return Math.round(m / 1440) + "d ago";
}
function shortPk(pk) { return pk.slice(0, 8) + "\u2026" + pk.slice(-4); }
function nameOf(pk) {
  const x = ((peersData && peersData.indexers) || []).find(
    (i) => i.pubkey === pk);
  return (x && x.naming && x.naming.name) || null;
}
function labelOf(pk) { return nameOf(pk) || shortPk(pk); }
function mapLabel(pk, maxChars) {
  const n = nameOf(pk);
  if (!n) return shortPk(pk);                 // unnamed: the short pubkey
  if (maxChars && n.length > maxChars) {
    return n.slice(0, Math.max(3, maxChars - 1)) + "…";
  }
  return n;
}
function scrollToCard(pk) {
  for (const card of document.querySelectorAll(".card")) {
    if (card.dataset.pubkey === pk) {
      $("cards").hidden = false;
      card.scrollIntoView({ block: "center", behavior: "smooth" });
      card.classList.add("flash");
      setTimeout(() => card.classList.remove("flash"), 1800);
      return;
    }
  }
}
function busy(on) { $("spinner").hidden = !on; }
function currentAsk() {
  const ask = { q: $("q").value.trim(),
                n: Math.max(1, Math.min(50, parseInt($("n").value) || 10)),
                exclude_onion: $("onion-off").checked };
  const cap = $("cap").value.trim();
  if (cap !== "" && !isNaN(parseInt(cap))) {
    ask.max_msat = Math.max(0, parseInt(cap)) * 1000;
  }
  if (scope.length) ask.pubkeys = scope.slice();
  return ask;
}

// --- step 1: live quote while typing ---------------------------------------
async function refreshQuote() {
  const ask = currentAsk();
  try {
    if (ask.q) sessionStorage.setItem(DRAFT_KEY, ask.q);
    else sessionStorage.removeItem(DRAFT_KEY);
  } catch (e) {}
  if (!ask.q) {
    lastQuote = null;
    $("go").textContent = "Search";
    $("quoteline").textContent = "";
    if (!pending) renderCards(peersData);   // a cleared box starts over
    renderScope();
    return;
  }
  try {
    const params = new URLSearchParams({ q: ask.q, n: ask.n });
    if (ask.max_msat != null) params.set("max_msat", ask.max_msat);
    if (ask.exclude_onion) params.set("exclude_tor", "1");
    if (ask.pubkeys) params.set("pubkeys", ask.pubkeys.join(","));
    const resp = await fetch("/api/quote?" + params.toString());
    const data = await resp.json();
    if (ask.q !== $("q").value.trim()) return;   // a newer quote is coming
    if (data.error === "empty_scope" || data.error === "unknown_pubkey") {
      lastQuote = null;
      $("go").textContent = "Search";
      $("quoteline").textContent = "";
      renderScope(scopeNoteFromQuote(data));
      return;
    }
    if (data.error === "unsatisfiable") {
      lastQuote = null;
      $("go").textContent = "Search";
      $("quoteline").textContent = "Nothing fits under this spend cap"
        + ((data.pubkeys || []).length ? " for " + nameList(data.pubkeys)
           : "") + ". Raise it or clear it.";
      renderScope();
      return;
    }
    if (data.error) throw new Error(data.error);
    lastQuote = data;
    // The cost sits under the bar (DESIGN 6.1); the button stays "Search".
    $("go").textContent = "Search";
    renderScope(scopeNoteFromQuote(data));
    $("quoteline").textContent = (data.quote_msat === 0
      ? "Free. " + plural(data.indexers_considered, "indexer")
        + " can answer this."
      : "About " + fmtSats(data.quote_msat) + ", estimated from "
        + fmtAge(data.stats_age_s) + ".") + optionsNote();
  } catch (e) {
    lastQuote = null;
    $("go").textContent = "Search";
    $("quoteline").textContent = "";
    renderScope();
  }
}
$("q").addEventListener("input", () => {
  clearTimeout(quoteTimer);
  quoteTimer = setTimeout(refreshQuote, 300);
});
for (const id of ["n", "cap"]) {
  $(id).addEventListener("input", () => {
    clearTimeout(quoteTimer);
    quoteTimer = setTimeout(refreshQuote, 300);
  });
}
$("onion-off").addEventListener("change", () => {
  renderScope();
  refreshQuote();
  loadPeers();
});

// --- small builders ---------------------------------------------------------
function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text != null) e.textContent = text;
  return e;
}
function plural(n, word) {
  return n.toLocaleString() + " " + word + (n === 1 ? "" : "s");
}
// Details as spaced groups that wrap whole — never a middle-dot string.
function metaRow(parts, cls) {
  const row = el("div", "meta" + (cls ? " " + cls : ""));
  for (const p of parts.filter(Boolean)) row.append(el("span", null, p));
  return row;
}

// --- empty and error blocks: figure, heading, one line, one action ----------
function dropBlock(container) {
  if (container.querySelector(":scope > .block")) container.replaceChildren();
}
function showBlock(container, { figure, heading, body, action }) {
  const block = el("div", "block");
  if (figure) {
    const fig = el("div", "figure");
    Figures.mount(fig, figure, null, false);   // the heading says it in words
    block.append(fig);
  }
  block.append(el("h2", null, heading));
  if (body) block.append(el("p", null, body));
  if (action) {
    const btn = el("button", null, action.label);
    btn.type = "button";
    btn.addEventListener("click", action.run);
    block.append(btn);
  }
  container.replaceChildren(block);
}

// --- the cards: every indexer, identity first ------------------------------
const REACH = {
  reachable: ["Reachable", "ok"], degraded: ["Degraded", "slow"],
  unreachable: ["Unreachable", "bad"], never: ["Not contacted yet", ""],
};
function priceLabel(x) {
  const p = (x.pricing || []).find((o) => o.type === "per_query");
  return p && p.price > 0 ? fmtSats(p.price) + " per search" : "Free";
}

function buildDetailPane(x, naming, hasHistory, hasVocab) {
  // Name history and vocabulary: two secondary controls in a row, each
  // opening its own view in one shared pane; tapping the open one closes.
  const pane = el("div", "detailpane");
  const tabs = el("div", "detailtabs");
  const body = el("div", "detailbody");
  body.id = "detail-" + x.pubkey;
  body.hidden = true;

  const views = [];
  if (hasHistory) {
    views.push(["Name history (" + naming.history.length + ")",
                () => buildHistory(naming.history)]);
  }
  if (hasVocab) views.push(["Vocabulary", () => buildVocabTable(x)]);

  let open = null;
  for (const [label, build] of views) {
    const btn = el("button", "detailtab textlink", label);
    btn.type = "button";
    btn.setAttribute("aria-expanded", "false");
    btn.setAttribute("aria-controls", body.id);
    btn.addEventListener("click", (ev) => {
      ev.stopPropagation();
      const closing = open === label;
      open = closing ? null : label;
      body.hidden = closing;
      if (!closing) body.replaceChildren(build());
      for (const b of tabs.children) {
        const on = !closing && b === btn;
        b.classList.toggle("on", on);
        b.setAttribute("aria-expanded", on ? "true" : "false");
      }
    });
    tabs.append(btn);
  }
  pane.append(tabs, body);
  return pane;
}

function buildHistory(history) {
  // Newest first, as the broker sends it; the open-ended entry is current.
  const list = el("ul", "histlist");
  for (const h of history) {
    const li = el("li");
    const name = el("div", "hname", h.name);
    if (!h.to) name.append(" ", el("span", "tag", "Current"));
    li.append(name, el("div", "hdates",
      fmtWhen(h.from) + " to " + (h.to ? fmtWhen(h.to) : "now")));
    list.append(li);
  }
  return list;
}

function buildVocabTable(x) {
  const wrap = el("div", "tablewrap");
  const table = el("table", "vocab");
  const head = el("tr");
  head.append(el("th", null, "Term"),
              el("th", "num", "Share of documents"),
              el("th", null, "Evidence"));
  table.append(head);
  for (const v of x.vocabulary) {
    const tr = el("tr");
    const pct = v.pct >= 0.01 ? v.pct.toFixed(2) + "%" : "<0.01%";
    const ev = el("td", "ev");
    // Only demonstrated delivery is marked; untested stays blank.
    if (v.evidence === "delivered") ev.textContent = "✓ Delivered";
    tr.append(el("td", "term", v.term), el("td", "num", pct), ev);
    table.append(tr);
  }
  wrap.append(table);
  return wrap;
}

function buildCard(x) {
  const card = el("section", "card");
  card.dataset.pubkey = x.pubkey;
  const naming = x.naming || {};

  // 1. name — the loudest thing on the card
  card.append(el("h3", "cardname" + (naming.name ? "" : " unnamed"),
                 naming.name || "Unnamed indexer"));

  // 2. status word, transport, endpoint
  const eprow = el("div", "eprow");
  const status = (x.reach && x.reach.status) || null;
  // A status word only when it's news: reachable is the normal case.
  if (status && status !== "reachable") {
    const [word, cls] = REACH[status] || [status, ""];
    eprow.append(el("span", "reach" + (cls ? " " + cls : ""), word));
    if (status === "unreachable") card.classList.add("unreachable");
  }
  if (x.transport === "tor") eprow.append(el("span", "tor", "Tor"));
  eprow.append(el("span", "epval mono", x.endpoint));
  card.append(eprow);
  if (x.reach && x.reach.last_error
      && (status === "unreachable" || status === "degraded")) {
    card.append(el("div", "reacherr" + (status === "degraded" ? " slow" : ""),
      "Last error" + (x.reach.last_error_at
        ? " " + fmtWhen(x.reach.last_error_at) : "")
      + ": " + x.reach.last_error));
  }

  // 3. the full pubkey — the identity, never shortened
  const pkrow = el("div", "pkrow");
  const copy = el("button", "copy textlink", "Copy");
  copy.type = "button";
  copy.setAttribute("aria-label", "Copy pubkey");
  copy.addEventListener("click", async (ev) => {
    ev.stopPropagation();
    try { await navigator.clipboard.writeText(x.pubkey); } catch (e) {}
    copy.textContent = "Copied";
    setTimeout(() => { copy.textContent = "Copy"; }, 1200);
  });
  pkrow.append(el("span", "pkfull", x.pubkey), copy);
  card.append(pkrow);

  // 4. stats as label-value groups (no term count on the card)
  const c = x.corpus || {};
  const stats = el("dl", "stats");
  for (const [label, value] of [
      ["Documents", (c.document_count || 0).toLocaleString()],
      ["Languages", (c.languages || []).length
        ? c.languages.join(", ") : "Not reported"],
      ["Ingested", x.last_ingest ? fmtWhen(x.last_ingest) : "Unknown"]]) {
    const g = el("div", "stat");
    g.append(el("dt", null, label), el("dd", null, value));
    stats.append(g);
  }
  card.append(stats);

  // 5. covers line
  if (naming.description) card.append(el("p", "cdesc", naming.description));

  // 6. provenance: what the description rests on, when the broker has read
  // any of this indexer's results itself
  const eb = naming.evidence_base;
  if (naming.description && eb && eb.observed_results) {
    card.append(el("div", "cbasis", "Described from "
      + plural(eb.observed_results, "result") + " read by the broker."));
  } else if (!naming.name && naming.last_error) {
    card.append(el("div", "cbasis", "Naming failed: " + naming.last_error));
  } else if (!naming.name && naming.status) {
    card.append(el("div", "cbasis", naming.status === "held"
      ? "Not named yet: naming is on hold."
      : "Not named yet: naming is " + naming.status + "."));
  }

  // 7. Search this
  card.append(buildSearchThis(x));

  // 8. collapsibles
  const hasHistory = (naming.history || []).length > 1;
  const hasVocab = (x.vocabulary || []).length > 0;
  if (hasHistory || hasVocab) {
    card.append(buildDetailPane(x, naming, hasHistory, hasVocab));
  }
  return card;
}

function renderCards(data) {
  const holder = $("cards-items");
  holder.replaceChildren();
  const indexers = (data && data.indexers) || [];
  const shown = indexers.filter((x) =>
    !($("onion-off").checked && x.transport === "tor"));
  const searchable = shown.filter((x) => !x.reach
    || x.reach.status === "reachable" || x.reach.status === "degraded");
  $("cards").hidden = shown.length === 0;
  const head = [plural(searchable.length, "indexer") + " searchable right "
                + "now."];
  const resting = shown.length - searchable.length;
  if (resting) {
    head.push(resting + (resting === 1 ? " more is" : " more are")
              + " listed but not answering.");
  }
  if ($("onion-off").checked) head.push("Tor indexers are hidden.");
  $("cards-head").textContent = head.join(" ");
  if (data && !data.error && searchable.length === 0) {
    showBlock($("netblock"), {
      figure: "nonetwork", heading: "No indexers are reachable right now",
      action: { label: "Try again", run: loadPeers } });
  } else {
    dropBlock($("netblock"));
  }
  for (const x of shown) holder.append(buildCard(x));
  if (currentOutcome) applyOutcome(currentOutcome);
}
// --- selection: narrow WHO is asked; the broker still decides the rest ----
const SKIP_WORDS = {
  excluded: "uses Tor", unreachable: "unreachable",
  never_contacted: "not contacted yet", degraded: "degraded",
  budget: "costs more than your spend cap",
  pay_disabled: "charges, and this broker can't pay",
  no_pricing: "has no per-search price",
  fanout: "over the broker's limit for one search",
  low_score: "unlikely to match this search",
  not_routed: "not asked", other: "not asked",
};
const UNSEARCHABLE_WORDS = {
  unreachable: "Unreachable right now", never_contacted: "Not contacted yet",
};
function peerOf(pk) {
  return ((peersData && peersData.indexers) || []).find(
    (i) => i.pubkey === pk) || null;
}
function nameList(pks) {
  const names = pks.map(labelOf);
  if (names.length <= 1) return names.join("");
  return names.slice(0, -1).join(", ") + " and " + names[names.length - 1];
}
function buildSearchThis(x) {
  const row = el("div", "searchthis");
  const label = el("label");
  const box = el("input");
  box.type = "checkbox";
  box.dataset.pubkey = x.pubkey;
  box.checked = scope.includes(x.pubkey);
  const searchable = x.searchable !== false;
  box.disabled = !searchable && !box.checked;
  box.addEventListener("change", () => {
    setScope(box.checked ? scope.concat([x.pubkey])
                         : scope.filter((p) => p !== x.pubkey));
  });
  label.append(box, " Search this");
  row.append(label);
  const price = priceLabel(x);
  if (price !== "Free") row.append(el("span", "price", price));
  if (!searchable) {
    row.append(el("span", "why", UNSEARCHABLE_WORDS[x.unsearchable_reason]
                  || "Can't be searched right now"));
  }
  return row;
}
function setScope(list) {
  scope = Array.from(new Set(list));
  for (const box of document.querySelectorAll(".searchthis input")) {
    box.checked = scope.includes(box.dataset.pubkey);
  }
  renderScope();
  refreshQuote();
}
function renderScope(quoteNote) {
  const chips = $("chips");
  chips.replaceChildren();
  for (const pk of scope) {
    const x = peerOf(pk);
    const chip = el("span", "chip");
    chip.append(el("span", "chipname", labelOf(pk)));
    if (!x) {
      chip.classList.add("gone");
      chip.append(el("span", "chipstate", "no longer listed"));
    } else if (x.searchable === false) {
      chip.classList.add("gone");
      chip.append(el("span", "chipstate", "unreachable"));
    }
    const rm = el("button", "chipx", "\u00d7");
    rm.type = "button";
    rm.setAttribute("aria-label", "Remove " + labelOf(pk) + " from search");
    rm.addEventListener("click", () => {
      setScope(scope.filter((p) => p !== pk));
      $("q").focus();
    });
    chip.append(rm);
    chips.append(chip);
  }
  if (scope.length >= 2) {
    const clear = el("button", "textbtn", "Clear all");
    clear.type = "button";
    clear.addEventListener("click", () => { setScope([]); $("q").focus(); });
    chips.append(clear);
  }
  chips.hidden = scope.length === 0;

  // What the scope can and can't do, before anything is spent.
  const lines = [];
  scopeBlocked = null;
  if (scope.length) {
    const total = ((peersData && peersData.indexers) || []).length;
    lines.push(["Searching " + scope.length + " of " + plural(total,
                "indexer") + ".", ""]);
    const torOut = scope.filter((pk) => {
      const x = peerOf(pk);
      return x && x.transport === "tor" && $("onion-off").checked;
    });
    const down = scope.filter((pk) => {
      const x = peerOf(pk);
      return !x || x.searchable === false;
    });
    const usable = scope.filter((pk) => !torOut.includes(pk)
                                        && !down.includes(pk));
    if (!usable.length && torOut.length && torOut.length === scope.length) {
      scopeBlocked = "All selected indexers use Tor. Untick \u2018Exclude "
        + "Tor indexers\u2019 or choose other indexers.";
    } else if (!usable.length) {
      scopeBlocked = "None of the selected indexers can be searched right "
        + "now. Remove them or choose others.";
    } else {
      if (torOut.length) {
        lines.push([nameList(torOut) + (torOut.length === 1 ? " uses"
          : " use") + " Tor and won't be asked.", "warn"]);
      }
      if (down.length) {
        lines.push([nameList(down) + (down.length === 1 ? " is" : " are")
          + " unreachable and won't be asked.", "warn"]);
      }
    }
  }
  if (!scopeBlocked && quoteNote) {
    if (quoteNote.blocked) scopeBlocked = quoteNote.blocked;
    else if (quoteNote.text) lines.push([quoteNote.text, "warn"]);
  }
  if (scopeBlocked) lines.push([scopeBlocked, "warn"]);
  const holder = $("scopeline");
  holder.replaceChildren(...lines.map(([t, c]) => el("div", c, t)));
  $("go").disabled = scopeBlocked !== null;
}
// The broker's own word on the scope, from the quote: indexers it won't
// ask and why, or why the scope can't run at all.
function scopeNoteFromQuote(data) {
  if (!scope.length || !data) return null;
  if (data.error === "unknown_pubkey") {
    return { blocked: nameList(data.pubkeys || []) + ((data.pubkeys || [])
      .length === 1 ? " is" : " are") + " no longer listed by this broker. "
      + "Remove " + ((data.pubkeys || []).length === 1 ? "it" : "them")
      + " to search." };
  }
  if (data.error === "empty_scope") {
    const why = (data.skipped || []).map((s) => labelOf(s.pubkey) + " "
      + (SKIP_WORDS[s.reason] || "can't be asked"));
    return { blocked: "None of the selected indexers can be asked: "
      + why.join("; ") + "." };
  }
  const skipped = ((data.scope || {}).skipped || []).filter(
    (s) => s.reason !== "excluded" && s.reason !== "unreachable"
           && s.reason !== "never_contacted");
  if (skipped.length) {
    return { text: "Won't be asked: " + skipped.map((s) => labelOf(s.pubkey)
      + " (" + (SKIP_WORDS[s.reason] || "not asked") + ")").join(", ")
      + "." };
  }
  return null;
}
// Backspace in an empty box takes the last chip off.
$("q").addEventListener("keydown", (ev) => {
  if (ev.key === "Backspace" && $("q").value === "" && scope.length) {
    ev.preventDefault();
    setScope(scope.slice(0, -1));
  }
});

async function loadPeers() {
  let data = null;
  try {
    data = await (await fetch("/api/peers")).json();
  } catch (e) { data = { error: "unreachable" }; }
  if (data.error) {
    // Keep whatever listing we had; say plainly that it could not refresh.
    if (!peersData) {
      showBlock($("netblock"), {
        figure: "nobroker", heading: "The broker isn't answering",
        body: "The list of indexers didn't load.",
        action: { label: "Try again", run: loadPeers } });
    }
    return;
  }
  peersData = data;
  renderCards(peersData);
  renderScope();
}

// --- receipt: what the broker actually did ---------------------------------
function resetReceipt() {
  currentOutcome = null;
  $("askdid").hidden = true;
  $("drill").hidden = true;
  for (const card of document.querySelectorAll(".card")) {
    delete card.dataset.stage;
    const state = card.querySelector(".cardstate");
    if (state) state.textContent = "";
  }
  renderMap();
}
// One indexer's part in a search, as a sentence.
function stageLabel(x) {
  if (x.stage === "considered") {
    return "Not asked: " + (SKIP_WORDS[x.dropped] || x.dropped
                             || "not routed") + ".";
  }
  const paid = x.paid_msat > 0 ? ", paid " + fmtSats(x.paid_msat) : "";
  if (x.stage === "responded") {
    return "Returned " + plural(x.result_count || 0, "result")
      + (x.latency_ms != null ? " in " + x.latency_ms + " ms" : "")
      + paid + ".";
  }
  if (x.stage === "failed") {
    return (x.paid_msat > 0 ? "Paid " + fmtSats(x.paid_msat)
            + " and got nothing back" : "Failed")
      + (x.error ? ": " + x.error.slice(0, 80) : "") + ".";
  }
  return x.paid_msat > 0 ? "Paid " + fmtSats(x.paid_msat) + "." : x.stage;
}
function applyOutcome(o) {
  currentOutcome = o;
  const byPk = {};
  for (const x of o.indexers || []) byPk[x.pubkey] = x;
  for (const card of document.querySelectorAll(".card")) {
    const x = byPk[card.dataset.pubkey];
    const state = card.querySelector(".cardstate");
    if (!x) { delete card.dataset.stage; if (state) state.textContent = "";
              continue; }
    card.dataset.stage = x.stage;
    if (state) state.textContent = stageLabel(x);
  }
  // the ask-vs-did line
  const ask = o.ask || {};
  const routed = (o.indexers || []).filter((x) => x.stage !== "considered");
  const responded = routed.filter((x) => x.stage === "responded");
  const failed = routed.filter((x) => x.stage === "failed");
  const considered = (o.indexers || []).length;
  let asked = "Asked for " + plural(ask.n || 0, "result");
  if (ask.max_msat != null) asked += ", up to " + fmtSats(ask.max_msat);
  if (ask.exclude_tor) asked += ", clearnet only";
  let did = "Queried " + routed.length + " of " + plural(considered,
                                                        "indexer");
  if (o.cost_msat > 0) did += " and paid " + fmtSats(o.cost_msat);
  did += "; " + responded.length + " responded";
  if (failed.length) did += ", " + failed.length + " failed";
  did += ", " + plural(o.merged ? o.merged.length : currentResults.length,
                       "result") + " merged.";
  $("askdid").textContent = asked + ". " + did;
  $("askdid").hidden = false;
  $("cards").hidden = false;
  renderMap();
}
function highlightCard(pk, on) {
  for (const card of document.querySelectorAll(".card")) {
    if (card.dataset.pubkey === pk) card.classList.toggle("hl", on);
  }
}
// --- search history: every answered search, last 20, this browser only ----
// Keyed by the outcome's request_id, or a local id when the broker withheld
// the outcome. The URL carries only that id, never the query text.
function newLocalId() {
  const b = new Uint8Array(8);
  crypto.getRandomValues(b);
  return "l" + Array.from(b, (x) => x.toString(16).padStart(2, "0")).join("");
}
function saveReceipt(outcome, results, ask, costMsat, scopeReport) {
  const r = { id: (outcome && outcome.request_id) || newLocalId(),
              ts: Date.now(), q: ask.q, ask: ask, results: results,
              outcome: outcome || null, scope: scopeReport || null,
              cost_msat: costMsat };
  try {
    localStorage.setItem(RCPT_PREFIX + r.id, JSON.stringify(r));
    const keys = [];
    for (let i = 0; i < localStorage.length; i++) {
      const key = localStorage.key(i);
      if (key && key.startsWith(RCPT_PREFIX)) {
        try {
          keys.push([JSON.parse(localStorage.getItem(key)).ts || 0, key]);
        } catch (e) { localStorage.removeItem(key); }
      }
    }
    keys.sort((a, b) => b[0] - a[0]);
    for (const [, key] of keys.slice(20)) localStorage.removeItem(key);
  } catch (e) { /* storage unavailable: this visit still shows them */ }
  renderReceiptList();
  return r;
}
function readReceipt(key) {
  try {
    const r = JSON.parse(localStorage.getItem(key));
    if (!r || !Array.isArray(r.results)) return null;
    r.id = key.slice(RCPT_PREFIX.length);
    if (r.cost_msat == null && r.outcome) r.cost_msat = r.outcome.cost_msat;
    return r;
  } catch (e) { return null; }
}
function findReceipt(id) {
  try { return readReceipt(RCPT_PREFIX + id); } catch (e) { return null; }
}
function listReceipts() {
  const out = [];
  try {
    for (let i = 0; i < localStorage.length; i++) {
      const key = localStorage.key(i);
      if (!key || !key.startsWith(RCPT_PREFIX)) continue;
      const r = readReceipt(key);
      if (r) out.push(r);
    }
  } catch (e) {}
  out.sort((a, b) => (b.ts || 0) - (a.ts || 0));
  return out;
}
function deleteReceipt(id) {
  const r = findReceipt(id);
  try {
    localStorage.removeItem(RCPT_PREFIX + id);
    if (r && sessionStorage.getItem(DRAFT_KEY) === r.q) {
      sessionStorage.removeItem(DRAFT_KEY);
    }
  } catch (e) {}
  if (shownId === id) leaveShown();
  renderReceiptList();
}
function clearHistory() {
  const items = listReceipts();
  if (!items.length) return;
  if (!confirm("Delete all " + items.length + " saved search"
      + (items.length === 1 ? "" : "es") + " from this browser?")) return;
  try {
    for (const r of items) localStorage.removeItem(RCPT_PREFIX + r.id);
    localStorage.removeItem(NEW_KEY);
    sessionStorage.removeItem(DRAFT_KEY);
  } catch (e) {}
  if (shownId) leaveShown();
  renderReceiptList();
}
// The search on screen was deleted: blank the page, and drop its id from
// the current URL so a reload does not go looking for it.
function leaveShown() {
  showBlank("");
  try { history.replaceState({}, "", location.pathname); } catch (e) {}
  refreshQuote();
}
function renderReceiptList() {
  const items = listReceipts();
  const holder = $("receiptitems");
  holder.replaceChildren();
  // The History button shows only when there is history; its panel only
  // when open.
  $("hist-btn").hidden = items.length === 0;
  $("histcount").textContent = "(" + items.length + ")";
  if (!items.length) setDisclosure("hist-btn", false);
  for (const r of items) {
    const div = document.createElement("div");
    div.className = "rcpt" + (r.id === shownId ? " on" : "");
    const q = document.createElement("div");
    q.className = "pq";
    q.textContent = r.q || "(untitled search)";
    const cost = r.cost_msat == null ? ""
      : r.cost_msat === 0 ? "Free" : fmtSats(r.cost_msat);
    const meta = metaRow([plural(r.results.length, "result"), cost,
                          fmtAgo(r.ts)], "pmeta");
    const row = document.createElement("div");
    row.className = "row";
    const open = document.createElement("button");
    open.type = "button";
    open.className = "textlink";
    open.textContent = "Open";
    open.addEventListener("click", () => {
      showReceipt(r);
      pushShown(r, false);
    });
    const drop = document.createElement("button");
    drop.type = "button";
    drop.textContent = "Delete";
    drop.className = "textlink destructive";
    drop.title = "Delete this search from this browser";
    drop.addEventListener("click", () => deleteReceipt(r.id));
    row.append(open, drop);
    div.append(q, meta, row);
    holder.append(div);
  }
}
$("histclear").addEventListener("click", clearHistory);
// The tool row's two disclosures: buttons with aria-expanded, each opening
// its panel under the row. History remembers whether it was left open.
function setDisclosure(id, open) {
  const btn = $(id);
  btn.setAttribute("aria-expanded", open ? "true" : "false");
  $(btn.getAttribute("aria-controls")).hidden = !open;
}
for (const id of ["opt-btn", "hist-btn"]) {
  $(id).addEventListener("click", () => {
    const open = $(id).getAttribute("aria-expanded") !== "true";
    setDisclosure(id, open);
    if (id === "hist-btn") {
      try { localStorage.setItem(HIST_OPEN_KEY, open ? "1" : "0"); }
      catch (e) {}
    }
  });
}
try {
  if (localStorage.getItem(HIST_OPEN_KEY) === "1") setDisclosure("hist-btn", true);
} catch (e) {}
// Options that aren't at their defaults are said under the search box (in
// the quote line), so a closed panel never hides a setting that shapes the
// next search; the button itself stays one word.
function optionsNote() {
  const bits = [];
  const n = parseInt($("n").value) || 10;
  if ($("cap").value.trim() !== "") bits.push("spend cap " + $("cap").value.trim() + " sats");
  if (n !== 10) bits.push(n + " results");
  return bits.length ? " Options: " + bits.join(", ") + "." : "";
}
function optionsLabel() { refreshQuote(); }
for (const id of ["n", "cap"]) $(id).addEventListener("input", optionsLabel);

// --- what is on screen, and the URL that names it --------------------------
function fillAsk(ask) {
  $("q").value = ask.q || "";
  if (ask.n) $("n").value = ask.n;
  $("cap").value = ask.max_msat != null ? ask.max_msat / 1000 : "";
  optionsLabel();
  const tor = ask.exclude_onion !== false;
  if ($("onion-off").checked !== tor) {
    $("onion-off").checked = tor;
    loadPeers();
  }
}
// A payment panel on screen steps aside; its invoice stays in the
// unredeemed list for Resume.
function stowPending() {
  if ($("pay").hidden) return;
  $("pay").hidden = true;
  clearTimeout(settleTimer);
  pending = null;
  renderPendingList();      // the stowed invoice joins the list for Resume
}
function showReceipt(r) {
  stowPending();
  shownId = r.id;
  fillAsk(r.ask || { q: r.q });
  // The chips show the scope this search ran on; changing them later
  // shapes only the next search, never this one or its URL.
  setScope((r.ask && r.ask.pubkeys) || []);
  renderResults(r.results, null, r.ask);
  if (r.outcome) applyOutcome(r.outcome); else resetReceipt();
  $("status").textContent += ", saved " + fmtAgo(r.ts) + ".";
  const head = resultHeader(r.scope, r.cost_msat);
  if (head && r.results.length) $("status").textContent += " " + head;
  $("newsearch").hidden = false;
  renderReceiptList();
  refreshQuote();
}
function showBlank(q) {
  stowPending();
  shownId = null;
  clearResults();
  resetReceipt();
  $("q").value = q;
  $("status").textContent = "";
  $("newsearch").hidden = true;
  renderCards(peersData);
  renderReceiptList();
}
function pushShown(r, replace) {
  try {
    history[replace ? "replaceState" : "pushState"](
      { r: r.id }, "", "?r=" + encodeURIComponent(r.id));
  } catch (e) {}
}
// Show whatever the URL names. On the first load with a bare URL, reopen
// the newest saved results if they are recent and "New search" was not
// pressed since; otherwise restore the unsent query, if any.
function route(initial) {
  const rid = new URLSearchParams(location.search).get("r");
  if (rid) {
    const r = findReceipt(rid);
    if (r) { showReceipt(r); return; }
    showBlank("");
    $("status").textContent = "that search is no longer saved in this "
      + "browser";
    return;
  }
  if (!initial) { showBlank(""); refreshQuote(); return; }
  const last = listReceipts()[0];
  let newAt = 0;
  try { newAt = parseInt(localStorage.getItem(NEW_KEY)) || 0; }
  catch (e) {}
  if (last && Date.now() - last.ts < REOPEN_MS && last.ts > newAt) {
    showReceipt(last);
    pushShown(last, true);
    return;
  }
  try {
    const draft = sessionStorage.getItem(DRAFT_KEY);
    if (draft) { $("q").value = draft; refreshQuote(); }
  } catch (e) {}
}
window.addEventListener("popstate", () => route(false));
$("newsearch").addEventListener("click", () => {
  try {
    localStorage.setItem(NEW_KEY, String(Date.now()));
    sessionStorage.removeItem(DRAFT_KEY);
  } catch (e) {}
  showBlank("");
  try { history.pushState({}, "", location.pathname); } catch (e) {}
  refreshQuote();
  $("q").focus();
});
// Leaving the page: stop the timers so the browser can keep the page in
// its back/forward cache; coming back from it, pick them up again.
window.addEventListener("pagehide", () => {
  clearTimeout(settleTimer);
  clearTimeout(quoteTimer);
});
window.addEventListener("pageshow", (ev) => {
  if (!ev.persisted) return;
  renderPendingList();
  if (pending && !$("pay").hidden) pollSettlement();
});

// --- pending persistence (browser holds the bytes) -------------------------
function persistPending(p) {
  if (!p.payment_hash) return;
  if (!p.q) p.q = $("q").value;
  if (!p.created_at) p.created_at = Date.now();
  try {
    localStorage.setItem(STORE_PREFIX + p.payment_hash, JSON.stringify(p));
  } catch (e) { /* storage unavailable: the page variable still works */ }
  renderPendingList();
}
function clearPending(hash) {
  try { if (hash) localStorage.removeItem(STORE_PREFIX + hash); }
  catch (e) {}
  pending = null;
  $("pay").hidden = true;
  clearTimeout(settleTimer);
  renderPendingList();
}
function listPending() {
  const out = [];
  try {
    for (let i = 0; i < localStorage.length; i++) {
      const key = localStorage.key(i);
      if (!key || !key.startsWith(STORE_PREFIX)) continue;
      try {
        const p = JSON.parse(localStorage.getItem(key));
        if (p && p.payment_hash && p.request_b64) out.push(p);
      } catch (e) { /* one corrupt entry must not hide the rest */ }
    }
  } catch (e) {}
  out.sort((a, b) => (b.created_at || 0) - (a.created_at || 0));
  return out;
}
function renderPendingList() {
  // The invoice on screen has its own panel; the list is for the others.
  const items = listPending().filter((p) => !(pending && !$("pay").hidden
    && p.payment_hash === pending.payment_hash));
  const holder = $("pendingitems");
  holder.replaceChildren();
  $("pendinglist").hidden = items.length === 0;
  renderReceiptList();
  for (const p of items) {
    const div = document.createElement("div");
    div.className = "pend";
    const q = document.createElement("div");
    q.className = "pq";
    q.textContent = p.q || "(untitled search)";
    const meta = metaRow([fmtSats(p.amount_msat), fmtAgo(p.created_at)],
                         "pmeta");
    const row = document.createElement("div");
    row.className = "row";
    const resume = document.createElement("button");
    resume.type = "button";
    resume.textContent = "Resume";
    resume.className = "textlink";
    resume.addEventListener("click", () => {
      if (p.q) $("q").value = p.q;
      const ask = p.ask || {};
      if (ask.n) $("n").value = ask.n;
      $("cap").value = ask.max_msat != null ? ask.max_msat / 1000 : "";
      $("onion-off").checked = ask.exclude_tor === true;
      setScope(Array.isArray(ask.pubkeys) ? ask.pubkeys : []);
      showChallenge(p, "resumed \u2014 if you already paid, your results "
        + "will collect; otherwise discard");
    });
    const drop = document.createElement("button");
    drop.type = "button";
    drop.textContent = "Discard";
    drop.className = "textlink destructive";
    drop.addEventListener("click", () => {
      if (pending && pending.payment_hash === p.payment_hash) {
        clearPending(p.payment_hash);
      } else {
        try { localStorage.removeItem(STORE_PREFIX + p.payment_hash); }
        catch (e) {}
        renderPendingList();
      }
    });
    row.append(resume, drop);
    div.append(q, meta, row);
    holder.append(div);
  }
}

// --- the payment panel ------------------------------------------------------
function showChallenge(p, note) {
  pending = p;
  persistPending(p);
  clearResults();
  resetReceipt();
  $("cards").hidden = true;
  $("pay-amount").textContent = "Invoice: " + fmtSats(p.amount_msat);
  const delta = $("pay-delta");
  if (p.amount_msat != null && p.quote_msat != null
      && p.amount_msat !== p.quote_msat) {
    const diff = p.amount_msat - p.quote_msat;
    delta.textContent = "Quoted ~" + fmtSats(p.quote_msat)
      + ", invoice is " + fmtSats(p.amount_msat) + " ("
      + (diff > 0 ? "+" : "\u2212") + fmtSats(Math.abs(diff))
      + ") \u2014 cached statistics moved between quote and request.";
    delta.hidden = false;
  } else {
    delta.hidden = true;
  }
  $("pay-qr").src = p.qr;
  $("pay-invoice").value = p.invoice;
  $("preimage").value = "";
  $("backup").open = false;
  $("pay").hidden = false;
  renderPendingList();      // now on screen: out of the list
  $("status").textContent = note || "";
  watchSettlement();
}

// --- settlement: the broker sees its own invoice paid; the page follows ---
let settleTimer = null;
let completing = false;    // one replay in flight at a time
let autoCollected = null;  // payment hash the poller already replayed for
function watchSettlement() {
  clearTimeout(settleTimer);
  $("pay-settled").hidden = true;
  autoCollected = null;
  pollSettlement();
}
async function pollSettlement() {
  if (!pending || !pending.payment_hash || $("pay").hidden) return;
  let delay = 3000;
  try {
    const resp = await fetch("/api/pending-status?hash="
      + encodeURIComponent(pending.payment_hash));
    const d = await resp.json();
    if (!pending || $("pay").hidden) return;
    if (d.settled) {
      $("pay-settled").hidden = false;
      $("pay-settled").textContent = d.ready
        ? "\u26a1 payment detected \u2014 your results are already "
          + "fetched; collecting them"
        : "\u26a1 payment detected \u2014 the broker is fetching your "
          + "results";
      // Once per payment: a replay that fails leaves the fallback to the
      // person rather than hammering the broker every poll.
      if (!completing && autoCollected !== pending.payment_hash) {
        autoCollected = pending.payment_hash;
        finish("");
      }
    }
  } catch (e) { delay = 5000; }
  settleTimer = setTimeout(pollSettlement, delay);
}

$("pay-copy").addEventListener("click", async () => {
  const field = $("pay-invoice");
  try { await navigator.clipboard.writeText(field.value); }
  catch (e) { field.select(); document.execCommand("copy"); }
  $("pay-copy").textContent = "Copied";
  setTimeout(() => { $("pay-copy").textContent = "Copy"; }, 1500);
});
$("discard").addEventListener("click", () => {
  clearPending(pending && pending.payment_hash);
  $("status").textContent = "Search discarded. An unpaid invoice simply "
    + "expires.";
});

// --- rendering (textContent only) ------------------------------------------
function buildResult(r) {
  const div = document.createElement("div");
  div.className = "result";
  const rowTop = document.createElement("div");
  rowTop.className = "titlerow";
  let title;
  const safe = typeof r.url === "string" &&
    (r.url.startsWith("http://") || r.url.startsWith("https://"));
  if (safe) {
    title = document.createElement("a");
    title.href = r.url;
    title.target = "_blank";
    title.rel = "noopener noreferrer nofollow";
  } else {
    title = document.createElement("span");
    title.className = "title nolink";
  }
  title.classList.add("title");
  title.textContent = r.title || "(untitled)";
  rowTop.append(title);
  const url = document.createElement("div");
  url.className = "url";
  url.textContent = safe ? r.url : "";
  const snippet = document.createElement("p");
  snippet.className = "snippet";
  snippet.textContent = r.snippet || "";
  div.append(rowTop, url, snippet);
  return div;
}
function clearResults() {
  currentResults = [];
  shownAsk = null;
  renderedView = null;
  dropBlock($("results"));
  $("results").replaceChildren();
}
function decorateResult(r) {
  // One result element with its "from [name]" line — used by the flat
  // list and the by-indexer grouping alike.
  const div = buildResult(r);
  if (r.weak) div.classList.add("weak");
  div.dataset.pks = (r.sources || []).map((x) => x.pubkey).join(",");
  if ((r.sources || []).length) {
    // "from [name]": the name opens that indexer's card
    const src = r.sources[0];
    const attr = el("div", "attr", "from ");
    const name = el("button", "srcname", labelOf(src.pubkey));
    name.type = "button";
    name.title = "Served by " + r.sources.map(
      (x) => labelOf(x.pubkey) + " (rank " + x.rank + ")").join(", ");
    name.addEventListener("mouseenter",
      () => highlightCard(src.pubkey, true));
    name.addEventListener("mouseleave",
      () => highlightCard(src.pubkey, false));
    name.addEventListener("click", () => scrollToCard(src.pubkey));
    attr.append(name);
    if (r.sources.length > 1) {
      attr.append(" and " + (r.sources.length - 1) + " more");
    }
    div.append(attr);
  }
  return div;
}
// ``ask`` is the search these results answer (kept for re-renders);
// ``keepStatus`` re-draws the list as another view without touching the
// status line the search wrote.
function renderResults(results, secs, ask, keepStatus) {
  currentResults = results;
  if (ask !== undefined) shownAsk = ask;
  renderedView = view === "group" ? "group" : "list";
  mapSelected = null;
  mapAnimate = true;
  const out = $("results");
  dropBlock(out);
  out.replaceChildren();
  $("drill").hidden = true;
  if (!results.length) {
    if (!keepStatus) $("status").textContent = "";
    const q = (shownAsk && shownAsk.q) || $("q").value.trim();
    const scoped = shownAsk && shownAsk.pubkeys && shownAsk.pubkeys.length;
    showBlock(out, {
      figure: "noresults", heading: "No results for \u2018" + q + "\u2019",
      action: scoped
        ? { label: "Search the full network",
            run: () => { setScope([]); $("f").requestSubmit(); } }
        : { label: "Try different words",
            run: () => { $("q").focus(); $("q").select(); } } });
    return;
  }
  const strong = results.filter((r) => !r.weak).length;
  if (!keepStatus) $("status").textContent = plural(strong, "result")
    + (results.length > strong
       ? ", plus " + (results.length - strong) + " below the line" : "")
    + (secs != null ? " in " + secs.toFixed(1) + "s" : "");
  if (view === "group") {
    renderGrouped(results, out);
    return;
  }
  let dividerPlaced = false;
  for (const r of results) {
    if (r.weak && !dividerPlaced) {
      dividerPlaced = true;
      const divider = document.createElement("div");
      divider.id = "divider";
      divider.textContent = "Below this line the broker doesn't think the "
        + "results answer your search. They are shown anyway, in its best "
        + "order.";
      out.append(divider);
    }
    out.append(decorateResult(r));
  }
}

// --- the route map: the whole fan-out in one picture ------------------------
// Broker on the left, one node per indexer, the merged list on the right.
// With the outcome record it shows every indexer considered; without it
// (expose_outcome off) it falls back to the sources[] on the results, so
// the map always has something true to draw.
const SVGNS = "http://www.w3.org/2000/svg";
function svgEl(tag, attrs, text) {
  const el = document.createElementNS(SVGNS, tag);
  for (const k in attrs) el.setAttribute(k, attrs[k]);
  if (text != null) el.textContent = text;
  return el;
}
function keptBySource() {
  const kept = {};
  currentResults.forEach((r, j) => {
    for (const src of r.sources || []) {
      if (!kept[src.pubkey]) kept[src.pubkey] = [];
      kept[src.pubkey].push({ own: src.rank, merged: j + 1, result: r });
    }
  });
  return kept;
}
function mapNodes(kept) {
  const o = currentOutcome;
  if (o && (o.indexers || []).length) return o.indexers;
  const transports = {};
  for (const x of (peersData && peersData.indexers) || []) {
    transports[x.pubkey] = x.transport;
  }
  return Object.keys(kept).map((pk) => (
    { pubkey: pk, stage: "responded", paid_msat: 0,
      transport: transports[pk] || "clearnet" }));
}
function setView(v) {
  view = v;
  try { localStorage.setItem(VIEW_KEY, v); } catch (e) {}
  renderMap();
}
function renderMap() {
  const kept = keptBySource();
  const nodes = mapNodes(kept);
  const has = nodes.length > 0;
  const showMap = has && view === "map";
  const showGroup = has && view === "group";
  $("viewtabs").hidden = !has;
  $("tab-list").classList.toggle("on", has && view === "list");
  $("tab-map").classList.toggle("on", showMap);
  $("tab-group").classList.toggle("on", showGroup);
  $("map").hidden = !showMap;
  $("results").hidden = showMap;
  const want = showGroup ? "group" : (!showMap && view === "list")
    ? "list" : null;
  if (has && want && renderedView !== want) {
    // A different VIEW of the same results — never a filter or re-query.
    renderResults(currentResults, null, undefined, true);
  }
  if (!showMap) return;
  $("drill").hidden = true;
  if (!nodes.some((x) => x.pubkey === mapSelected)) mapSelected = null;
  drawMap(nodes, kept);
  renderMapDetail(nodes, kept);
}
// The short facts under a node's name, as separate segments (laid out with
// space between them — never a middle-dot string), cut to what fits.
// The map's boxes are narrow on a phone: short forms of the skip reasons
// (the detail panel and the results header use the full sentences).
const MAP_SKIP = {
  excluded: "uses Tor", unreachable: "unreachable",
  never_contacted: "not contacted", degraded: "degraded",
  budget: "over your cap", pay_disabled: "can't pay it",
  no_pricing: "no price", fanout: "over the limit",
  low_score: "unlikely match",
};
function mapFacts(x, n) {
  if (x.stage === "considered") {
    return ["not asked", MAP_SKIP[x.dropped] || ""].filter(Boolean);
  }
  if (x.stage === "failed") {
    return [x.paid_msat > 0 ? "paid, got nothing" : "failed"];
  }
  if (x.stage === "responded") {
    const f = x.result_count != null
      ? [plural(x.result_count, "result"), n + " kept"]
      : [n + " in the list"];
    if (x.latency_ms != null) f.push(x.latency_ms + " ms");
    if (x.paid_msat > 0) f.push("paid " + fmtSats(x.paid_msat));
    return f;
  }
  return [x.stage];
}
function drawMap(nodes, kept) {
  const holder = $("map-svg");
  const W = Math.max(280, Math.floor(holder.clientWidth || 640));
  const R = 26, rowH = 56, boxH = 42, gap = W < 480 ? 28 : 72;
  const H = Math.max(nodes.length * rowH, 2 * R + 48) + 16;
  const cy = H / 2, bx = R + 1, mx = W - R - 1;
  const x0 = bx + R + gap, x1 = mx - R - gap;
  const top = (H - nodes.length * rowH) / 2 + rowH / 2;
  const count = (x) => (kept[x.pubkey] || []).length;
  const animate = mapAnimate;
  mapAnimate = false;
  const curve = (ax, ay, zx, zy) => {
    const m = (ax + zx) / 2;
    return "M" + ax + " " + ay + " C" + m + " " + ay + " " + m + " " + zy
      + " " + zx + " " + zy;
  };
  const chars = (px, size) => Math.floor(px / (size * 0.56));

  const svg = svgEl("svg", { width: W, height: H,
    viewBox: "0 0 " + W + " " + H, role: "group",
    "aria-label": "Route map: the broker, the indexers it considered, "
      + "and the merged list" });
  const edges = svgEl("g", {}), boxes = svgEl("g", {});
  svg.append(edges, boxes);

  const light = (pk, on) => {
    for (const e of svg.querySelectorAll("[data-pk]")) {
      if (e.dataset.pk === pk) e.classList.toggle("hot", on);
    }
    svg.classList.toggle("has-hot", on);
    highlightCard(pk, on);
  };

  nodes.forEach((x, i) => {
    const y = top + i * rowH;
    const n = count(x);
    const asked = x.stage !== "considered";
    const failed = x.stage === "failed";
    const state = !asked ? "off" : failed ? "bad" : "on";

    const out = svgEl("path", { d: curve(bx + R, cy, x0, y),
      class: "edge " + state });
    out.dataset.pk = x.pubkey;
    edges.append(out);
    if (x.stage === "responded") {
      const back = svgEl("path", { d: curve(x1, y, mx - R, cy),
        class: "edge " + (n ? "on" : "off") });
      back.dataset.pk = x.pubkey;
      edges.append(back);
      if (animate && n) {
        back.setAttribute("pathLength", 1);
        back.classList.add("draw");
        back.style.animationDelay = "0.35s";
      }
    }
    if (animate && asked) {
      out.setAttribute("pathLength", 1);
      out.classList.add("draw");
    }

    const facts = mapFacts(x, n);
    const g = svgEl("g", { class: "mnode " + state, tabindex: 0,
      role: "button", "aria-label": labelOf(x.pubkey) + ": "
        + stageLabel(x) });
    g.dataset.pk = x.pubkey;
    g.append(svgEl("title", {}, x.pubkey));
    g.append(svgEl("rect", { x: x0, y: y - boxH / 2, width: x1 - x0,
      height: boxH, rx: 6 }));
    const inner = x1 - x0 - 20;
    const name = svgEl("text", { x: x0 + 10, y: y - 3, class: "name" },
      mapLabel(x.pubkey, chars(inner - (x.transport === "tor" ? 30 : 0),
                               12.5)));
    if (x.transport === "tor") {
      name.append(svgEl("tspan", { class: "torm", dx: 8 }, "Tor"));
    }
    g.append(name);
    // facts as spaced segments, as many as fit
    const sub = svgEl("text", { x: x0 + 10, y: y + 13, class: "sub" });
    let used = 0;
    facts.forEach((f, k) => {
      const need = f.length + (k ? 3 : 0);
      if (k && used + need > chars(inner, 11)) return;
      sub.append(svgEl("tspan", { dx: k ? 10 : 0 }, f));
      used += need;
    });
    g.append(sub);
    g.addEventListener("click", () => selectMapNode(x.pubkey));
    g.addEventListener("keydown", (ev) => {
      if (ev.key === "Enter" || ev.key === " ") {
        ev.preventDefault();
        selectMapNode(x.pubkey);
      }
    });
    for (const [evt, on] of [["mouseenter", true], ["mouseleave", false],
                             ["focus", true], ["blur", false]]) {
      g.addEventListener(evt, () => light(x.pubkey, on));
    }
    boxes.append(g);
  });

  const o = currentOutcome;
  const broker = svgEl("g", { class: "hub" });
  broker.append(svgEl("circle", { cx: bx, cy: cy, r: R }));
  broker.append(svgEl("text", { x: bx, y: cy + 4, "text-anchor": "middle" },
    "Broker"));
  if (o && o.cost_msat > 0) {
    broker.append(svgEl("text", { x: 1, y: cy + R + 18, class: "cap" },
      "paid " + fmtSats(o.cost_msat)));
  }
  const merged = svgEl("g", { class: "hub tap" });
  merged.append(svgEl("title", {}, "The merged list. Tap to clear the "
    + "selection."));
  merged.append(svgEl("circle", { cx: mx, cy: cy, r: R }));
  merged.append(svgEl("text", { x: mx, y: cy + 6, class: "big",
    "text-anchor": "middle" }, String(currentResults.length)));
  merged.append(svgEl("text", { x: W - 1, y: cy + R + 18, class: "cap",
    "text-anchor": "end" }, "merged"));
  merged.addEventListener("click", () => {
    if (mapSelected) selectMapNode(mapSelected);
  });
  svg.append(broker, merged);

  holder.replaceChildren(svg);
  markMapSelection();
}
function markMapSelection() {
  const svg = $("map-svg").firstChild;
  if (!svg) return;
  svg.classList.toggle("has-sel", mapSelected !== null);
  for (const e of svg.querySelectorAll("[data-pk]")) {
    e.classList.toggle("sel", e.dataset.pk === mapSelected);
  }
}
function selectMapNode(pk, scroll) {
  mapSelected = mapSelected === pk && !scroll ? null : pk;
  const kept = keptBySource();
  markMapSelection();
  renderMapDetail(mapNodes(kept), kept);
  if (scroll) $("map").scrollIntoView({ block: "nearest" });
}
function renderMapDetail(nodes, kept) {
  const panel = $("map-detail");
  panel.replaceChildren();
  const hint = (text) => panel.append(el("div", "hint", text));
  const x = nodes.find((node) => node.pubkey === mapSelected);
  if (!x) {
    hint("Tap an indexer to see the results it returned. Solid lines were "
      + "asked, dashed lines were not, red lines failed.");
    return;
  }
  const h = el("h2");
  const name = el("button", "srcname", labelOf(x.pubkey));
  name.type = "button";
  name.title = "Open this indexer's card";
  name.addEventListener("click", () => scrollToCard(x.pubkey));
  h.append(name);
  if (x.transport === "tor") h.append(el("span", "tor", "Tor"));
  panel.append(h);

  const mine = (kept[x.pubkey] || []).slice().sort((a, b) => a.own - b.own);
  const raw = currentOutcome && currentOutcome.raw
    && currentOutcome.raw[x.pubkey];
  if (x.stage !== "responded") {
    hint(stageLabel(x));
    return;
  }
  let rows;
  if (raw) {
    hint(stageLabel(x) + " Its full list, in its own order, and where "
      + "the merge put each result.");
    rows = raw.map((r, i) => {
      const hit = mine.find((m) => m.own === i + 1);
      return { own: i + 1, merged: hit ? hit.merged : null, result: r };
    });
  } else {
    hint((x.result_count != null ? stageLabel(x) + " " : "")
      + "Showing the " + plural(mine.length, "result") + " that reached "
      + "the merged list; this broker doesn't publish full result sets.");
    rows = mine;
  }
  for (const row of rows) {
    const div = el("div", "mres");
    div.append(el("span", "fate" + (row.merged ? " kept" : ""),
      row.merged ? "#" + row.own + " became #" + row.merged
                 : "#" + row.own + " dropped"),
      buildResult(row.result));
    panel.append(div);
  }
}
$("tab-list").addEventListener("click", () => setView("list"));
$("tab-map").addEventListener("click", () => setView("map"));
$("tab-group").addEventListener("click", () => setView("group"));
let mapResizeTimer = null;
window.addEventListener("resize", () => {
  clearTimeout(mapResizeTimer);
  mapResizeTimer = setTimeout(() => {
    if (!$("map").hidden) renderMap();
  }, 150);
});

// ask: what was searched for; costMsat: what the person paid, for when the
// broker withheld the outcome record.
function handleOutcome(data, hashToClear, secs, ask, costMsat) {
  busy(false);
  stopProgress();
  if (data.results) {
    if (data.credential_retained) {
      // No indexer answered, and the broker did not spend the credential:
      // the pending payment stays valid, so keep it and invite a retry.
      $("status").textContent = "No indexer answered, so your payment "
        + "was not spent. Press Get results to try again.";
      $("backup").open = true;
      return;
    }
    clearPending(hashToClear);
    const charged = typeof data.charged_msat === "number"
      ? data.charged_msat : costMsat;
    renderResults(data.results, secs, ask);
    if (data.outcome) applyOutcome(data.outcome); else resetReceipt();
    const head = resultHeader(data.scope, charged);
    if (head && data.results.length) {
      $("status").textContent += ". " + head;
    }
    const saved = saveReceipt(data.outcome, data.results, ask, charged,
                              data.scope);
    shownId = saved.id;
    pushShown(saved, false);
    $("newsearch").hidden = false;
    renderReceiptList();
  } else if (data.payment_required) {
    showChallenge(data, data.note);
  } else if (data.unsettled) {
    $("status").textContent = data.note || "Payment not seen yet.";
    autoCollected = null;   // a false alarm: let the poller try again
  } else if (data.error === "unknown_pubkey"
             || data.error === "empty_scope"
             || data.error === "malformed_scope") {
    // The scope changed under us (delisted, gone unreachable): say which,
    // and never widen to the full network on the searcher's behalf.
    $("status").textContent = "";
    const note = scopeNoteFromQuote(data);
    showBlock($("results"), {
      figure: "nonetwork", heading: "This search can't run as selected",
      body: note && note.blocked ? note.blocked
        : "The selection couldn't be read. Clear it and choose again.",
      action: { label: "Search the full network",
                run: () => { setScope([]); $("f").requestSubmit(); } } });
    refreshQuote();
  } else if (data.price_exceeded) {
    $("status").textContent = "The price rose past the estimate. The "
      + "new estimate is below the search box; press Search to accept it.";
    refreshQuote();
  } else {
    $("status").textContent = "";
    showBlock($("results"), {
      figure: "refused", heading: "The broker refused this search",
      body: data.error ? "It said: " + data.error : "It gave no reason.",
      action: { label: "Try again", run: () => $("f").requestSubmit() } });
  }
}

function renderGrouped(results, out) {
  // Groups keyed by each result's highest-ranked source, ordered by the
  // merge rank of each group's best result; merge order kept within.
  const groups = [];
  const byPk = {};
  for (const r of results) {
    const pk = ((r.sources || [])[0] || {}).pubkey || "unknown";
    if (!byPk[pk]) {
      byPk[pk] = { pubkey: pk, items: [] };
      groups.push(byPk[pk]);
    }
    byPk[pk].items.push(r);
  }
  for (const g of groups) {
    const head = document.createElement("div");
    head.className = "grouphead";
    const gname = document.createElement("div");
    gname.className = "gname";
    const gbtn = el("button", "srcname", labelOf(g.pubkey));
    gbtn.type = "button";
    gbtn.addEventListener("click", () => scrollToCard(g.pubkey));
    gname.append(gbtn, " ", el("span", "gcount",
                               plural(g.items.length, "result")));
    const gmeta = document.createElement("div");
    gmeta.className = "gmeta mono";
    gmeta.textContent = g.pubkey;
    head.append(gname, gmeta);
    out.append(head);
    for (const r of g.items) out.append(decorateResult(r));
  }
}

// --- progress: one line, then one ranked list (no live trickle) -----------
let slowTimer = null;
function startProgress() {
  const q = lastQuote && lastQuote.scope ? lastQuote.scope.queried.length
    : lastQuote ? lastQuote.indexers_considered : 0;
  const line = q ? "Asking " + plural(q, "indexer") + "\u2026"
                 : "Searching\u2026";
  $("status").textContent = line;
  clearTimeout(slowTimer);
  slowTimer = setTimeout(() => {
    if ($("status").textContent === line) {
      $("status").textContent = line + " Some are slow to answer.";
    }
  }, 3000);
}
function stopProgress() { clearTimeout(slowTimer); }

// The results header: who answered, who didn't, who wasn't asked, and the
// spend — from the broker's effective-scope report.
function resultHeader(sc, chargedMsat) {
  const parts = [];
  if (sc) {
    const answered = sc.queried.filter((pk) => !sc.failed.includes(pk));
    if (sc.failed.length) {
      parts.push("Results from " + answered.length + " of "
        + plural(sc.queried.length, "indexer") + ". "
        + nameList(sc.failed) + " didn't answer.");
    }
    if (sc.skipped && sc.skipped.length) {
      parts.push("Not asked: " + sc.skipped.map((x) => labelOf(x.pubkey)
        + " (" + (SKIP_WORDS[x.reason] || "not asked") + ")").join(", ")
        + ".");
    }
  }
  if (chargedMsat > 0) parts.push("Spent " + fmtSats(chargedMsat) + ".");
  return parts.join(" ");
}

// --- step 2: send the query -------------------------------------------------
$("f").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const ask = currentAsk();
  if (!ask.q || scopeBlocked) return;
  startProgress();
  busy(true);
  clearResults();
  resetReceipt();
  const t0 = performance.now();
  try {
    const body = { ...ask,
                   quote_msat: lastQuote ? lastQuote.quote_msat : 0 };
    const resp = await fetch("/api/search", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify(body),
    });
    handleOutcome(await resp.json(), null,
                  (performance.now() - t0) / 1000, ask, 0);
  } catch (e) {
    busy(false);
    stopProgress();
    $("status").textContent = "";
    showBlock($("results"), {
      figure: "nobroker", heading: "The search didn't reach the broker",
      body: "Check your connection, then try again.",
      action: { label: "Try again", run: () => $("f").requestSubmit() } });
  }
});

// --- steps 3-4: pay, replay (preimage only as the fallback) -----------------
async function finish(preimage) {
  if (!pending || completing) return;
  completing = true;
  const p = pending;
  const sent = p.ask || {};
  const ask = { q: p.q || sent.query || "", n: sent.n,
                exclude_onion: sent.exclude_tor === true };
  if (sent.max_msat != null) ask.max_msat = sent.max_msat;
  if (Array.isArray(sent.pubkeys) && sent.pubkeys.length) {
    ask.pubkeys = sent.pubkeys.slice();
  }
  $("status").textContent = "Collecting your results\u2026";
  busy(true);
  const t0 = performance.now();
  try {
    const resp = await fetch("/api/complete", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({
        request_b64: pending.request_b64,
        macaroon: pending.macaroon,
        preimage: preimage,
        quote_msat: pending.quote_msat,
      }),
    });
    handleOutcome(await resp.json(), p.payment_hash,
                  (performance.now() - t0) / 1000, ask, p.amount_msat);
  } catch (e) {
    busy(false);
    $("status").textContent = "The page couldn't reach the broker. Your "
      + "payment is kept: press Get results to try again.";
  } finally {
    completing = false;
  }
}
$("finish").addEventListener("click",
  () => finish($("preimage").value.trim()));

// --- boot: cards, pending, receipts -----------------------------------------
Figures.mount($("hero-fig"), "fanout", "How a search travels: your query "
  + "goes to a broker, which asks several independent indexers, pays the one "
  + "that charges in sats over Lightning, and sends back one ranked list.",
  true);

renderPendingList();
loadPeers();
route(true);
