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
- **Both test suites must pass.** `.github/workflows/test.yml` runs the Python
  suite and the frontend suite on every PR, and each enforces a coverage floor.
  Run them locally first:

  ```bash
  pytest tests/unit/ --cov=trawlarr --cov-report=term-missing
  pytest tests/integration/

  cd trawlarr/webserver/frontend
  npm run lint && npm run test:coverage && npm run build
  ```

  Both pytest suites run in CI. `tests/integration/` needs no services and no
  network — it is a second suite rather than a slower one, and it is under the
  same "must stay green" rule as the unit tests.

  The frontend `build` is not optional politeness — Quasar's webpack build is
  the only step that resolves every import in every `.vue` file, so it is what
  catches a broken import before the Docker image does.

### The coverage ratchet

Coverage floors here are a **ratchet, not a target**. The rule:

- The floor sits just below the number currently measured on `main`.
- When a PR pushes coverage meaningfully above the floor, **raise the floor in
  the same PR**. Leaving a floor far below actual coverage means the number can
  halve without anyone noticing, which is the whole failure mode we are trying
  to avoid.
- **Never lower a floor to make a red build green.** If a change genuinely makes
  a floor unreachable, say so in the PR and get agreement — a lowered floor is a
  decision, not a fix.

Where the floors live:

| Suite    | Floor                                                     | Value               | Measured on `main`         |
| -------- | --------------------------------------------------------- | ------------------- | -------------------------- |
| Python   | `PYTHON_COVERAGE_FLOOR` in `.github/workflows/test.yml`    | 51 (lines)          | 53.1% lines (969 tests)    |
| Frontend | `test.coverage.thresholds` in `frontend/vitest.config.js`  | 20 (functions)      | 20.5% functions (25 tests) |

The Python job does the nagging for you. When measured coverage runs more than
three points ahead of the floor, the run emits a notice and a job-summary line
naming the value to set `PYTHON_COVERAGE_FLOOR` to. It never fails the build for
this — failing the PR that *raised* coverage would be a strange way to encourage
raising coverage — so it is on the author to act on it.

The suggested value is `floor(measured - 0.5)`. Half a point of slack is
deliberate: a floor set a hundredth of a point under the measurement is a
tripwire that ordinary work sets off, and a tripwire people disarm is worse than
no floor at all. The measurement also drifts a few hundredths of a point with the
interpreter version (41.76% on CI's Python 3.10, 41.72% on a 3.12 checkout), so
quote the CI number when you raise the floor.

For the frontend, read **functions** as the honest number. v8 marks a module's
top-level statements covered merely for having been imported, and the router
spec imports every page in the app — so the statement and line percentages are
flattered by roughly 60 points of "covered but never asserted on".

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

### Pinning a call site

Most of this project's escaped bugs share one shape:

> The pieces are tested. The wiring between them is not.

A mechanism gets built and tested thoroughly — and then the single line that
invokes it in production is covered by nothing. Delete that line and the whole
suite stays green while the mechanism is, in effect, gone. It has happened to
the worker stall detector, the done-state write, the convergence check, both
start-up guards, and the Foreman's republish after a supervised restart. Every
one was found by mutation testing; none was found in review.

The failures are silent by construction. Hung workers are never killed, files
are reprocessed for ever, an unmigrated install comes up looking empty. Nothing
raises, so nothing notices.

**So: when you add a mechanism that something else must invoke, add a test that
fails if the invocation is removed.**

**Prefer a behavioural test.** If the machinery can be driven for one iteration
with real objects, do that — it proves the mechanism runs *and* that its effect
lands. `tests/unit/test_safety_mechanism_call_sites.py` has several:
`PostProcessor.run()` is driven over exactly one task, with a bounded fake
`event` so a broken loop fails rather than hangs, and the assertion is that the
delivered file afterwards answers "already completed" to the check that keeps
it out of the next scan.

**Fall back to a source-level pin** when standing the machinery up would cost
far more than it proves — `main()`, which parses `argv` and starts the whole
application, is the canonical example. Read the function's AST for the call,
never its source text: a call that has been commented out, or that survives
only inside a docstring, is not a call, and a substring check cannot tell the
difference.

```python
def _calls_made_by(func):
    """Names of the functions called directly in `func`'s body."""
    tree = ast.parse(textwrap.dedent(inspect.getsource(func)))
    names = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name):
                names.append(node.func.id)
            elif isinstance(node.func, ast.Attribute):
                names.append(node.func.attr)
    return names


def test_main_refuses_to_start_against_an_unmigrated_install():
    from trawlarr import service

    assert 'guard_against_legacy_config_directory' in _calls_made_by(service.main), (
        "main() no longer calls guard_against_legacy_config_directory(). An "
        "upgrade with a populated ~/.unmanic and no ~/.trawlarr will start "
        "cleanly and present itself as a fresh install, with every library, "
        "plugin and setting apparently gone."
    )
```

Where ordering is part of the mechanism, pin the ordering too — the guards have
to run *before* `Config()` creates the configuration directory, or they can no
longer tell an unmigrated install from a fresh one:

```python
calls = _calls_made_by(service.main)
assert calls.index('guard_against_legacy_config_directory') < calls.index('Config')
```

Three rules for either kind:

- **The failure message names the consequence, not the assertion.** "The stall
  detector is not called" is nearly useless at 3am; "hung workers are never
  killed" is what the reader needs.
- **Bound every loop from the outside.** A test that drives a `while True` loop
  must make it terminate through something other than the line under test, or
  deleting that line turns a failure into a hang.
- **A source-level pin is a stopgap, not a destination.** Say in the docstring
  why the behavioural version was not written, so the next person can upgrade it
  rather than assume it was impossible.

The end-to-end test in `tests/integration/test_pipeline_end_to_end.py` is the
other half of this. It starts the service against a temp `$HOME` and a temp
library and asserts a file is discovered, queued, processed and recorded as
done.

Its file is where the per-library allow-list regression and the
plugins-directory regression would have been caught — both lived entirely
between components, which is why every unit test passed while the features
were disconnected. Note the two are caught by different tests in that file,
and only the allow-list one by the pipeline path itself; the
plugins-directory one is a call-site pin, because the running pipeline
resolves plugin modules from the path stored in the database and so never
crosses that seam. If your change touches the pipeline, run it.

### Checking that a test actually catches something

New tests are cheap to write and easy to write green. Before claiming a
mechanism is covered, break it and watch the test fail:

```bash
python3 devops/mutation_check.py \
    --file trawlarr/libs/postprocessor.py \
    --old 'self.record_completed_file()' \
    --new 'pass' \
    --tests tests/unit/test_safety_mechanism_call_sites.py
```

The script copies your working tree to a temp directory, applies the
substitution there, runs the tests you name, and reports `KILLED` (the suite
noticed — good) or `SURVIVED` (**a finding**: nothing tests that the mutated
line does anything). Your working tree is never modified and `HOME` is
redirected inside the copy.

Run with no arguments to re-check the catalogue of mutations that have already
escaped once. It is deliberately not a CI job — a full mutation run is minutes
of CPU for a signal nobody reads on a Tuesday, and a slow job on the PR path
gets skipped and then deleted. It is a tool you point at the thing you are
actually worried about.

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
