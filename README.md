# HTLC Atomic Swap

An HTLC (Hash Time-Locked Contract) atomic swap simulator in a single
pure-Python file, zero dependencies. It models the classic two-party
cross-chain swap behind early trustless exchanges and Lightning-style
payment channels.

## The protocol

Alice wants to trade 50 A-coins for Bob's 8 B-coins with no middleman:

1. Alice picks a random secret `s` and computes `H = sha256(s)`.
2. Alice locks 50 coins on chain A: claimable by Bob **only with `s`**
   before block T1, refundable by Alice at/after T1.
3. Bob mirrors on chain B: 8 coins locked for Alice, claimable with `s`
   before block T2 — with **T2 < T1**, so Alice must claim first and
   Bob always gets his turn afterward.
4. Alice claims Bob's lock, revealing `s` on-chain.
5. Bob copies `s` from that transaction and claims Alice's lock.

Atomicity comes from the construction itself: if any step stalls, the
party that funded the untouched lock takes the **refund** branch — so
either both swaps complete, or everyone gets their coins back. No
half-swap is possible.

## Run it

```sh
python3 htlc.py --demo   # happy path + both refund/timeout stories
python3 htlc.py --test   # 27 doctests
```

Example output:

```
=== HTLC atomic swap: happy path ===
chain A balances: {'alice': 950, 'bob': 50}
chain B balances: {'bob': 892, 'alice': 8}
alice swapped 50 A-coins for bob's 8 B-coins
```

## What's inside

- `Chain` — a tiny ledger with block-height time, balances and locked
  contracts (`fund_htlc`, `claim`, `refund`, `advance`).
- `HTLC` — the contract record: `sha256` hash lock, refund height,
  claimed/refunded state with guards (double-claim, wrong preimage,
  past-deadline claim, early refund).
- `atomic_swap()` — drives the full Alice↔Bob story, including the
  "Bob never funds" and "Alice never claims" failure branches.

Educational toy — the chains are in-memory and the cryptography is
plain `hashlib`, so don't use it for real money. 🙂
