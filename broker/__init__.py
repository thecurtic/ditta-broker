"""A broker for the federated search protocol.

A broker accepts a query, decides which indexers to ask, pays them, merges
what comes back, and returns results. It speaks the protocol defined in
``spec/protocol.md`` and is built against that spec and its conformance
vectors alone, sharing no code with any other implementation.
"""

__version__ = "0.0.1"
