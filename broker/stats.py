"""Cached per-term corpus statistics, fetched from indexers by gossip.

Discovery's second product. Announcements carry only a corpus summary; the
per-term detail — document frequency and freshness per term — is fetched on
demand from each indexer's ``/corpus`` and cached here. Routing consults the
cache to rank indexers against the *terms of the query*, which is what makes
a quote genuinely query-dependent: a query containing a term nobody covers
prices differently from one every indexer covers.

The cache is a hint, like the announcements it derives from: statistics age
between refreshes, and the network may have moved. ``age_seconds`` reports
the oldest fetch backing the cache so a human can be shown why a quote and a
later invoice may disagree.
"""

from __future__ import annotations

import math
import time

# Card-rendering policy defaults (overridable via the config's ``cards``
# section — display policy, not protocol behavior). Chips are ranked by
# DISTINCTIVENESS against a small bundled general-English document-
# frequency prior: score = log(df_here / df_general). A corpus's own raw
# document frequencies cannot say what makes it special — the top of any
# DF list is function words — and cross-corpus contrast (the previous
# design) rewarded whatever tokens the OTHER corpus's tokenizer filtered
# out. A fixed external prior is immune to both failure modes.
MIN_TOKEN_LENGTH = 3
FRESH_WINDOW_SECONDS = 7 * 86400

# Below this coverage a term is noise: three documents prove nothing.
_MIN_COVERAGE = 0.005

# "Distinctive" means MATERIALLY overrepresented: at least 2x the blended
# baseline. Without the margin, two near-identical corpora give every
# shared term a lift of log(total/(total - self)) — a hair above zero —
# and ranking collapses to raw df, surfacing boilerplate (observed live
# on the twin 102M-doc crawls: whatsapp, sitemap, datenschutz...).
MIN_LIFT = math.log(2)

# Function words dropped outright, whatever their score.
STOPWORDS = frozenset("""
the and for that this with you not are was but his her they have from one
had word what all were when your can said there use each which she how
their will other about out many then them these some would like him into
time has look two more write see number way could people than first been
call who oil its now find long down day did get come made may part over
new sound take only little work know place year live back give most very
after thing our just name good sentence man think say great where help
through much before line right too mean old any same tell boy follow came
want show also around form three small set put end does another well
large must big even such because turn here why ask went men read need
land different home move try kind hand picture again change off play
spell air away animal house point page letter mother answer found study
still learn should america world
above across almost along already although among anything are because
been before behind being below between beyond both but can cannot could
did does doing done during each either else enough ever every few

further had has have having her here hers him how however itself its
just least less like made make many may might more most much must near
neither never next none nor not nothing now once only onto others onto
our ours over own per perhaps rather same several shall she since some
something soon such than that their theirs them then there these they
those through thus together too toward towards under until unto upon
very was were what whatever when whenever where whether which while
whom whose why will within without would yet you your yours
don won isn didn doesn wasn aren couldn shouldn wouldn hasn haven
weren ain wont cant dont non etc via per
its dont cant wont didnt doesnt isnt arent wasnt werent im ive ill
youre youve theyre theyve weve lets thats whats heres theres hes shes
""".split())

