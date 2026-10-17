"use strict";
// Our figures: labeled diagrams of DITTA's parts, drawn as inline SVG.
//
// One cast in every figure: you, the broker, indexers (each named by what it
// covers), a query pill, an invoice card (amount in sats, a QR, the
// indexer's signature), a results card, and the lightning bolt for a payment
// moving. Lines and labels use the page's tokens (see page.css, "figures");
// the accent marks only what moves or matters most in each figure. Motion is
// one short loop per figure, skipped under reduced motion and paused while
// the figure is off screen. Text is set with textContent only.
const Figures = (() => {
  const NS = "http://www.w3.org/2000/svg";
  const W = 360;           // every figure is 360 units wide; heights vary
  const LOOP = 6000;       // ms, one cycle of a figure's motion

  function s(tag, attrs, parent, text) {
    const e = document.createElementNS(NS, tag);
    for (const k in attrs) e.setAttribute(k, attrs[k]);
    if (text != null) e.textContent = text;
    if (parent) parent.append(e);
    return e;
  }
  const g = (p, cls) => s("g", cls ? { class: cls } : {}, p);
  const text = (p, x, y, str, cls, anchor) =>
    s("text", { x, y, class: cls || "lbl", "text-anchor": anchor || "middle" },
      p, str);

  // -- lines ------------------------------------------------------------------
  function head(p, x, y, ang, cls) {
    s("path", { d: "M-6.5 -3.6 L0 0 L-6.5 3.6", class: "ln " + (cls || ""),
                transform: `translate(${x} ${y}) rotate(${ang})` }, p);
  }
  /** A straight line, optionally with an arrowhead at its end (or start). */
  function line(p, x1, y1, x2, y2, cls, arrow) {
    s("line", { x1, y1, x2, y2, class: "ln " + (cls || "") }, p);
    const ang = Math.atan2(y2 - y1, x2 - x1) * 180 / Math.PI;
    if (arrow === "end" || arrow === "both") head(p, x2, y2, ang, cls);
    if (arrow === "start" || arrow === "both") head(p, x1, y1, ang + 180, cls);
  }
  /** A link that has failed: the line breaks around a cross. */
  function broken(p, x1, y1, x2, y2) {
    const mx = (x1 + x2) / 2, my = (y1 + y2) / 2;
    const dx = x2 - x1, dy = y2 - y1, len = Math.hypot(dx, dy);
    const ux = dx / len * 9, uy = dy / len * 9;
    line(p, x1, y1, mx - ux, my - uy, "off");
    line(p, mx + ux, my + uy, x2, y2, "off");
    s("path", { d: `M${mx - 4} ${my - 4} L${mx + 4} ${my + 4} M${mx + 4} ${my - 4} L${mx - 4} ${my + 4}`,
                class: "ln x" }, p);
    return [mx, my];
  }

  // -- the cast -----------------------------------------------------------------
  function you(p, x, y, dim) {
    const e = g(p, "node" + (dim ? " dim" : ""));
    s("rect", { x, y, width: 44, height: 28, rx: 5 }, e);
    text(e, x + 22, y + 18, "you", "nm");
    return { l: [x, y + 14], r: [x + 44, y + 14], c: [x + 22, y + 14] };
  }
  function broker(p, x, y, dim, h = 36) {
    const e = g(p, "node hub" + (dim ? " dim" : ""));
    s("rect", { x, y, width: 72, height: h, rx: 18 }, e);
    text(e, x + 36, y + h / 2 + 4, "broker", "nm");
    return { l: [x, y + h / 2], r: [x + 72, y + h / 2], c: [x + 36, y + h / 2],
             t: [x + 36, y], b: [x + 36, y + h] };
  }
  /** An indexer: a corpus, named by what it covers. */
  function indexer(p, x, y, niche, dim, w = 84, h = 40) {
    const e = g(p, "node" + (dim ? " dim" : ""));
    s("rect", { x, y, width: w, height: h, rx: 6 }, e);
    text(e, x + 9, y + 15, niche, "nm", "start");
    s("path", { d: `M${x + 9} ${y + 25} H${x + w - 14} M${x + 9} ${y + 32} H${x + w - 26}`,
                class: "ln faint" }, e);
    return { l: [x, y + h / 2], r: [x + w, y + h / 2], c: [x + w / 2, y + h / 2] };
  }
  function magnifier(p, x, y) {
    s("circle", { cx: x, cy: y, r: 3.6, class: "ln" }, p);
    s("path", { d: `M${x + 2.6} ${y + 2.6} L${x + 5.6} ${y + 5.6}`, class: "ln" }, p);
  }
  /** The query: a pill with a magnifier and its words. */
  function pill(p, cx, cy, words, cls) {
    const w = words.length * 6.1 + 30, x = cx - w / 2;
    const e = g(p, "pill " + (cls || ""));
    s("rect", { x, y: cy - 10, width: w, height: 20, rx: 10 }, e);
    magnifier(e, x + 12, cy - 1);
    text(e, x + 22, cy + 4, words, "lbl", "start");
    return { w, x };
  }
  const BOLT = "M4.6 0 L0 8.4 H3.9 L2.4 14 L8 5 H4.3 L6.1 0 Z";
  /** The lightning bolt: a payment. */
  function bolt(p, x, y, cls, scale = 1) {
    return s("path", { d: BOLT, class: "bolt " + (cls || ""),
                       transform: `translate(${x} ${y}) scale(${scale})` }, p);
  }
  /** A small QR: three finder squares and a fixed scatter of cells. */
  function qr(p, x, y, size) {
    const e = g(p, "qr"), c = size / 9;
    for (const [fx, fy] of [[0, 0], [6, 0], [0, 6]]) {
      s("rect", { x: x + fx * c, y: y + fy * c, width: 3 * c, height: 3 * c,
                  class: "qrf" }, e);
      s("rect", { x: x + (fx + 1) * c, y: y + (fy + 1) * c, width: c,
                  height: c, class: "qrc" }, e);
    }
    const bits = [[4, 0], [4, 1], [3, 2], [5, 2], [4, 3], [0, 4], [2, 4],
                  [3, 4], [6, 4], [8, 4], [1, 5], [4, 5], [7, 5], [3, 6],
                  [5, 6], [8, 6], [4, 7], [6, 7], [3, 8], [5, 8], [7, 8],
                  [8, 8], [6, 5]];
    for (const [bx, by] of bits) {
      s("rect", { x: x + bx * c, y: y + by * c, width: c, height: c,
                  class: "qrc" }, e);
    }
  }
  /** A signature: a pen stroke, and the word. */
  function signed(p, x, y, label) {
    s("path", { d: `M${x} ${y} c3 -6 5 4 8 -1 s5 -4 7 1 s5 3 8 -2`,
                class: "ln sig" }, p);
    if (label !== false) text(p, x + 27, y + 3, label || "signed", "sm", "start");
  }
  /** The invoice: the amount in sats, a QR, the indexer's signature. */
  function invoice(p, x, y, sats) {
    const e = g(p, "card");
    s("rect", { x, y, width: 120, height: 92, rx: 6 }, e);
    bolt(e, x + 10, y + 9);
    text(e, x + 24, y + 21, sats + " sats", "amt", "start");
    qr(e, x + 10, y + 34, 45);
    text(e, x + 63, y + 46, "invoice", "sm", "start");
    signed(e, x + 63, y + 66, false);
    text(e, x + 63, y + 81, "signed", "sm", "start");
    return e;
  }
  /** A result set: numbered rows (empty rows dashed). */
  function results(p, x, y, w, title, rows, empty) {
    const h = 26 + rows * 12;
    const e = g(p, "card");
    s("rect", { x, y, width: w, height: h, rx: 6 }, e);
    text(e, x + 9, y + 16, title, "nm", "start");
    for (let i = 0; i < rows; i++) {
      const ry = y + 30 + i * 12;
      if (!empty) text(e, x + 9, ry + 3, String(i + 1), "sm", "start");
      s("path", { d: `M${x + (empty ? 9 : 20)} ${ry} H${x + w - 12 - (i % 2) * 14}`,
                  class: "ln " + (empty ? "off" : "faint") }, e);
    }
    return { h };
  }
  /** A small copy of a signed card, for cards in transit. */
  function mini(p, x, y, cls) {
    const e = g(p, "card mini " + (cls || ""));
    s("rect", { x, y, width: 24, height: 17, rx: 3 }, e);
    s("path", { d: `M${x + 4} ${y + 5} H${x + 19}`, class: "ln faint" }, e);
    s("path", { d: `M${x + 4} ${y + 12} c2 -3 3 2 5 -1 s3 -2 5 1`, class: "ln sig" }, e);
    return e;
  }
  function check(p, x, y) {
    s("path", { d: `M${x} ${y} l3 3 l6 -7`, class: "ln ok" }, p);
  }

  // -- motion -------------------------------------------------------------------
  /** A mover travels along points between t0 and t1 (ms) of the loop, and is
   *  hidden the rest of the time. */
  function travel(el, pts, t0, t1) {
    const at = (t) => t / LOOP, tr = ([x, y]) => `translate(${x}px, ${y}px)`;
    const seg = [];
    let total = 0;
    for (let i = 1; i < pts.length; i++) {
      const d = Math.hypot(pts[i][0] - pts[i - 1][0], pts[i][1] - pts[i - 1][1]);
      seg.push(d); total += d;
    }
    const frames = [{ offset: 0, opacity: 0, transform: tr(pts[0]) },
                    { offset: at(t0), opacity: 0, transform: tr(pts[0]) },
                    { offset: at(t0) + 0.005, opacity: 1, transform: tr(pts[0]) }];
    let run = 0;
    for (let i = 1; i < pts.length; i++) {
      run += seg[i - 1];
      frames.push({ offset: at(t0) + 0.005 + (at(t1) - at(t0) - 0.005) * run / total,
                    opacity: 1, transform: tr(pts[i]) });
    }
    frames.push({ offset: Math.min(1, at(t1) + 0.02), opacity: 0,
                  transform: tr(pts[pts.length - 1]) });
    if (frames[frames.length - 1].offset < 1) {
      frames.push({ offset: 1, opacity: 0, transform: tr(pts[pts.length - 1]) });
    }
    return { el, frames };
  }
  /** A part that is always shown, sliding in from an offset between t0 and
   *  t1 and resting in place for the rest of the loop. */
  function slide(el, [dx, dy], t0, t1) {
    const a = t0 / LOOP, b = t1 / LOOP, off = `translate(${dx}px, ${dy}px)`;
    // The loop starts on the resting pose, so a figure paused off screen
    // (or caught before t0) still shows the card in place.
    const home = "translate(0px, 0px)";
    return { el, frames: [{ offset: 0, opacity: 1, transform: home },
                          { offset: a, opacity: 1, transform: home },
                          { offset: a + 0.001, opacity: 0, transform: off },
                          { offset: a + 0.03, opacity: 1, transform: off },
                          { offset: b, opacity: 1, transform: home },
                          { offset: 1, opacity: 1, transform: home }] };
  }
  /** A part that lights up (accent) between t0 and t1. */
  function flash(el, t0, t1) {
    const a = t0 / LOOP, b = t1 / LOOP;
    return { el, frames: [{ offset: 0, opacity: 0 }, { offset: a, opacity: 0 },
                          { offset: a + 0.02, opacity: 1 }, { offset: b, opacity: 1 },
                          { offset: Math.min(1, b + 0.02), opacity: 0 },
                          { offset: 1, opacity: 0 }] };
  }
  /** A dot that carries a message along a path. */
  const dot = (p) => s("circle", { cx: 0, cy: 0, r: 3.4, class: "mover" }, p);

  // -- the figures ------------------------------------------------------------------
  // Each takes its svg and returns { h, motion: [...] }.
  const FIG = {};

  // What DITTA does: a query fans out to independent indexers, one is paid,
  // and one ranked list comes back.
  FIG.fanout = (svg) => {
    const Y = you(svg, 2, 64);
    const B = broker(svg, 142, 60);
    const I = [indexer(svg, 274, 6, "forums"), indexer(svg, 274, 62, "news"),
               indexer(svg, 274, 118, "code")];
    line(svg, Y.r[0], 71, B.l[0], 71, "", "end");
    line(svg, B.l[0], 85, Y.r[0], 85, "", "end");
    pill(svg, 95, 54, "query");
    text(svg, 95, 104, "one ranked list", "sm");
    for (const ix of I) line(svg, B.r[0], B.r[1], ix.l[0], ix.l[1], "", "end");
    bolt(svg, 204, 120);
    text(svg, 216, 132, "21 sats", "sm", "start");
    const lit = bolt(svg, 204, 120, "hot");
    lit.style.opacity = 0;
    const m = [travel(dot(svg), [[48, 71], [142, 71]], 0, 800)];
    for (const ix of I) {
      m.push(travel(dot(svg), [B.r, ix.l], 1000, 1800));
      m.push(travel(dot(svg), [ix.l, B.r], 2700, 3500));
    }
    m.push(flash(lit, 1900, 2600));
    m.push(travel(dot(svg), [[142, 85], [48, 85]], 3700, 4500));
    return { h: 164, motion: m };
  };

  // No results: the broker comes back with an empty results card.
  FIG.noresults = (svg) => {
    const B = broker(svg, 22, 37);
    line(svg, B.r[0], B.r[1], 146, 55, "", "end");
    results(svg, 150, 8, 104, "results", 5, true);
    text(svg, 266, 59, "0 results", "lbl", "start");
    return { h: 110, motion: [] };
  };

  // No indexers reachable: the broker alone, every link broken.
  FIG.nonetwork = (svg) => {
    const B = broker(svg, 36, 64);
    const I = [indexer(svg, 252, 6, "forums", true), indexer(svg, 252, 62, "news", true),
               indexer(svg, 252, 118, "code", true)];
    for (const ix of I) broken(svg, B.r[0], B.r[1], ix.l[0], ix.l[1]);
    text(svg, 180, 160, "unreachable", "sm");
    return { h: 164, motion: [] };
  };

  // The broker didn't answer: the break is between you and it.
  FIG.nobroker = (svg) => {
    const Y = you(svg, 8, 34);
    const B = broker(svg, 280, 30, true);
    const [mx] = broken(svg, Y.r[0], Y.r[1], B.l[0], B.l[1]);
    pill(svg, 108, 30, "query");
    text(svg, mx, 74, "no answer", "sm");
    return { h: 90, motion: [] };
  };

  // Refused: the query comes straight back, marked.
  FIG.refused = (svg) => {
    const Y = you(svg, 8, 34);
    const B = broker(svg, 280, 30);
    line(svg, B.l[0], B.l[1], Y.r[0], Y.r[1], "", "end");
    const p = pill(svg, 168, 48, "query", "bad");
    s("path", { d: `M${p.x + p.w + 6} 44 l8 8 M${p.x + p.w + 14} 44 l-8 8`, class: "ln x" }, svg);
    text(svg, 168, 76, "refused", "lbl");
    return { h: 90, motion: [] };
  };

  // Announcements: indexers announce themselves to each other and to a
  // broker, which files each into a profile with a short history. Signed
  // cards travel the arrows in turn; nothing floats when still.
  FIG.announce = (svg) => {
    const I = [indexer(svg, 2, 6, "forums"), indexer(svg, 138, 6, "code"),
               indexer(svg, 274, 6, "news")];
    line(svg, I[0].r[0], 26, I[1].l[0], 26, "", "both");
    line(svg, I[1].r[0], 26, I[2].l[0], 26, "", "both");
    const B = broker(svg, 40, 122);
    for (const ix of I) line(svg, ix.c[0], 46, B.t[0], B.t[1], "", "end");
    text(svg, 300, 90, "signed announcements", "sm");
    line(svg, B.r[0], B.r[1], 168, B.r[1], "", "end");
    const card = g(svg, "card");
    s("rect", { x: 170, y: 100, width: 188, height: 88, rx: 6 }, card);
    text(card, 179, 116, "profiles", "nm", "start");
    [["forums", "since 26d", "last 1h"], ["code", "since 9d", "last 5m"],
     ["news", "since 2h", "last 2h"]].forEach(([n, since, last], i) => {
      const y = 136 + i * 16;
      text(card, 179, y, n, "sm", "start");
      text(card, 236, y, since, "sm", "start");
      text(card, 300, y, last, "sm", "start");
    });
    const card0 = () => mini(svg, -12, -8, "moving");
    const m = [travel(card0(), [I[0].r, I[1].l], 200, 900),
               travel(card0(), [I[1].r, I[2].l], 1000, 1700)];
    I.forEach((ix, k) => m.push(travel(card0(), [[ix.c[0], 46], B.t],
                                       2000 + k * 250, 2900 + k * 250)));
    m.push(travel(card0(), [B.r, [158, B.r[1]]], 3500, 4100));
    return { h: 194, motion: m };
  };

  // Step 1: a query reaches a broker.
  FIG.step1 = (svg) => {
    const Y = you(svg, 2, 32);
    const B = broker(svg, 286, 28);
    line(svg, Y.r[0], Y.r[1], B.l[0], B.l[1], "", "end");
    pill(svg, 166, 46, "channel jamming");
    return { h: 80, motion: [travel(dot(svg), [Y.r, B.l], 300, 1700)] };
  };

  // Step 2: the broker picks indexers from the announcements it holds.
  FIG.step2 = (svg) => {
    const B = broker(svg, 8, 64);
    mini(svg, 20, 112); mini(svg, 30, 118); mini(svg, 40, 124);
    text(svg, 44, 160, "announcements", "sm");
    text(svg, 44, 172, "it holds", "sm");
    const picks = [["forums", true], ["code", true], ["news", false], ["recipes", false]];
    picks.forEach(([niche, asked], i) => {
      const ix = indexer(svg, 266, 4 + i * 46, niche, !asked, 90, 38);
      line(svg, B.r[0], B.r[1], ix.l[0], ix.l[1], asked ? "acc" : "off", asked ? "end" : null);
      if (asked) check(svg, 238, ix.l[1] - 15);
      text(svg, 222, ix.l[1] - (i < 2 ? 12 : -16), asked ? "asked" : "not asked", "sm", "end");
    });
    return { h: 184, motion: [] };
  };

  // Step 3: a charging indexer answers 402 with a signed invoice.
  FIG.step3 = (svg) => {
    const B = broker(svg, 2, 58);
    const I = indexer(svg, 274, 56, "forums");
    line(svg, I.l[0], I.l[1], B.r[0], B.r[1], "", "end");
    text(svg, 180, 12, "402 payment required", "lbl");
    const card = invoice(svg, 120, 30, "21");
    return { h: 128, motion: [slide(card, [110, 0], 300, 1300)] };
  };

  // Step 4: the broker pays, then sends the identical query with proof.
  // Taller nodes so the payment and the query each meet them evenly,
  // 12 units either side of their centres.
  FIG.step4 = (svg) => {
    const B = broker(svg, 2, 40, false, 56);
    const I = indexer(svg, 274, 40, "forums", false, 84, 56);
    line(svg, B.r[0], 56, I.l[0], 56, "", "end");
    line(svg, B.r[0], 80, I.l[0], 80, "", "end");
    bolt(svg, 146, 34);
    text(svg, 160, 46, "21 sats", "lbl", "start");
    pill(svg, 175, 80, "channel jamming");
    text(svg, 175, 106, "+ proof of payment", "sm");
    text(svg, 175, 120, "same query, same bytes", "sm");
    const b = bolt(svg, 0, 0, "hot moving");
    return { h: 126, motion: [travel(b, [[B.r[0] + 4, 49], [I.l[0] - 14, 49]], 200, 1400),
                              travel(dot(svg), [[B.r[0], 80], [I.l[0], 80]], 1700, 2900)] };
  };

  // Step 5: the indexer answers with its results, best first.
  FIG.step5 = (svg) => {
    const B = broker(svg, 2, 52);
    const I = indexer(svg, 274, 50, "forums");
    line(svg, I.l[0], I.l[1], B.r[0], B.r[1], "", "end");
    const e = g(svg);
    results(e, 128, 24, 104, "results", 3);
    text(svg, 180, 108, "its own order, best first", "sm");
    return { h: 114, motion: [slide(e, [100, 0], 300, 1300)] };
  };

  // Step 6: three result sets become one ranked list.
  FIG.step6 = (svg) => {
    const sets = [["forums", 2], ["code", 60], ["news", 118]];
    const B = broker(svg, 150, 66);
    const m = [];
    for (const [niche, y] of sets) {
      results(svg, 2, y, 96, niche, 2);
      line(svg, 98, y + 25, B.l[0], B.l[1], "", "end");
      m.push(travel(dot(svg), [[98, y + 25], B.l], 300, 1300));
    }
    line(svg, B.r[0], B.r[1], 250, B.r[1], "", "end");
    m.push(travel(dot(svg), [B.r, [250, B.r[1]]], 1500, 2200));
    results(svg, 254, 32, 104, "one list", 6);
    text(svg, 306, 150, "to you", "sm");
    return { h: 172, motion: m };
  };

  // -- mounting -------------------------------------------------------------------
  const reduced = () => matchMedia("(prefers-reduced-motion: reduce)").matches;
  let seen = null;   // one observer pauses figures that are off screen
  function watch(svg, anims) {
    if (!("IntersectionObserver" in window)) return;
    if (!seen) {
      seen = new IntersectionObserver((entries) => {
        for (const en of entries) {
          for (const a of en.target.__anims || []) {
            if (en.isIntersecting) a.play(); else a.pause();
          }
        }
      });
    }
    svg.__anims = anims;
    seen.observe(svg);
  }

  /** Draws figure `name` into el (replacing what is there). ``label`` is
   *  its text alternative; a decorative figure passes none and is hidden. */
  function mount(el, name, label, play) {
    const fig = FIG[name];
    if (!fig) return null;
    const svg = s("svg", { class: "fig", "aria-hidden": label ? "false" : "true" });
    if (label) { svg.setAttribute("role", "img"); svg.setAttribute("aria-label", label); }
    const { h, motion } = fig(svg);
    svg.setAttribute("viewBox", `0 0 ${W} ${h}`);
    el.replaceChildren(svg);
    if (play && motion.length && !reduced() && svg.animate) {
      const anims = motion.map(({ el: m, frames }) =>
        m.animate(frames, { duration: LOOP, iterations: Infinity, easing: "linear" }));
      watch(svg, anims);
    }
    return svg;
  }

  return { mount, names: Object.keys(FIG) };
})();
