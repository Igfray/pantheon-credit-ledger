# Copyright 2026 Isaac Teague Frayling
# Licensed under the Apache License, Version 2.0 (see LICENSE).
"""Atomic, overdraft-proof, idempotent credit metering.

The whole charge is one guarded UPDATE:

    UPDATE credit_balance SET credits = credits - n WHERE tenant_id = :t AND credits >= n

The row lock serialises concurrent charges; the ``credits >= n`` guard makes overdraft
impossible — a charge either fully succeeds or doesn't happen. Every charge is written
to an append-only ledger; ``UNIQUE(tenant_id, idempotency_key)`` makes a retried charge a
no-op (and, on a concurrent dup, the loser's transaction rolls back its decrement too).

Runs on a PRIVILEGED database role (the money path is infrastructure) and scopes by
``tenant_id`` explicitly; row-level security on the tables is defence-in-depth — hence the
per-transaction bind of the ``app.current_tenant`` GUC so those privileged writes still
satisfy the tenant-isolation policy on any Postgres where the role is not BYPASSRLS.

Extracted from PANTHEON (a multi-tenant AI substrate). See README.md for the design notes.
"""
from __future__ import annotations

import logging
import os
import uuid
from dataclasses import dataclass

from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError

log = logging.getLogger("credit_ledger.metering")

# The Postgres GUC (session/txn variable) that carries the current tenant for row-level security.
# Set per-transaction by _bind so writes on a non-BYPASSRLS role satisfy the tenant_isolation policy.
# Override via the env var if your schema uses a different GUC name.
TENANT_GUC = os.environ.get("CREDIT_LEDGER_TENANT_GUC", "app.current_tenant")

_ENGINE = None
_DATABASE_URL = os.environ.get("DATABASE_URL")


def configure(database_url: str) -> None:
    """Set the default database URL (alternative to the ``DATABASE_URL`` env var). Optional: every
    public function also accepts an explicit ``engine=`` for full control over pooling / lifecycle."""
    global _DATABASE_URL, _ENGINE
    _DATABASE_URL = database_url
    _ENGINE = None


def _engine():
    global _ENGINE
    if _ENGINE is None:
        if not _DATABASE_URL:
            raise RuntimeError(
                "No database configured — set DATABASE_URL, call configure(url), or pass engine=.")
        _ENGINE = create_engine(_DATABASE_URL, future=True)
    return _ENGINE


def _bind(c, tenant_id: str) -> None:
    """Bind the RLS tenant GUC for this transaction so a privileged engine's writes satisfy the
    tenant_isolation policy on any Postgres where the role is NOT BYPASSRLS (every managed offering,
    and a hardened self-host). Harmless on a superuser dev box. Without it, the decrement matches
    zero rows and the INSERT's WITH CHECK fails the moment RLS is actually enforced — billing
    silently bricks in prod."""
    c.execute(text("SELECT set_config(:g, :t, true)"), {"g": TENANT_GUC, "t": str(tenant_id)})


@dataclass
class Charge:
    ok: bool
    balance: float
    deduped: bool = False


def balance(tenant_id: str, *, engine=None) -> float:
    eng = engine or _engine()
    with eng.begin() as c:
        _bind(c, tenant_id)
        v = c.execute(text("SELECT credits FROM credit_balance WHERE tenant_id = :t"),
                      {"t": str(tenant_id)}).scalar()
    return float(v) if v is not None else 0.0


