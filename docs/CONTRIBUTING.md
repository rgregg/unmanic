# Contributing to Trawlarr

Trawlarr is a fork of [Unmanic](https://github.com/Unmanic/unmanic). It is a
small project with a small maintainer team, so treat the following as
guidelines rather than rules — use your best judgement.

Everyone participating here is expected to follow the
[Code of Conduct](CODE_OF_CONDUCT.md).

## Reporting bugs

Open an issue using the [bug report template](https://github.com/rgregg/trawlarr/issues/new/choose)
and include as much detail as you can: what you expected, what happened, the
version from the web UI footer, and how you installed it.

Before filing, check whether the same behaviour exists in upstream Unmanic. If
it does, upstream is usually the better place to report it — this fork carries
a specific set of changes (see [`FORK.md`](../FORK.md)) and everything else is
upstream's code.

If you find a **closed** issue that looks like what you are hitting, open a new
one and link to the original rather than commenting on the closed thread.

## Suggesting features

Feature ideas are welcome via the feature request template. Include the problem
you are trying to solve, not only the solution you have in mind.

Note that a good number of ideas are better served as **plugins** than as core
changes. Trawlarr uses the upstream Unmanic plugin architecture and pulls
plugin catalogs directly from GitHub, so a plugin is often the faster path and
does not require a fork release to ship.

## Reporting security issues

Do not open a public issue for a security vulnerability. Use
[private vulnerability reporting](https://github.com/rgregg/trawlarr/security/advisories/new)
on the repository.

## Developing

See the [Development Environment Guide](DEVELOPING.md) for setting up a local
environment, building the frontend, and running the test suite.

## Opening pull requests

Code contributions are very welcome.

- **PRs target `main`.** This is a single-branch repo — there is no `staging`
  or `master` branch.
- **The test suite must pass.** `.github/workflows/test.yml` runs
  `pytest tests/unit/` on every PR and enforces a coverage floor. Coverage must
  not regress below it. Run the suite locally first:

  ```bash
  pytest tests/unit/ --cov=trawlarr --cov-report=term-missing
  ```

- **Write new code against the `trawlarr` package.** The application lives
  in `trawlarr/`; the `unmanic/` directory is a compatibility shim that
  keeps `unmanic.*` imports resolving for community plugins. It is
  supported, not deprecated — but it is for plugins, not for our own
  imports. Package layout, runtime paths and the environment variables are
  summarised in [DEVELOPING.md](DEVELOPING.md#package-layout-and-naming).

- **New behaviour should come with a test.** The tests under `tests/unit/` each
  pin an invariant this fork depends on; that is what stops a careless edit
  silently reintroducing upstream behaviour.
- **Consider sending the patch upstream too.** If your change would benefit
  Unmanic generally, upstream and this fork both come out ahead if it lands in
  both places.

### Copyright and licensing of contributions

**You keep the copyright in what you write.** There is no CLA and no copyright
assignment. By opening a pull request you are licensing your contribution
inbound under the same license the project is distributed under —
**GPL-3.0-or-later**, see [`LICENSE`](../LICENSE) — which is the same thing you
would be agreeing to by contributing to any GPL project.

That is a deliberate departure from upstream's contributing policy, which asks
for copyright to be assigned to the Unmanic project owner. Contributions here
are not assigned to anyone.

### License headers

Every Python file in the repo carries a header. Which one depends on where the
file came from.

**New files you write** get a short SPDX header:

```python
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Your Name
#
# This file is part of Trawlarr, a fork of Unmanic.
# See LICENSE for the full license text.
```

The full GPL boilerplate lives in [`LICENSE`](../LICENSE); the SPDX identifier
is the machine-readable form that tooling expects. Put your own name in the
copyright line — see above, you keep it.

**Files inherited from upstream** keep the header they already have. Do not
relicense, reformat, or strip upstream's copyright block. GPL-3.0 §5(a)
requires that modified files carry a prominent notice saying they were changed
and when, so if you are the first to modify an upstream file, add a line
beneath its `Date:` line:

```
    Modified 2026 by Your Name as part of Trawlarr.
```

One such line per file is enough — it does not need updating on every
subsequent edit.

CI enforces that every Python file has one header or the other. To check
before pushing:

```bash
devops/check_license_headers.sh
```

Non-Python files (workflows, shell scripts, Dockerfiles) are not checked, but
an SPDX comment on new fork-authored ones is appreciated.
