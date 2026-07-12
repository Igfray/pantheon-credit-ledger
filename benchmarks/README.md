# Benchmark

One question: under a **stampede of concurrent charges against a single tenant's
balance** — the hot-row, worst-case-contention path, where every charge serialises on
the same row lock — how fast is the guarded atomic `UPDATE`, and does the invariant
*still* hold exactly?

`bench.py` fires `balance + extra` charges across a thread pool, times each one, then
re-checks correctness: successful charges must equal the starting balance, ledger rows
must equal successful charges, and the final balance must be exactly `0` (never
negative). Speed with a violated invariant is a failure, and it prints as one.

## Run it yourself

```bash
createdb ledger_bench
psql "$DATABASE_URL" -f schema.sql
DATABASE_URL=postgresql+psycopg://localhost/ledger_bench python benchmarks/bench.py \
  --balance 5000 --extra 5000 --workers 32
```

## A real run

Postgres 16.14 in Docker, **stock config** (fsync on, `synchronous_commit` on — i.e.
every charge is a durable commit), 8-core host, 32 workers, 10,000 charges against a
balance of 5,000:

```
  throughput            1,100 charges/sec   (10,000 in ~9s)
  latency p50           ~22 ms
  latency p95           ~77 ms
  latency p99          ~138 ms

  succeeded             5,000   (expected 5,000)
  rejected              5,000   (insufficient funds — expected 5,000)
  ledger rows           5,000   (one per real debit)
  final balance           0.0   (never negative)

  INVARIANT HELD ✓
```

## Reading the number

This is the **pessimal** case on purpose: every one of the 10,000 charges targets the
*same* balance row, so Postgres serialises them all on that row's lock — there is no
parallelism to exploit, by design, and throughput is bounded by
`1 / (single durable charge round-trip)`. Each charge is a full transaction (bind the
RLS tenant GUC → guarded `UPDATE` → append a ledger row → durable commit).

Two things follow:

- **It holds under contention.** 10,000 charges racing one balance, half of them
  colliding at the empty-balance boundary, and not one overdraft or lost update. That's
  the property worth having, and the benchmark's real job is to prove it stays true at
  load, not to post a big number.
- **Real workloads are not one row.** Charges spread across many tenants hit *different*
  balance rows, so they don't contend and throughput scales with Postgres, not with this
  single-row ceiling. Turning off per-charge durability (`synchronous_commit=off`, or
  batching) trades the durability guarantee for more throughput; the default here keeps
  it. Numbers vary with hardware and config — the invariant does not.
