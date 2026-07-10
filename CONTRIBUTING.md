# Contributing

Thanks for the interest. This project is deliberately kept in a state where it *can* be offered
under commercial terms later, so the contribution terms below matter a bit more than the size of the
project might suggest — please read them before sending code.

## Before a substantial PR — open an issue first

This is a solo project with a specific licensing posture. **Small, obvious fixes** (typos, clear bugs)
are welcome as direct PRs. For anything **substantial**, please open an issue to discuss first — it
saves you writing code that might not fit the direction, and keeps the licensing clean (below).

## Sign your commits (DCO)

Contributions are accepted under the **Developer Certificate of Origin (DCO)** — by signing off, you
certify you wrote the change (or otherwise have the right to submit it). Add a sign-off to each commit:

```bash
git commit -s -m "your message"
```

That appends a `Signed-off-by: Your Name <you@example.com>` trailer (make sure your git `user.name`
and `user.email` are set). The full DCO 1.1 text is at the bottom of this file.

## Contribution licensing

The DCO on its own only certifies *provenance* — it does **not** grant the maintainer any right to
relicense. So, in addition to the DCO, by submitting a contribution you agree that:

1. Your contribution is licensed to the project and everyone downstream under the project's
   open-source license, **Apache-2.0**; **and**
2. You grant the maintainer a perpetual, worldwide, non-exclusive, royalty-free, irrevocable license
   to also distribute your contribution, as part of the project, under **separate commercial terms**
   — i.e. the project may be offered under a dual (open-source + commercial) license in future.

In plain terms: **you keep your copyright**, your contribution stays Apache-2.0 for everyone, and the
maintainer can *also* include it if a commercially-licensed edition of the project ever exists. This
keeps that option open without asking you to assign anything away.

> This is the maintainer's stated contribution terms, not legal advice. If external contribution ever
> picks up, a formal CLA (e.g. via CLA Assistant) may replace this section.

## Running the tests

The suite needs a Postgres with `schema.sql` applied — see the README. In short:

```bash
createdb ledger && psql "$DATABASE_URL" -f schema.sql
DATABASE_URL=postgresql://localhost/ledger pytest -q
```

Keep the tests green; they're the spec. New behaviour should come with a test that would fail without it.

---

## Developer Certificate of Origin 1.1

```
Developer Certificate of Origin
Version 1.1

Copyright (C) 2004, 2006 The Linux Foundation and its contributors.

Everyone is permitted to copy and distribute verbatim copies of this
license document, but changing it is not allowed.


Developer's Certificate of Origin 1.1

By making a contribution to this project, I certify that:

(a) The contribution was created in whole or in part by me and I
    have the right to submit it under the open source license
    indicated in the file; or

(b) The contribution is based upon previous work that, to the best
    of my knowledge, is covered under an appropriate open source
    license and I have the right under that license to submit that
    work with modifications, whether created in whole or in part
    by me, under the same open source license (unless I am
    permitted to submit under a different license), as indicated
    in the file; or

(c) The contribution was provided directly to me by some other
    person who certified (a), (b) or (c) and I have not modified
    it.

(d) I understand and agree that this project and the contribution
    are public and that a record of the contribution (including all
    personal information I submit with it, including my sign-off) is
    maintained indefinitely and may be redistributed consistent with
    this project or the open source license(s) involved.
```
