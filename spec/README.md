# ditta-spec

The DITTA federated web search protocol.

- `protocol.md` is the specification. It is the only normative document
  here; where anything else disagrees with it, the specification is right.
- `OVERVIEW.md` explains the protocol in plain terms. Read it first.
- `RATIONALE.md` records why each rule is the way it is.
- `vectors/vectors.json` is the executable definition of conformance: an
  implementation is conformant when it passes every vector. The file is
  generated, byte for byte, by `ditta-wire-py`'s `tests/generate.py`.

The reference implementation of the wire format is `ditta-wire-py`, a
sibling repository.

## License

CC0.