# Approximate general-English DOCUMENT frequencies (fraction of ordinary
# English documents containing the word at least once). Coarse by design:
# the score is a log ratio, so being off by 2x moves nothing visible.
# Words absent from this table default to _GENERAL_FLOOR — rare in
# general English — which is exactly what makes technical vocabulary win.
_GENERAL_DF = {
    "time": 0.55, "people": 0.40, "year": 0.40, "years": 0.38,
    "make": 0.45, "made": 0.40, "making": 0.25, "used": 0.35,
    "using": 0.30, "use": 0.40, "work": 0.35, "works": 0.20,
    "working": 0.25, "way": 0.40, "ways": 0.20, "day": 0.35,
    "days": 0.30, "world": 0.30, "life": 0.30, "part": 0.35,
    "number": 0.30, "numbers": 0.18, "system": 0.25, "systems": 0.18,
    "state": 0.25, "states": 0.22, "group": 0.22, "case": 0.28,
    "cases": 0.22, "point": 0.30, "points": 0.22, "place": 0.30,
    "government": 0.18, "company": 0.20, "companies": 0.15,
    "problem": 0.25, "problems": 0.22, "question": 0.22, "fact": 0.25,
    "information": 0.25, "data": 0.18, "service": 0.18, "services": 0.16,
    "power": 0.20, "money": 0.20, "water": 0.18, "family": 0.18,
    "history": 0.16, "result": 0.20, "results": 0.20, "level": 0.20,
    "business": 0.18, "process": 0.18, "public": 0.20, "control": 0.16,
    "example": 0.22, "including": 0.25, "important": 0.25,
    "different": 0.28, "possible": 0.22, "available": 0.20,
    "however": 0.30, "although": 0.20, "therefore": 0.15, "based": 0.22,
    "called": 0.25, "found": 0.28, "given": 0.22, "known": 0.22,
    "still": 0.35, "since": 0.28, "while": 0.32, "during": 0.25,
    "between": 0.30, "within": 0.20, "without": 0.25, "against": 0.20,
    "several": 0.20, "become": 0.20, "became": 0.16, "being": 0.30,
    "having": 0.22, "getting": 0.18, "going": 0.28, "really": 0.25,
    "actually": 0.20, "probably": 0.18, "usually": 0.16, "often": 0.22,
    "always": 0.25, "never": 0.25, "every": 0.30, "everything": 0.18,
    "something": 0.28, "anything": 0.20, "nothing": 0.20, "someone": 0.16,
    "does": 0.30, "doing": 0.22, "done": 0.25, "says": 0.20,
    "want": 0.28, "wants": 0.15, "wanted": 0.18, "need": 0.28,
    "needs": 0.20, "needed": 0.18, "better": 0.25, "best": 0.25,
    "least": 0.20, "less": 0.25, "able": 0.20, "sure": 0.22,
    "real": 0.20, "true": 0.18, "free": 0.18, "full": 0.20,
    "high": 0.25, "higher": 0.18, "low": 0.20, "lower": 0.16,
    "open": 0.20, "close": 0.16, "start": 0.20, "started": 0.18,
    "run": 0.20, "running": 0.18, "next": 0.25, "last": 0.30,
    "left": 0.22, "others": 0.20, "along": 0.18, "already": 0.22,
    "though": 0.22, "thought": 0.20, "things": 0.28, "thing": 0.25,
    "means": 0.20, "come": 0.28, "comes": 0.18, "came": 0.22,
    "took": 0.18, "taken": 0.16, "goes": 0.15, "went": 0.20,
    "seen": 0.20, "look": 0.25, "looks": 0.16, "looking": 0.20,
    "post": 0.12, "posts": 0.08, "posted": 0.10, "reply": 0.06,
    "thread": 0.05, "forum": 0.05, "page": 0.20, "site": 0.15,
    "online": 0.12, "email": 0.10, "link": 0.12, "click": 0.10,
    "https": 0.15, "http": 0.12, "www": 0.12, "com": 0.15,
}
_GENERAL_FLOOR = 0.0001

# Interface chrome: terms whose WEB document frequency is huge because
# they appear in every page's furniture (footers, nav, consent banners,
# share buttons), which no prose-frequency prior can see — "contact" is
# rare in running text and present on half the web's pages. Finite and
# enumerable, so enumerated; multilingual because crawls are.
WEB_CHROME = frozenset("""
contact copyright privacy policy policies terms conditions rights
reserved menu home homepage about login logout register signin signup
subscribe unsubscribe newsletter search news blog blogs website site
webpage skip info cookie cookies consent sitemap navigation footer
header share follow facebook twitter instagram youtube linkedin
whatsapp telegram tiktok pinterest reddit email mail account accounts
settings help support faq legal disclaimer accessibility careers press
advertise advertising shop cart checkout products services read click
links related posts comments tags categories category archive archives
previous next prev page pages view views download downloads top back
content main online rss feed update updated publish published author
admin profile password username language languages edit source sources
reply quote print save saved loading submit cancel close open toggle
dropdown button icon logo banner widget sidebar copyright trademark
inc llc ltd gmbh impressum datenschutz kontakt startseite agb
nutzungsbedingungen anmelden abonnieren suche mehr weiter politique
confidentialité mentions légales accueil rechercher connexion
inscription contacto política privacidad buscar inicio registrarse
boletín aviso condiciones privacidade contato buscar código política
please sign signin powered submit address password loading subscribe
unsubscribe comment comments posted author toggle thanks today
""".split())


def _norm_token(t: str) -> str:
    """How a reported term is judged (never how it is displayed): strip
    hashtag/mention sigils and apostrophes, so "it's" is judged as "its",
    "#bitcoin" as "bitcoin"."""
    return t.lstrip("#@").replace("'", "").replace("\u2019", "")


