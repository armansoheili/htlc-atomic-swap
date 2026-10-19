#!/usr/bin/env python3
"""HTLC-based atomic swap simulator in pure Python.

Models the classic two-chain atomic swap (Alice <-> Bob) using
Hash Time-Locked Contracts, the construction behind early
cross-chain swaps and Lightning payment channels:

- a *hash lock*: funds are claimable only with knowledge of a secret
  preimage `s` such that `sha256(s) == lock_hash`;
- a *time lock*: if the counterparty never acts, the funder gets a
  refund after a block height deadline.

The second chain's deadline must be earlier than the first's so the
initiator always has time to claim after revealing the secret:

    chain A (Alice funds): refund after T1
    chain B (Bob funds):   refund after T2,  T2 < T1

Happy-path protocol
-------------------
1. Alice picks secret `s`, computes `H = sha256(s)`.
2. Alice locks her coins on chain A for Bob: claimable with `s`
   before block T1, refundable by Alice at/after T1.
3. Bob sees `H` on chain A and mirrors the lock on chain B for Alice:
   claimable with `s` before block T2 (< T1), refundable by Bob
   at/after T2.
4. Alice claims Bob's lock on chain B, revealing `s` on-chain.
5. Bob copies `s` and claims Alice's lock on chain A before T1.

If any step stalls, the party that funded the unclaimed lock takes
the refund branch. Swap stays atomic: either both get the other's
coins, or everyone gets their own coins back.

Usage:
    python3 htlc.py --demo   # run the happy-path + refund stories
    python3 htlc.py --test   # run doctests
"""

import hashlib
import secrets
import sys
from dataclasses import dataclass, field

sha256 = lambda b: hashlib.sha256(b).digest()


# --------------------------------------------------------------------------- #
# Ledger primitives (block-height-time, balances, locked contracts)
# --------------------------------------------------------------------------- #
@dataclass
class HTLC:
    """One hash-time-locked contract on a chain."""
    htlc_id: str
    lock_hash: bytes          # sha256(secret preimage)
    amount: int               # smallest units
    sender: str
    recipient: str
    refund_height: int        # sender may refund at/after this block
    claimed: str | None = None
    refunded: bool = False


@dataclass
class Chain:
    """Minimal ledger with block height, balances and HTLCs."""
    name: str
    balances: dict = field(default_factory=dict)
    htlcs: dict = field(default_factory=dict)
    height: int = 0

    def credit(self, who: str, amount: int) -> None:
        self.balances[who] = self.balances.get(who, 0) + amount

    def fund_htlc(self, htlc_id: str, lock_hash: bytes, amount: int,
                  sender: str, recipient: str, refund_height: int) -> HTLC:
        """Lock `amount` of the sender's balance into an HTLC.

        >>> c = Chain("demo")
        >>> c.credit("alice", 100)
        >>> h = c.fund_htlc("h1", sha256(b"s"), 40, "alice", "bob", 120); assert h.amount == 40
        >>> c.balances["alice"]
        60
        >>> c.fund_htlc("h2", sha256(b"t"), 61, "alice", "bob", 130)
        Traceback (most recent call last):
        ...
        ValueError: alice has only 60 available, tried to lock 61
        """
        if htlc_id in self.htlcs:
            raise ValueError(f"htlc {htlc_id!r} already exists on {self.name}")
        if self.balances.get(sender, 0) < amount:
            raise ValueError(
                f"{sender} has only {self.balances.get(sender, 0)} available,"
                f" tried to lock {amount}"
            )
        if refund_height <= self.height:
            raise ValueError(
                f"refund height {refund_height} must be after current"
                f" height {self.height}"
            )
        self.balances[sender] -= amount
        htlc = HTLC(htlc_id, lock_hash, amount, sender, recipient,
                    refund_height)
        self.htlcs[htlc_id] = htlc
        return htlc

    def claim(self, htlc_id: str, preimage: bytes, claimer: str) -> int:
        """Claim an HTLC by revealing the secret before the deadline.

        >>> c = Chain("demo"); c.credit("alice", 50)
        >>> lock = sha256(b"secret")
        >>> _ = c.fund_htlc("h", lock, 50, "alice", "bob", 100)
        >>> c.claim("h", b"wrong", "bob")
        Traceback (most recent call last):
        ...
        ValueError: preimage does not match the hash lock
        >>> c.claim("h", b"secret", "bob")
        50
        >>> c.balances["bob"]
        50
        >>> c.claim("h", b"secret", "bob")
        Traceback (most recent call last):
        ...
        ValueError: htlc 'h' was already claimed by bob
        """
        htlc = self.htlcs[htlc_id]
        if htlc.claimed is not None:
            raise ValueError(f"htlc {htlc_id!r} was already claimed by"
                             f" {htlc.claimed}")
        if htlc.refunded:
            raise ValueError(f"htlc {htlc_id!r} was already refunded")
        if claimer != htlc.recipient:
            raise ValueError(f"only {htlc.recipient} may claim htlc"
                             f" {htlc_id!r}")
        if sha256(preimage) != htlc.lock_hash:
            raise ValueError("preimage does not match the hash lock")
        if self.height >= htlc.refund_height:
            raise ValueError(f"deadline passed: block {self.height} is at/after"
                             f" refund height {htlc.refund_height}")
        htlc.claimed = claimer
        self.credit(claimer, htlc.amount)
        return htlc.amount

    def refund(self, htlc_id: str) -> int:
        """Refund the sender once the timelock has expired.

        >>> c = Chain("demo"); c.credit("alice", 50)
        >>> _ = c.fund_htlc("h", sha256(b"secret"), 50, "alice", "bob", 10)
        >>> c.refund("h")
        Traceback (most recent call last):
        ...
        ValueError: refund of htlc 'h' locked until block 10 (now 0)
        >>> c.height = 10
        >>> c.refund("h")
        50
        >>> c.balances["alice"]
        50
        """
        htlc = self.htlcs[htlc_id]
        if htlc.claimed is not None:
            raise ValueError(f"htlc {htlc_id!r} was claimed; no refund")
        if htlc.refunded:
            raise ValueError(f"htlc {htlc_id!r} was already refunded")
        if self.height < htlc.refund_height:
            raise ValueError(f"refund of htlc {htlc_id!r} locked until block"
                             f" {htlc.refund_height} (now {self.height})")
        htlc.refunded = True
        self.credit(htlc.sender, htlc.amount)
        return htlc.amount

    def advance(self, blocks: int = 1) -> None:
        """Mine `blocks` empty blocks, moving time forward."""
        if blocks < 0:
            raise ValueError("cannot move time backwards")
        self.height += blocks


