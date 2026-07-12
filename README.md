# credit-ledger

[![tests](https://github.com/Igfray/pantheon-credit-ledger/actions/workflows/ci.yml/badge.svg)](https://github.com/Igfray/pantheon-credit-ledger/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/credit-ledger)](https://pypi.org/project/credit-ledger/)
[![Python](https://img.shields.io/badge/python-3.10%2B-blue)](https://pypi.org/project/credit-ledger/)
[![License](https://img.shields.io/badge/license-Apache%202.0-blue)](LICENSE)

**An atomic, overdraft-proof, idempotent credit ledger — for metering money on a pay-per-use or multi-tenant service.** ~150 lines of Python over Postgres. Extracted from [PANTHEON](https://pantheonlabs.co.uk), a multi-tenant AI substrate, where it's the money path for autonomous AI agents that spend real credits on every turn.

The interesting part isn't the size — it's that three properties that are usually gotten *subtly wrong* are each guaranteed by a single, boring database mechanism instead of application-level hope:

| Property | How | Not by |
|---|---|---|
| **No overdraft, ever** — a balance can never go negative, even under a stampede of concurrent charges | one guarded `UPDATE … WHERE credits >= n` | `SELECT` then `UPDATE` (a race), or a mutex (doesn't survive multiple processes) |
| **Exactly-once under retries** — a replayed charge (a Stripe webhook fired twice, a client retry) charges once | `UNIQUE(tenant_id, idempotency_key)` on an append-only ledger | de-duping in app code (another race) |
| **Tenant isolation** — tenant A can't read or write tenant B's balance | Postgres row-level security, `FORCE`d | a `WHERE tenant_id = ?` you have to remember to add every time |

## The core: one statement does the hard part

A spend is a single guarded, atomic `UPDATE`:

```sql
UPDATE credit_balance
   SET credits = credits - :n
 WHERE tenant_id = :t AND credits >= :n
RETURNING credits;
```

- The row lock **serialises** concurrent charges against the same tenant — no lost updates.
- The `credits >= :n` guard makes overdraft **structurally impossible**: the charge either fully succeeds (a row comes back) or changes nothing (zero rows). There is no window between the check and the debit, because they're the same statement.

That's the whole trick, and the test proves it: fire **100 concurrent spends of 1 credit against a balance of 50**, and *exactly 50* succeed — the balance lands at 0, never negative, with 50 ledger rows.

```python
# tests/test_metering.py
def test_atomic_decrement_under_concurrency(engine, tenant):
    metering.grant(tenant, 50, engine=engine)
    with ThreadPoolExecutor(max_workers=20) as ex:
        results = list(ex.map(lambda _: metering.decrement(tenant, 1, engine=pool).ok, range(100)))
    assert sum(results) == 50                              # exactly the balance, no more
    assert metering.balance(tenant, engine=engine) == 0.0  # never negative
```

## Idempotency: the ledger *is* the dedup

Every grant and spend appends a row to `credit_ledger`, which carries a `UNIQUE(tenant_id, idempotency_key)`. Pass an `idempotency_key` (a Stripe `event_id`, a request id) and a replay can't insert a second row — the database rejects it, and the code returns the *original* result marked `deduped=True`. Money is never double-counted, and a duplicate never surfaces as a 500.

The subtle bit is the concurrent race: two identical charges arrive at once, both past the "already seen?" check, both try to insert. One wins; the other's `INSERT` violates the constraint, which **rolls back its whole transaction — including the decrement**. The loser then re-reads the ledger, finds the winner's row, and returns that. So a concurrent duplicate is a clean dedup, not a double-charge and not an error. There's a test for exactly this — 20 concurrent replays of one Stripe event → *one* credit applied, zero exceptions.

## The correctness detail most people miss: fail loud, not phantom-success

An `IntegrityError` after retries is ambiguous — it could be a genuine concurrent dedup (the winner committed, there's a ledger row), **or** it could be a foreign-key violation because the tenant doesn't exist (nothing landed). Reporting success in the second case would silently lose money. So the code disambiguates by re-reading the ledger: a row means real dedup → report success; no row means nothing happened → **fail loud** (`ok=False`). This came out of an adversarial self-audit of the substrate; it's the difference between "looks fine" and "correct."

## The RLS footgun this quietly avoids

Row-level security is only a guarantee if the writer is actually subject to it. Metering runs on a *privileged* app role (the money path is infrastructure), and on any managed Postgres that role is **not** `BYPASSRLS`. So before every transaction the code binds the tenant into a session variable the policy reads:

```python
c.execute(text("SELECT set_config('app.current_tenant', :t, true)"), {"t": tenant_id})
```

Skip this and, the moment RLS is enforced in production, the guarded `UPDATE` matches zero rows and the `INSERT`'s `WITH CHECK` fails — **billing silently bricks**, while every dev box (superuser, RLS bypassed) looks perfectly fine. Getting this right is the difference between a demo and something you'd run.

## Why so little code

The database does the concurrency, the atomicity, and the isolation. The code's entire job is to *use those primitives correctly* — bind the tenant, guard the decrement, key the ledger, and disambiguate the one genuinely-ambiguous failure. Fewer moving parts is the point, not an accident.

## Use it

```bash
pip install -e .                       # SQLAlchemy is the only runtime dep
createdb ledger && psql "$DATABASE_URL" -f schema.sql
```

```python
from credit_ledger import metering

metering.grant(tenant_id, 100, idempotency_key="stripe:evt:abc")  # top up (Stripe-retry-safe)
c = metering.decrement(tenant_id, 1, reason="chat-turn")          # spend
if not c.ok:
    ...  # out of credits — nothing was charged
metering.balance(tenant_id)                                       # -> float
```

Every function also takes an explicit `engine=` (for your own pool/lifecycle). `schema.sql` is the full Postgres schema including the RLS policies.

## Run the tests

```bash
DATABASE_URL=postgresql://localhost/ledger pytest -q     # needs Postgres + schema.sql applied
```

The suite is the spec: overdraft impossibility under 100-way concurrency, idempotent spend and grant retries, concurrent Stripe-replay dedup, and rejection of non-positive grants (a negative "grant" would decrement a tenant — the primitive refuses it before touching the DB).

## Benchmark: does it hold at load?

There's a reproducible load benchmark in [`benchmarks/`](benchmarks/) — it fires thousands of concurrent charges at a **single** balance row (the worst case: every charge serialises on the same lock) and re-checks the invariant afterwards. A real run on stock Postgres 16 in Docker (durable commit per charge, 32 workers), 10,000 charges against a balance of 5,000:

```
throughput   ~1,100 charges/sec        p50 ~22 ms · p95 ~77 ms · p99 ~138 ms
succeeded     5,000  (= the balance, exactly)      rejected 5,000 (insufficient)
ledger rows   5,000  (one per real debit)          final balance 0.0 (never negative)
INVARIANT HELD ✓
```

That's the *pessimal* number — one contended row, throughput bounded by a single durable round-trip. Charges spread across many tenants hit different rows and don't contend, so real throughput scales with Postgres, not this ceiling. The point isn't the speed; it's that under 10,000 racing charges colliding at the empty-balance boundary, not one overdrafts. See [`benchmarks/README.md`](benchmarks/README.md) for how to read it and run your own.

## Design notes

A few deliberate choices, and the answers to the sharp questions they invite:

- **A failed (insufficient) charge records nothing — so a retry with the same key re-attempts. That's intended.** Idempotency dedups *successful effects*, not attempts. A charge rejected for insufficient balance didn't happen, so the same key should be free to succeed later — e.g. a metered webhook that hit a zero balance and is retried after a top-up. The dedup keys the *committed ledger row*, which only exists for charges that actually landed.
- **`numeric(18,4)` in the database, `float` at the API boundary.** The stored balance and every ledger delta are exact decimals — no binary-float drift on the money itself. The functions return `float` for ergonomics, which is fine for integer-ish credits; if you were metering *actual currency*, return `Decimal` at the boundary too (a one-line change) so nothing rounds on the way out.
- **Why two attempts is enough** (`for _attempt in range(2)`). Under N concurrent charges sharing one key, exactly one wins the `UNIQUE(tenant_id, idempotency_key)` insert and commits; every loser's `INSERT` raises, rolling back its whole transaction (decrement included). On the second pass the losers see the winner's now-committed ledger row in the "already seen?" read and return *that* — a clean dedup. A third attempt can't be needed: once the winner commits, the row is visible, so attempt two is always either the winner's own success or a loser's dedup. The re-read *after* the loop only exists to distinguish the genuinely-nothing-landed case (e.g. a foreign-key violation) — so a constraint error is never reported as phantom success.

## Context

This is one self-contained piece of **[PANTHEON](https://pantheonlabs.info)** — a multi-tenant substrate for running AI agents safely on other people's money and data (tenancy + RLS isolation, a governed reasoning loop, an approval queue, this metering ledger, and a quality gate), designed and built solo. The live Studio at [pantheonlabs.info](https://pantheonlabs.info) runs on it; the flagship write-up is at [pantheonlabs.co.uk](https://pantheonlabs.co.uk). Built by Isaac Teague Frayling.

## License

Apache-2.0. See [LICENSE](LICENSE).
