# Copyright 2026 Isaac Teague Frayling
# Licensed under the Apache License, Version 2.0 (see LICENSE).
"""Reproducible load benchmark for the credit ledger.

It answers one question: under a stampede of concurrent charges against a *single*
tenant's balance (the hot-row, worst-case-contention path), how fast is the guarded
atomic UPDATE, and does the invariant still hold exactly?

It measures throughput and latency, and re-checks correctness at the end:
  * successful charges == starting balance   (no overdraft, no lost update)
  * ledger rows        == successful charges (exactly one row per real debit)
  * final balance      == 0                  (never negative)

Run it yourself (needs a Postgres with schema.sql applied):

    createdb ledger_bench
    psql "$DATABASE_URL" -f schema.sql
    DATABASE_URL=postgresql+psycopg://localhost/ledger_bench python benchmarks/bench.py

Tune with flags: --balance 5000 --extra 5000 --workers 32
The default fires (balance + extra) charges so roughly half are rejected — exercising
both the success and the insufficient-funds paths under contention.
"""
from __future__ import annotations

import argparse
import os
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

from sqlalchemy import create_engine, text

from credit_ledger import metering


def _percentile(sorted_ms: list[float], pct: float) -> float:
    if not sorted_ms:
        return 0.0
    k = max(0, min(len(sorted_ms) - 1, round((pct / 100) * len(sorted_ms) + 0.5) - 1))
    return sorted_ms[k]


def main() -> int:
    ap = argparse.ArgumentParser(description="credit-ledger load benchmark")
    ap.add_argument("--balance", type=int, default=5000, help="starting credits (also = charges that should succeed)")
    ap.add_argument("--extra", type=int, default=5000, help="extra charges beyond the balance (should be rejected)")
    ap.add_argument("--workers", type=int, default=32, help="concurrent DB connections / threads")
    args = ap.parse_args()

    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        print("set DATABASE_URL to a Postgres with schema.sql applied")
        return 2

    total = args.balance + args.extra
    pool = create_engine(db_url, future=True, pool_size=args.workers, max_overflow=args.workers)
    tenant = str(uuid.uuid4())
    with pool.begin() as c:
        c.execute(text("INSERT INTO tenants (id, slug, name) VALUES (:id,:s,:s)"),
                  {"id": tenant, "s": f"bench-{tenant[:8]}"})
    metering.grant(tenant, args.balance, reason="bench-seed", engine=pool)

    lat_ms: list[float] = [0.0] * total

    def charge(i: int) -> bool:
        t0 = time.perf_counter()
        ok = metering.decrement(tenant, 1, reason="bench", engine=pool).ok
        lat_ms[i] = (time.perf_counter() - t0) * 1000.0
        return ok

    print(f"firing {total:,} concurrent charges ({args.workers} workers) against a balance of {args.balance:,} …")
    wall0 = time.perf_counter()
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        oks = sum(ex.map(charge, range(total)))
    wall = time.perf_counter() - wall0

    # correctness re-check — the whole point: speed means nothing if the invariant slipped
    final = metering.balance(tenant, engine=pool)
    with pool.begin() as c:
        rows = c.execute(text("SELECT count(*) FROM credit_ledger WHERE tenant_id=:t AND reason='bench'"),
                         {"t": tenant}).scalar()
        c.execute(text("DELETE FROM tenants WHERE id=:t"), {"t": tenant})   # cascades ledger + balance
    pool.dispose()

    lat_sorted = sorted(lat_ms)
    ok_invariant = (oks == args.balance and rows == args.balance and final == 0.0)

    print()
    print(f"  throughput      {total / wall:>10,.0f} charges/sec   ({total:,} in {wall:.2f}s)")
    print(f"  latency p50     {_percentile(lat_sorted, 50):>10.2f} ms")
    print(f"  latency p95     {_percentile(lat_sorted, 95):>10.2f} ms")
    print(f"  latency p99     {_percentile(lat_sorted, 99):>10.2f} ms")
    print(f"  latency max     {lat_sorted[-1]:>10.2f} ms")
    print()
    print(f"  succeeded       {oks:>10,}   (expected {args.balance:,})")
    print(f"  rejected        {total - oks:>10,}   (insufficient funds — expected {args.extra:,})")
    print(f"  ledger rows     {rows:>10,}   (one per real debit)")
    print(f"  final balance   {final:>10.1f}   (never negative)")
    print()
    print("  INVARIANT HELD ✓" if ok_invariant else "  INVARIANT VIOLATED ✗")
    return 0 if ok_invariant else 1


if __name__ == "__main__":
    raise SystemExit(main())