def grant(tenant_id: str, amount: float, *, reason: str = "grant",
          idempotency_key: str | None = None, engine=None) -> Charge:
    """Add credits. Idempotent on ``idempotency_key`` (e.g. a Stripe ``event_id``): a retried or
    concurrently-replayed grant is a no-op, not a double-credit or a 500. The ledger's
    ``UNIQUE(tenant_id, idempotency_key)`` is the real guarantee; the retry loop turns a racing
    duplicate (which would otherwise surface an IntegrityError) into a clean dedup."""
    if not amount > 0:                       # invariant at the primitive: a non-positive grant would
        raise ValueError(f"grant amount must be positive, got {amount!r}")  # decrement/brick a tenant.
    eng = engine or _engine()
    for _attempt in range(2):
        try:
            with eng.begin() as c:
                _bind(c, tenant_id)
                if idempotency_key:
                    prior = c.execute(text("SELECT balance_after FROM credit_ledger "
                                           "WHERE tenant_id=:t AND idempotency_key=:k"),
                                      {"t": str(tenant_id), "k": idempotency_key}).scalar()
                    if prior is not None:
                        return Charge(ok=True, balance=float(prior), deduped=True)
                bal = c.execute(text(
                    "INSERT INTO credit_balance (tenant_id, credits) VALUES (:t, :a) "
                    "ON CONFLICT (tenant_id) DO UPDATE SET credits = credit_balance.credits + :a, "
                    "updated_at = now() RETURNING credits"),
                    {"t": str(tenant_id), "a": amount}).scalar()
                c.execute(text("INSERT INTO credit_ledger (id, tenant_id, delta, reason, idempotency_key, balance_after) "
                               "VALUES (:id,:t,:d,:r,:k,:b)"),
                          {"id": str(uuid.uuid4()), "t": str(tenant_id), "d": amount,
                           "r": reason, "k": idempotency_key, "b": bal})
                return Charge(ok=True, balance=float(bal))
        except IntegrityError:                        # a constraint blocked it — could be a concurrent
            continue                                  # same-key dedup OR an FK (no such tenant); decide below
    # Both attempts hit a constraint. If a ledger row exists for this key, it was a genuine concurrent
    # dedup (the winner committed). If NOT, nothing landed (e.g. an FK violation: the tenant doesn't
    # exist) — fail LOUD rather than report a phantom success that silently loses money.
    prior = None
    if idempotency_key:
        with eng.begin() as c:
            _bind(c, tenant_id)
            prior = c.execute(text("SELECT balance_after FROM credit_ledger "
                                   "WHERE tenant_id=:t AND idempotency_key=:k"),
                              {"t": str(tenant_id), "k": idempotency_key}).scalar()
    if prior is not None:
        return Charge(ok=True, balance=float(prior), deduped=True)
    log.error("grant did NOT land for tenant %s (key=%r) — no ledger row after retries (no such tenant?)",
              tenant_id, idempotency_key)
    return Charge(ok=False, balance=balance(tenant_id, engine=eng))


def decrement(tenant_id: str, amount: float = 1.0, *, reason: str = "usage",
              idempotency_key: str | None = None, engine=None) -> Charge:
    """Atomically spend `amount` credits. Returns ok=False (no change) if insufficient."""
    eng = engine or _engine()
    for _attempt in range(2):
        try:
            with eng.begin() as c:
                _bind(c, tenant_id)
                if idempotency_key:
                    prior = c.execute(text("SELECT balance_after FROM credit_ledger "
                                           "WHERE tenant_id=:t AND idempotency_key=:k"),
                                      {"t": str(tenant_id), "k": idempotency_key}).scalar()
                    if prior is not None:
                        return Charge(ok=True, balance=float(prior), deduped=True)
                bal = c.execute(text(
                    "UPDATE credit_balance SET credits = credits - :n, updated_at = now() "
                    "WHERE tenant_id = :t AND credits >= :n RETURNING credits"),
                    {"t": str(tenant_id), "n": amount}).scalar()
                if bal is None:                       # insufficient — no change
                    cur = c.execute(text("SELECT credits FROM credit_balance WHERE tenant_id=:t"),
                                    {"t": str(tenant_id)}).scalar()
                    return Charge(ok=False, balance=float(cur) if cur is not None else 0.0)
                c.execute(text("INSERT INTO credit_ledger (id, tenant_id, delta, reason, idempotency_key, balance_after) "
                               "VALUES (:id,:t,:d,:r,:k,:b)"),
                          {"id": str(uuid.uuid4()), "t": str(tenant_id), "d": -amount,
                           "r": reason, "k": idempotency_key, "b": bal})
                return Charge(ok=True, balance=float(bal))
        except IntegrityError:                        # concurrent same idempotency_key
            continue                                  # txn rolled back (incl. decrement); retry → dedup
    with eng.begin() as c:
        _bind(c, tenant_id)
        prior = c.execute(text("SELECT balance_after FROM credit_ledger "
                               "WHERE tenant_id=:t AND idempotency_key=:k"),
                          {"t": str(tenant_id), "k": idempotency_key}).scalar()
    if prior is None:                                    # IntegrityError but NO prior row → not a real dedup;
        return Charge(ok=False, balance=balance(tenant_id, engine=eng))   # don't report a phantom success
    return Charge(ok=True, balance=float(prior), deduped=True)