# --------------------------------------------------------------------------- #
# Full-swap driver used by --demo and the doctests
# --------------------------------------------------------------------------- #
def atomic_swap(alice_wants_claim: bool = True,
                bob_funds: bool = True) -> dict:
    """Run one Alice<->Bob swap and return the ending balances.

    Chain A holds Alice's coins (she funds first, refund after T1=120);
    chain B holds Bob's coins (he mirrors, refund after T2=100, T2 < T1).

    Happy path (bob_funds=True, alice_wants_claim=True):

    >>> res = atomic_swap()
    >>> res["chain_a"]["alice"], res["chain_a"].get("bob")
    (950, 50)
    >>> res["chain_b"]["alice"], res["chain_b"]["bob"]
    (8, 892)

    If Bob never funds his side, Alice simply refunds (no atomicity
    issue — nothing of Bob's was ever at risk):

    >>> res = atomic_swap(bob_funds=False)
    >>> res["chain_a"]["alice"], res["chain_a"].get("bob", 0)
    (1000, 0)
    >>> res["chain_b"].get("alice", 0), res["chain_b"]["bob"]
    (0, 900)

    If Alice never claims Bob's lock, Bob refunds his side and Alice
    must also refund hers — both walk away whole (no half-swap):

    >>> res = atomic_swap(alice_wants_claim=False)
    >>> res["chain_a"]["alice"], res["chain_a"].get("bob", 0)
    (1000, 0)
    >>> res["chain_b"].get("alice", 0), res["chain_b"]["bob"]
    (0, 900)
    """
    a = Chain("chain-a"); b = Chain("chain-b")
    a.credit("alice", 1000); b.credit("bob", 900)

    # 1) Alice picks a secret and funds her HTLC on chain A.
    secret = secrets.token_bytes(32)
    lock_hash = sha256(secret)
    t1 = a.height + 120
    a.fund_htlc("swap-a", lock_hash, 50, "alice", "bob", t1)

    if not bob_funds:
        # Bob bails: Alice waits out T1 and refunds.
        a.advance(120)
        a.refund("swap-a")
        return {"chain_a": dict(a.balances), "chain_b": dict(b.balances)}

    # 2) Bob mirrors on chain B with an *earlier* deadline.
    t2 = b.height + 100
    assert t2 < t1, "chain B deadline must precede chain A's"
    b.fund_htlc("swap-b", lock_hash, 8, "bob", "alice", t2)

    if alice_wants_claim:
        # 3) Alice claims on chain B, revealing `secret` on-chain.
        b.advance(10)
        b.claim("swap-b", secret, "alice")
        # 4) Bob learns the secret from the claim tx and claims on A.
        a.advance(10)
        a.claim("swap-a", secret, "bob")
    else:
        # Alice ghosts: Bob refunds at T2, Alice refunds at T1.
        b.advance(100)
        b.refund("swap-b")
        a.advance(120)
        a.refund("swap-a")

    return {"chain_a": dict(a.balances), "chain_b": dict(b.balances)}


def demo() -> None:
    print("=== HTLC atomic swap: happy path ===")
    res = atomic_swap()
    print(f"chain A balances: {res['chain_a']}")
    print(f"chain B balances: {res['chain_b']}")
    assert res["chain_a"]["bob"] == 50 and res["chain_b"]["alice"] == 8
    print("alice swapped 50 A-coins for bob's 8 B-coins\n")

    print("=== refund path: bob never funds ===")
    res = atomic_swap(bob_funds=False)
    print(f"chain A balances: {res['chain_a']}")
    assert res["chain_a"]["alice"] == 1000
    print("alice refunded in full\n")

    print("=== timeout path: alice never claims ===")
    res = atomic_swap(alice_wants_claim=False)
    print(f"chain A balances: {res['chain_a']}")
    print(f"chain B balances: {res['chain_b']}")
    assert res["chain_a"]["alice"] == 1000 and res["chain_b"]["bob"] == 900
    print("both sides refunded — no half-swap")


def test() -> None:
    import doctest
    results = doctest.testmod()
    print(f"{results.attempted} doctests, {results.failed} failed")
    if results.failed:
        sys.exit(1)


if __name__ == "__main__":
    if "--test" in sys.argv:
        test()
    else:
        demo()
