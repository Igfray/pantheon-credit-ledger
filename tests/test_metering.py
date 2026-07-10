# Copyright 2026 Isaac Teague Frayling
# Licensed under the Apache License, Version 2.0 (see LICENSE).
"""Atomic credit metering — no overdraft under concurrency, idempotent retries.

Needs a Postgres reachable at $DATABASE_URL with schema.sql applied:
    createdb ledger_test
    psql "$DATABASE_URL" -f schema.sql
    DATABASE_URL=postgresql://localhost/ledger_test pytest -q
"""
from __future__ import annotations

import os
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import create_engine, text

from credit_ledger import metering

DB_URL = os.environ.get("DATABASE_URL")


@pytest.fixture(scope="module")
def engine():
    if not DB_URL:
        pytest.skip("set DATABASE_URL to a Postgres with schema.sql applied")
    eng = create_engine(DB_URL, future=True)
    try:
        with eng.connect() as c:
            c.execute(text("SELECT 1 FROM credit_balance LIMIT 0"))   # schema applied?
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"apply schema.sql first: {exc}")
    return eng


@pytest.fixture()
def tenant(engine):
    tid = uuid.uuid4()
    with engine.begin() as c:
        c.execute(text("INSERT INTO tenants (id, slug, name) VALUES (:id,:s,:n)"),
                  {"id": str(tid), "s": f"t-{tid.hex[:8]}", "n": f"t-{tid.hex[:8]}"})
    yield str(tid)
    with engine.begin() as c:
        c.execute(text("DELETE FROM tenants WHERE id=:id"), {"id": str(tid)})   # cascades


def test_grant_and_spend(engine, tenant):
    assert metering.grant(tenant, 10, engine=engine).balance == 10.0
    r = metering.decrement(tenant, 3, engine=engine)
    assert r.ok and r.balance == 7.0
    assert metering.balance(tenant, engine=engine) == 7.0


def test_insufficient_is_rejected_without_change(engine, tenant):
    metering.grant(tenant, 2, engine=engine)
    r = metering.decrement(tenant, 5, engine=engine)
    assert r.ok is False and r.balance == 2.0              # unchanged
    assert metering.balance(tenant, engine=engine) == 2.0


def test_idempotent_retry_charges_once(engine, tenant):
    metering.grant(tenant, 10, engine=engine)
    a = metering.decrement(tenant, 4, idempotency_key="charge-1", engine=engine)
    b = metering.decrement(tenant, 4, idempotency_key="charge-1", engine=engine)   # retry
    assert a.ok and b.ok and b.deduped is True
    assert metering.balance(tenant, engine=engine) == 6.0  # charged once, not twice


def test_grant_idempotent_retry_credits_once(engine, tenant):
    a = metering.grant(tenant, 100, idempotency_key="stripe:evt:abc", engine=engine)
    b = metering.grant(tenant, 100, idempotency_key="stripe:evt:abc", engine=engine)   # Stripe retry
    assert a.ok and b.ok and b.deduped is True
    assert metering.balance(tenant, engine=engine) == 100.0   # credited once, not 200


# ── ★ atomic under concurrency: no overdraft, no double-spend ─────────────────
def test_atomic_decrement_under_concurrency(engine, tenant):
    metering.grant(tenant, 50, engine=engine)
    # 100 concurrent spends of 1 against a balance of 50 → exactly 50 succeed
    pool_engine = create_engine(DB_URL, future=True, pool_size=20, max_overflow=20)

    def spend(_i):
        return metering.decrement(tenant, 1, reason="race", engine=pool_engine).ok

    with ThreadPoolExecutor(max_workers=20) as ex:
        results = list(ex.map(spend, range(100)))

    pool_engine.dispose()
    assert sum(results) == 50                              # exactly the balance, no more
    assert metering.balance(tenant, engine=engine) == 0.0  # never negative
    with engine.begin() as c:
        spent = c.execute(text("SELECT count(*) FROM credit_ledger "
                               "WHERE tenant_id=:t AND reason='race'"), {"t": tenant}).scalar()
    assert spent == 50                                     # one ledger row per successful spend


# ── ★ grant is retry-safe under concurrency: a replayed Stripe event credits once ─
def test_grant_idempotent_under_concurrency(engine, tenant):
    # 20 concurrent grants of the SAME event_id → exactly one credit, and none raises / 500s
    pool_engine = create_engine(DB_URL, future=True, pool_size=20, max_overflow=20)

    def give(_i):
        return metering.grant(tenant, 100, reason="stripe", idempotency_key="stripe:evt:race",
                              engine=pool_engine)

    with ThreadPoolExecutor(max_workers=20) as ex:
        results = list(ex.map(give, range(20)))

    pool_engine.dispose()
    assert all(r.ok for r in results)                     # every call returned a clean Charge, no 500
    assert metering.balance(tenant, engine=engine) == 100.0   # exactly one grant applied, not 20
    with engine.begin() as c:
        rows = c.execute(text("SELECT count(*) FROM credit_ledger "
                              "WHERE tenant_id=:t AND idempotency_key='stripe:evt:race'"),
                         {"t": tenant}).scalar()
    assert rows == 1                                       # one ledger row, not 20


@pytest.mark.parametrize("bad", [0, -1, -100.0])
def test_grant_rejects_non_positive_amounts(bad):
    # A non-positive grant would DECREMENT (brick) a tenant or 500; the primitive must refuse it loudly,
    # before touching the DB — so the invariant lives at the money primitive, not only in each caller.
    with pytest.raises(ValueError):
        metering.grant("00000000-0000-0000-0000-000000000000", bad, reason="bad", engine="UNUSED")
