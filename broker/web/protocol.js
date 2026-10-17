"use strict";
// The explainer: figures that carry the content, and one live panel.

// --- figures -----------------------------------------------------------------
// Our labeled diagrams (figures.js). Here they carry the content, so each is
// an image with its text alternative (DESIGN.md §8).
function mountFigures() {
  for (const el of document.querySelectorAll("[data-figure]")) {
    Figures.mount(el, el.dataset.figure, el.getAttribute("aria-label"),
                  el.hasAttribute("data-play"));
    el.removeAttribute("aria-label");   // the svg carries it now
  }
}

// --- the live panel: this broker's view, degrading to one line -------------
const $ = (id) => document.getElementById(id);
function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text != null) e.textContent = text;
  return e;
}
function plural(n, word) {
  return n.toLocaleString() + " " + word + (n === 1 ? "" : "s");
}

async function loadLive() {
  const live = $("live");
  let peers = null, anns = null;
  try {
    [peers, anns] = await Promise.all([
      fetch("/api/peers").then((r) => r.json()),
      fetch("/api/announcements").then((r) => r.json()),
    ]);
  } catch (e) { /* handled below */ }
  agentLink(anns && anns.broker);
  const list = (anns && !anns.error && anns.announcements) || [];
  if (!peers || peers.error || !list.length) {
    live.replaceChildren(el("p", "hint",
      "Live data from this broker isn't available right now."));
    return;
  }
  const known = (peers.indexers || []).length;
  const reachable = (peers.indexers || []).filter(
    (x) => x.searchable !== false).length;
  const counts = el("p");
  counts.append("This broker knows " + plural(known, "indexer") + "; "
    + reachable + (reachable === 1 ? " is" : " are")
    + " reachable right now. ");
  const see = el("a", null, "See them on the search page.");
  see.href = "/#cards";
  counts.append(see);

  // One real, current announcement, exactly as signed: the newest.
  const a = list.slice().sort((x, y) => y.timestamp - x.timestamp)[0];
  const name = ((peers.indexers || []).find((x) => x.pubkey === a.pubkey)
                || {}).name;
  const fig = el("figure", "announcement");
  const pre = el("pre", "mono", JSON.stringify(a, null, 2));
  pre.tabIndex = 0;     // scrollable on its own: reachable by keyboard
  const cap = el("figcaption", "hint");
  cap.append((name ? "The announcement of " + name + ", " : "An announcement, ")
    + "as this broker received it. The pubkey is the indexer's identity; "
    + "the endpoint is where it's reached; the timestamp says which "
    + "announcement from that key is newest; pricing is in millisatoshis; "
    + "the corpus summary describes what it has indexed; and the signature "
    + "covers every other field. ");
  const spec = el("a", null, "The announcement in the specification.");
  spec.href = "https://github.com/ditta-protocol/ditta-spec/blob/main/"
    + "protocol.md#announcement";
  cap.append(spec);
  fig.append(pre, cap);
  live.replaceChildren(counts, fig);
}

// "This broker's client API" links to the API's own description, the
// broker's GET / at the endpoint it announces; only the broker knows it.
function agentLink(broker) {
  const span = $("agent-api-link");
  if (broker && broker.endpoint) {
    const a = el("a", null, span.textContent);
    a.href = broker.endpoint + "/";
    a.title = "This broker's API, described at " + broker.endpoint + "/";
    span.replaceWith(a);
  } else {
    $("agent-api").append(" Its address isn't available right now.");
  }
}

document.addEventListener("DOMContentLoaded", () => {
  mountFigures();
  loadLive();
});
