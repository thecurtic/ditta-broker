"""Term selection for the cards: distinctive against general English.

Chips are ranked by log(df_here / df_general) over a bundled general-
English document-frequency prior, with a stopword list. This replaced
cross-corpus contrast, which rewarded whatever tokens the OTHER corpus's
tokenizer filtered out; a fixed external prior cannot be gamed by anyone's
tokenizer — see DECISIONS.md.
"""

from broker.stats import distinctive_terms, last_ingest, recent_terms

NOW = 1_760_000_000
DAY = 86400

# A forum-ish corpus that never stripped stopwords — the observed failure.
DIRTY_COUNT, DIRTY = 3000, {
    "the": (2900, NOW), "and": (2800, NOW), "while": (900, NOW),
    "where": (800, NOW), "over": (700, NOW), "who": (600, NOW),
    "2024": (900, NOW), "42": (400, NOW),
    "malware": (600, NOW), "ransomware": (450, NOW), "botnet": (300, NOW),
    "cve": (60, NOW - 30 * DAY),
}


def test_chips_are_distinctive_not_common():
    chips = distinctive_terms(DIRTY_COUNT, DIRTY)
    assert chips[:3] == ["malware", "ransomware", "botnet"]
    for word in ("the", "and", "while", "where", "over", "who", "2024",
                 "42"):
        assert word not in chips


def test_general_english_words_lose_even_off_the_stopword_list():
    # "while" is not necessarily stopword-listed, but its general-English
    # document frequency is high: common-here-and-everywhere scores <= 0
    # and never becomes a chip.
    terms = {"while": (600, 1), "htlc": (30, 1)}
    assert distinctive_terms(3000, terms) == ["htlc"]


def test_noise_floor_short_and_numeric_tokens_drop():
    terms = {"a1": (500, 1), "777": (500, 1), "rare": (2, 1),
             "jamming": (200, 1)}
    assert distinctive_terms(1000, terms) == ["jamming"]


def test_recent_terms_windowed_ranked_and_deduped_against_chips():
    fresh = recent_terms(DIRTY_COUNT, DIRTY,
                         exclude={"malware", "ransomware", "botnet"},
                         now=NOW)
    # Whole-crawl date ties resolve by distinctiveness, never input order;
    # "cve" is outside the freshness window despite being distinctive.
    assert "cve" not in fresh
    assert all(t not in ("the", "and", "while") for t in fresh)


def test_last_ingest_is_the_newest_timestamp_anywhere():
    assert last_ingest(DIRTY) == NOW
    assert last_ingest({}) == 0


def test_network_share_kills_web_boilerplate_the_prior_misses():
    # "copyright" is rare in general English prose but on most web pages:
    # two web crawls both report it, so its network share crushes its lift.
    from broker.stats import network_context

    crawl_a = (1000, {"copyright": (700, 1), "htlc": (40, 1)})
    crawl_b = (2000, {"copyright": (1500, 1), "malware": (300, 1)})
    # The baseline is the REST of the network (self excluded by caller).
    chips = distinctive_terms(*crawl_a, network=network_context([crawl_b]))
    assert chips == ["htlc"]
    # ...while the general prior still kills the stopword-keeping-tokenizer
    # trap: a term the rest of the network never reports gets no alibi.
    lone = (1000, {"while": (300, 1), "jamming": (200, 1)})
    assert distinctive_terms(*lone,
                             network=network_context([crawl_b])) \
        == ["jamming"]
    # An empty baseline (lone indexer) falls back to the prior alone.
    assert distinctive_terms(*lone, network=network_context([])) \
        == ["jamming"]


def test_vocabulary_overlap_and_boilerplate_share():
    from broker.stats import boilerplate_share, vocabulary_overlap

    a = {"alpha": (1, 1), "beta": (1, 1), "gamma": (1, 1)}
    b = {"beta": (9, 1), "gamma": (9, 1), "delta": (9, 1)}
    assert vocabulary_overlap(a, b) == 0.5          # 2 shared of 4 distinct
    assert vocabulary_overlap(a, {}) == 0.0
    chrome = {"the": (1, 1), "copyright": (1, 1), "while": (1, 1),
              "htlc": (1, 1)}
    assert boilerplate_share(chrome) == 0.75        # all but htlc


def test_identical_twins_cancel_to_no_distinctive_vocabulary():
    """Two announcements serving one corpus (the live 102M-doc pair): each
    is the other's baseline, so NOTHING clears the 2x lift margin — the
    honest answer, instead of df-ranked multilingual boilerplate."""
    from broker.stats import network_context

    twin_terms = {"whatsapp": (5_000_000, 1), "sitemap": (4_000_000, 1),
                  "datenschutz": (3_000_000, 1), "htlc": (900_000, 1)}
    twin = (102_000_000, twin_terms)
    small = (3_351, {"cwe": (400, 1)})
    baseline = network_context([twin, small])
    assert distinctive_terms(*twin, network=baseline) == []
    # ...while the small focused corpus still stands out sharply.
    assert distinctive_terms(
        *small, network=network_context([twin, twin])) == ["cwe"]


def test_web_chrome_and_social_tokens_never_chip():
    """The two live failure classes: interface chrome (huge web DOCUMENT
    frequency, invisible to any prose prior) and raw social tokens
    ("it's", "#bitcoin") that slip stopwords on punctuation."""
    terms = {"contact": (330, 1), "copyright": (250, 1), "privacy": (240, 1),
             "menu": (200, 1), "datenschutz": (150, 1), "impressum": (90, 1),
             "it's": (120, 1), "i'm": (100, 1), "don't": (90, 1),
             "#bitcoin": (300, 1), "bitcoin": (400, 1), "nostr": (280, 1)}
    chips = distinctive_terms(1000, terms)
    assert chips == ["bitcoin", "nostr"]   # #bitcoin folded, chrome gone
