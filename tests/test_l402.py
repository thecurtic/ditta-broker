"""L402 receiver: body binding, key binding, batch dedupe, claim decoding."""

import hashlib

from broker.payment import L402Receiver, MockInvoiceMinter, MockPayProvider
from broker.payment.l402 import decode_claims


def _receiver(tmp_path):
    return L402Receiver(b"secret", tmp_path / "tokens.db",
                        MockInvoiceMinter())


def _auth(challenge: dict) -> str:
    preimage = MockPayProvider().pay(challenge["invoice"], 10_000)
    return f'L402 {challenge["macaroon"]}:{preimage}'


def test_body_bound_token_redeems_only_against_the_same_bytes(tmp_path):
    receiver = _receiver(tmp_path)
    body = b'{"query":"exact bytes"}'
    body_hash = hashlib.sha256(body).hexdigest()
    auth = _auth(receiver.challenge(1000, "per_query", 1, body_hash))

    other = hashlib.sha256(b'{"query":"rebuilt"}').hexdigest()
    assert not receiver.authorize(auth, "per_query", other)   # wrong bytes
    assert not receiver.authorize(auth, "per_query", None)    # no bytes
    assert receiver.authorize(auth, "per_query", body_hash)   # exact bytes
    assert not receiver.authorize(auth, "per_query", body_hash)  # spent


def test_key_bound_token_redeems_only_under_the_same_pubkey(tmp_path):
    """Payment profile: a credential for a signed query binds to its key; a
    presentation under any other key gets a fresh 402, exactly as expiry."""
    receiver = _receiver(tmp_path)
    auth = _auth(receiver.challenge(1000, "per_query", 1,
                                    bound_pubkey="aa" * 32))
    assert not receiver.authorize(auth, "per_query", pubkey="bb" * 32)
    assert not receiver.authorize(auth, "per_query", pubkey=None)
    assert receiver.authorize(auth, "per_query", pubkey="aa" * 32)


def test_batch_counts_distinct_request_ids_not_presentations(tmp_path):
    """Payment profile: under a multi-use credential a repeated request
    identifier is not counted again."""
    receiver = _receiver(tmp_path)
    auth = _auth(receiver.challenge(1000, "per_batch", 2))
    assert receiver.authorize(auth, "per_batch", request_id="r1")
    assert receiver.authorize(auth, "per_batch", request_id="r1")  # replay
    assert receiver.authorize(auth, "per_batch", request_id="r2")
    assert receiver.authorize(auth, "per_batch", request_id="r1")  # still free
    assert not receiver.authorize(auth, "per_batch", request_id="r3")  # spent


def test_redeemable_peeks_and_only_consume_spends(tmp_path):
    """Consume-on-service: checking a credential costs nothing, however
    often; only an explicit consume — after results were produced — spends."""
    receiver = _receiver(tmp_path)
    auth = _auth(receiver.challenge(1000, "per_query", 1))
    assert receiver.redeemable(auth, "per_query")
    assert receiver.redeemable(auth, "per_query")     # peeking is free
    assert not receiver.redeemable(auth, "per_result")  # checks still apply
    assert receiver.consume(auth)
    assert not receiver.redeemable(auth, "per_query")   # spent by service


def test_empty_preimage_slot_redeems_on_our_nodes_word_alone(tmp_path):
    """Off-protocol: a token we minted, presented with the preimage slot
    empty, redeems once the minter reports the invoice settled — and not
    one moment before. The web page's flow rests on this."""
    minter = MockInvoiceMinter()
    receiver = L402Receiver(b"secret", tmp_path / "tokens.db", minter)
    ch = receiver.challenge(1000, "per_query", 1)
    bare = f'L402 {ch["macaroon"]}:'
    nocolon = f'L402 {ch["macaroon"]}'
    assert not receiver.redeemable(bare, "per_query", witnessed=True)
    assert not receiver.redeemable(nocolon, "per_query", witnessed=True)
    minter.mark_settled(decode_claims(ch["macaroon"])["h"])
    # Settled — but an unwitnessed presentation (an agent) still needs
    # the preimage: to it an empty slot is just a bad credential.
    assert not receiver.redeemable(bare, "per_query")
    assert not receiver.consume(bare)
    assert not receiver.authorize(bare, "per_query")
    assert receiver.redeemable(bare, "per_query", witnessed=True)
    assert receiver.redeemable(nocolon, "per_query", witnessed=True)
    assert receiver.consume(bare, witnessed=True)
    assert not receiver.redeemable(bare, "per_query", witnessed=True)
    # A token from another broker never gets as far as the lookup.
    other = _receiver(tmp_path)
    assert not other.redeemable(bare, "per_query", witnessed=True)


def test_challenge_states_the_expiry_it_records(tmp_path):
    """Payment profile: the expiry MUST be recorded in the macaroon and
    stated in the challenge, so the payer learns when the credential ends."""
    receiver = _receiver(tmp_path)
    ch = receiver.challenge(2500, "per_query", 1)
    assert decode_claims(ch["macaroon"])["e"] == ch["expiry"]
    assert f'macaroon="{ch["macaroon"]}"' in ch["header"]
    assert f'invoice="{ch["invoice"]}"' in ch["header"]


def test_decode_claims_reads_without_the_secret(tmp_path):
    receiver = _receiver(tmp_path)
    body_hash = hashlib.sha256(b"x").hexdigest()
    ch = receiver.challenge(2500, "per_query", 1, body_hash)
    claims = decode_claims(ch["macaroon"])
    assert claims["t"] == "per_query"
    assert claims["q"] == 1
    assert claims["b"] == body_hash
    assert len(claims["h"]) == 64
    assert decode_claims("not a token") is None