def _eligible(document_count: int, terms: dict, *,
              min_token_length: int) -> dict:
    out = {}
    for t, (dc, newest) in terms.items():
        norm = _norm_token(t)
        if len(norm) < min_token_length or norm.isdigit() \
                or norm in STOPWORDS or norm in WEB_CHROME:
            continue
        if document_count > 0 and dc / document_count < _MIN_COVERAGE:
            continue
        out[t] = (dc, newest)
    return out


def network_context(snapshots: list[tuple[int, dict]]) -> dict:
    """Precomputed network-wide document-frequency shares.

    ``snapshots`` is [(document_count, terms)] for the indexers forming
    the baseline — callers pass the REST of the network, excluding the
    indexer being scored (summing over all j, as the formula literally
    reads, degenerates at small N: a lone indexer becomes its own
    baseline). A term absent from every baseline snapshot gets no network
    share at all; the general-English prior is what keeps such terms
    honest.
    """
    total_docs = sum(max(0, count) for count, _terms in snapshots)
    shares: dict[str, int] = {}
    for _count, terms in snapshots:
        for t, (dc, _newest) in terms.items():
            shares[t] = shares.get(t, 0) + dc
    return {"total_docs": max(1, total_docs), "dfs": shares}


def _distinctiveness(term: str, dc: int, document_count: int,
                     network: dict | None = None) -> float:
    """log-lift of a term here against the LARGER of two baselines: its
    network-wide share and its general-English document frequency.

    The two baselines fail differently, which is why both are needed: the
    general prior misses web boilerplate ("copyright", "facebook" — rare
    in prose, everywhere on the web), the network share misses whatever
    only one tokenizer kept ("while" from a stopword-keeping indexer).
    A term must beat BOTH to count as distinctive.
    """
    if document_count <= 0:
        return 0.0
    baseline = _GENERAL_DF.get(term, _GENERAL_FLOOR)
    if network is not None and term in network["dfs"]:
        baseline = max(baseline,
                       network["dfs"][term] / network["total_docs"])
    return math.log((dc / document_count) / baseline)


def vocabulary_overlap(terms_a: dict, terms_b: dict) -> float:
    """Jaccard overlap of two reported vocabularies (term sets only).

    A cheap first mirror signal: reported top-term sets that mostly agree
    suggest the same corpus (or copied statistics) behind two identities.
    """
    a, b = set(terms_a), set(terms_b)
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def boilerplate_share(terms: dict) -> float:
    """What fraction of a reported vocabulary is function words or very
    common general English — the signature of a raw web-page crawl that
    tokenizes chrome, versus a curated corpus."""
    if not terms:
        return 0.0
    common = sum(1 for t in terms
                 if _norm_token(t) in STOPWORDS
                 or _norm_token(t) in WEB_CHROME
                 or _GENERAL_DF.get(t, 0) >= 0.1)
    return common / len(terms)


def distinctive_terms(document_count: int, terms: dict, limit: int = 12, *,
                      network: dict | None = None,
                      min_token_length: int = MIN_TOKEN_LENGTH) -> list[str]:
    """The chips: what this corpus is ABOUT, by log(df_here / df_general).

    A term common here but common everywhere ("while", "post") scores near
    zero; a term common here and rare in general English ("htlc",
    "ransomware") scores high. Deterministic: score, then size, then term.
    """
    eligible = _eligible(document_count, terms,
                         min_token_length=min_token_length)
    scored = sorted(
        eligible.items(),
        key=lambda kv: (-_distinctiveness(kv[0], kv[1][0], document_count,
                                          network),
                        -kv[1][0], kv[0]))
    out, seen_norms = [], set()
    for t, (dc, _newest) in scored:
        if _distinctiveness(t, dc, document_count, network) < MIN_LIFT:
            continue
        norm = _norm_token(t)
        if norm in seen_norms:
            continue  # "#bitcoin" folds into "bitcoin"
        seen_norms.add(norm)
        out.append(t)
        if len(out) >= limit:
            break
    return out


