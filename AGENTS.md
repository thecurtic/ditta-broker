# AGENTS.md

Instructions for coding agents working in this repository. `CLAUDE.md` is a
symlink to this file; edit this one.

Read [README.md](README.md) for what the broker is and how it runs, and
[DECISIONS.md](DECISIONS.md) before changing anything it covers. DECISIONS.md
is the authority on design choices: don't silently reverse one. If a change
needs to, say so and update the entry.

## Commands

```sh
pip install -e '.[dev]'                 # everything the tests need
python3 -m pytest -q                    # full suite, a few minutes
python3 -m pytest tests/test_conformance.py -q   # the spec's vectors only
python3 tests/demo_stack.py [--paid]    # local demo network + web client
python3 -m broker [web|mcp] broker.yaml # broker + enabled front ends, or one
```

Run the full suite before every commit, and never commit with failures.

## Hard rules

**Clean-room.** The broker is an independent implementation of DITTA, built
from `spec/` and its conformance vectors alone. Don't copy from, import, or
consult the code of any other DITTA implementation.

**`spec/` is vendored byte-for-byte.** Never edit anything under it. The
conformance tests depend on the exact bytes, and `spec/VENDORED` records the
upstream revision. Updating the spec means re-vendoring a new upstream
revision and recording it there.

**The protocol paths stay conformant.** `/announce`, `/peers`, `/query` and
`/corpus` behave exactly as `spec/protocol.md` says. Product features go on
the `/client/*` API, never onto a protocol path or into a protocol message.

**The pipeline contracts hold.** Routing ranks on merit and never reads a
price. Economics chooses who to ask within the ceiling and never reorders
merit. Merge orders result sets, owns de-duplication, and never sees a
price. `tests/test_modules.py` enforces this; don't weaken it to make a
change pass.

**Front ends are clients, not parts of the broker.** `webclient.py` and
`mcp.py` reach the broker over HTTP through `/client` only, and import
nothing of it but `clientlib.py`, `broker.wire` and the package version
(`tests/test_frontends.py` enforces this). If a front end needs something,
add it to the `/client` API for every client. Front ends stay off unless
their config section says `enabled: true`.

**The wire layer is stdlib only.** `broker/wire/` imports nothing outside
the standard library. Elsewhere, third-party packages are imported lazily
and declared as extras in `pyproject.toml`.

## Money and credentials

- No credential ever goes in the repository: not in code, tests, docs,
  examples or commit messages. Configs with real values (`broker.yaml`,
  `.env`) are gitignored; examples use `<placeholders>`.
- The spend credential (the `pay` section) never sits on a host where a
  coding agent runs. Develop and test with no `pay` section or with the
  mock provider. Never ask for, accept, or configure a real spend
  credential.
- The L402 invariants in DECISIONS.md are deliberate: a credential binds the
  exact request bytes, is consumed by service rather than presentation, and
  an empty preimage slot redeems only for the broker's own site.
- The broker never pays a 402 whose signed body doesn't verify against the
  announcement it routed on.

## Security

- Every string in a result set is attacker-controlled. Escape it for its
  render context; treat URLs as data, never fetch them.
- `.onion` names are never resolved or dialed without a Tor proxy.
- The web pages load nothing from third parties at runtime and keep their
  strict Content Security Policy.

## The web front end

- `docs/DESIGN.md` is the visual contract; `docs/search-and-selection.md`,
  `docs/indexer-naming-spec.md` and `docs/explainer-page.md` define behaviour.
  If a task contradicts one of them, flag the conflict instead of choosing.
- Every figure is a labeled diagram of DITTA's own parts, drawn by
  `broker/web/figures.js` with its existing cast (you, the broker, indexers
  named by what they cover, query, invoice, results). Money is a lightning
  bolt with an amount in sats. No stock or library illustrations. Describe a
  new figure and get it approved before building it.
- Pages are plain files in `broker/web/`, assembled at startup. There is no
  build step; don't add one.

## Docs and writing

- When a change makes a non-obvious design choice, record it in
  DECISIONS.md. Keep the README and the specs in `docs/` matching what the
  code does.
- Don't name people anywhere in the repository: code, docs, decisions or
  commit messages. Describe the decision, not who made it.
- Don't name or reference other projects, private hosts, addresses or
  deployments. DITTA and its published repositories are the exception.

## Commits

- Never credit Claude or any other AI tool as an author or co-author. No
  `Co-Authored-By` trailers, no "Generated with" lines in commits or pull
  requests, and never set an AI tool as the git author.
- Write commit messages that describe the change and why it was made.