def recent_terms(document_count: int, terms: dict, exclude: set,
                 limit: int = 3, *, now: float | None = None,
                 network: dict | None = None,
                 fresh_window_seconds: int = FRESH_WINDOW_SECONDS,
                 min_token_length: int = MIN_TOKEN_LENGTH) -> list[str]:
    """What the corpus ingested lately, ranked by the same distinctiveness
    — never input order, so a whole crawl sharing one date stays stable.
    Terms already shown as chips are excluded so the row adds information.
    """
    now = time.time() if now is None else now
    eligible = _eligible(document_count, terms,
                         min_token_length=min_token_length)
    fresh = {t: v for t, v in eligible.items()
             if v[1] >= now - fresh_window_seconds and t not in exclude
             and _distinctiveness(t, v[0], document_count,
                                  network) >= MIN_LIFT}
    scored = sorted(
        fresh.items(),
        key=lambda kv: (-_distinctiveness(kv[0], kv[1][0], document_count,
                                          network),
                        -kv[1][0], kv[0]))
    return [t for t, _v in scored[:limit]]


def term_lift(term: str, dc: int, document_count: int,
              network: dict | None = None) -> float:
    """The blended log-lift, exposed for the vocabulary table."""
    return _distinctiveness(term, dc, document_count, network)


def last_ingest(terms: dict) -> int:
    """The newest document timestamp anywhere in the cached vocabulary."""
    return max((newest for _dc, newest in terms.values()), default=0)


class StatsCache:
    def __init__(self, db_path=None):
        """``db_path`` turns on persistence: cached /corpus snapshots (and
        their fetch times) survive a restart, so quotes and cards are
        right from second one instead of blank until gossip's first
        cycle."""
        # pubkey -> {"terms": {term: (document_count, newest)},
        #            "updated": corpus timestamp, "fetched_at": local time}
        self._by_pubkey: dict[str, dict] = {}
        self._con = None
        if db_path is not None:
            import json
            import sqlite3
            from pathlib import Path

            db_path = Path(db_path)
            db_path.parent.mkdir(parents=True, exist_ok=True)
            self._con = sqlite3.connect(db_path, check_same_thread=False)
            self._con.execute(
                "CREATE TABLE IF NOT EXISTS snapshots ("
                "pubkey TEXT PRIMARY KEY, terms TEXT NOT NULL, "
                "updated INTEGER NOT NULL, fetched_at REAL NOT NULL)")
            self._con.commit()
            for pubkey, terms_json, updated, fetched_at in self._con.execute(
                    "SELECT pubkey, terms, updated, fetched_at "
                    "FROM snapshots").fetchall():
                try:
                    terms = {t: (v[0], v[1]) for t, v
                             in json.loads(terms_json).items()}
                    self._by_pubkey[pubkey] = {
                        "terms": terms, "updated": updated,
                        "fetched_at": fetched_at}
                except Exception:
                    pass  # one bad row must not keep the cache from loading

    def put(self, pubkey: str, terms: dict[str, tuple[int, int]],
            updated: int, *, now: float | None = None) -> None:
        fetched_at = time.time() if now is None else now
        self._by_pubkey[pubkey] = {
            "terms": terms,
            "updated": updated,
            "fetched_at": fetched_at,
        }
        if self._con is not None:
            import json

            self._con.execute(
                "INSERT OR REPLACE INTO snapshots "
                "(pubkey, terms, updated, fetched_at) VALUES (?, ?, ?, ?)",
                (pubkey, json.dumps({t: list(v) for t, v in terms.items()},
                                    separators=(",", ":")),
                 updated, fetched_at))
            self._con.commit()

    def terms(self, pubkey: str) -> dict[str, tuple[int, int]]:
        entry = self._by_pubkey.get(pubkey)
        return entry["terms"] if entry else {}

    def has(self, pubkey: str) -> bool:
        return pubkey in self._by_pubkey

    def fetched_at(self, pubkey: str) -> float | None:
        """Local time of this indexer's cached fetch, or None — persisted,
        so it survives a restart and can seed reachability."""
        entry = self._by_pubkey.get(pubkey)
        return entry["fetched_at"] if entry else None

    def age_for(self, pubkey: str, *,
                now: float | None = None) -> int | None:
        """Age of one indexer's cached fetch, or None if never fetched."""
        entry = self._by_pubkey.get(pubkey)
        if entry is None:
            return None
        now = time.time() if now is None else now
        return max(0, int(now - entry["fetched_at"]))

    def age_seconds(self, *, now: float | None = None) -> int | None:
        """Age of the OLDEST cached fetch, or None when nothing is cached.

        The oldest is the honest number to show next to a quote: the quote
        is only as fresh as the stalest statistics it consulted.
        """
        if not self._by_pubkey:
            return None
        now = time.time() if now is None else now
        oldest = min(e["fetched_at"] for e in self._by_pubkey.values())
        return max(0, int(now - oldest))
