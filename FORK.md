# Fork notes

This is a fork of [Unmanic/unmanic](https://github.com/Unmanic/unmanic). The
upstream maintainer is solo and currently closes outside PRs targeting
`master`; PRs targeting `staging` are sometimes merged. We carry a small set
of patches that aren't shipping upstream (yet, or ever) and build our own
Docker image from them.

## Goals

This fork aims to be a **fully self-hosted, no-phone-home, no-license-tier**
build of Unmanic that exercises the rights granted by upstream's GPLv3
license:

- **No `api.unmanic.app` dependency.** Plugin discovery, plugin downloads,
  registration, token refresh, and "linked installation" sync are all
  removed or stubbed. The app runs end-to-end against the local network
  and the public GitHub-hosted plugin catalogs.
- **No telemetry.** No registration heartbeat, no plugin-install reporting,
  no "user info" lookups, no remote installation address sync.
- **No supporter level gates.** Library count limits, linked-installation
  count limits, and per-plugin `req_lev` setting restrictions are removed.
  Every feature in the codebase is available to every installation, which
  matches the freedoms granted by GPLv3.

The fork does not fork the public plugin catalog at
`Unmanic/unmanic-plugins`; that repo is GPLv3 and openly published, and
plugin zips are pulled directly from it via raw.githubusercontent.com. If
upstream ever takes the catalog private, point
`UNMANIC_DEFAULT_PLUGIN_REPO_URL` at a mirror.

### Status

| Goal | State |
|---|---|
| Plugin catalog fetched directly from public GitHub (no proxy) | Done — `feat/direct-plugin-repo-fetch` |
| Plugin zip downloads go straight to GitHub when using direct catalogs | Done (consequence of the above — `repo_data_directory` is preserved) |
| Registration / heartbeat / `verify_token` / `fetch_user_data` calls stubbed | Done — "Stub api.unmanic.app dependencies for self-hosted operation" |
| 60-minute scheduler heartbeat removed | Done (same commit) |
| Plugin install telemetry call removed | Done (same commit) |
| Community-forks endpoint short-circuited | Done (same commit) |
| Supporter-level feature gates removed | Done — "Remove supporter-level feature gates" |

## Branch layout

| Branch | Purpose |
|---|---|
| `master` | Mirror of `upstream/master`. Never commit here. |
| `staging` | Mirror of `upstream/staging`. Never commit here. |
| `local` | **The working branch.** All fork changes commit here directly. Docker image is built from this branch. Force-pushed after each rebase onto `upstream/staging`. |
| `feat/*` (optional) | Short-lived feature branches off `local` for in-progress work too large to land in one commit. Merge back into `local` and delete. |

We don't carry topic branches off `staging` for upstream PRs anymore.
The maintainer's track record is closing outside PRs; the existing PRs
(#617/#618/#619) are kept open as a record but treated as unlikely to
land. If a single patch ever turns out to be worth submitting, extract
it: `git cherry-pick <sha>` onto a fresh branch off `staging` and PR
from there. One command, no ongoing overhead.

## Carried patches on `local`

In application order on top of `upstream/staging`. Each is a single
commit on `local` with the topic branch (where one existed) listed for
historical reference; future patches just commit directly.

| # | Subject | Notes |
|---|---|---|
| 1 | Validate plugin zip members before extraction (zip-slip) | Security. Was `fix/zip-slip-plugin-install` (PR #617, ignored upstream). |
| 2 | Stop running plugin string exec_command with `shell=True` | Security. Was `fix/no-shell-true-exec-command` (PR #618, ignored upstream). |
| 3 | Use Windows-safe `shlex.split` for deprecated string exec_command | Companion to #2. |
| 4 | Defer source removal in `post_process_remote_file` until delivery succeeds | Correctness. Was `fix/postprocessor-remote-data-loss` (PR #619, ignored upstream). |
| 5 | Fetch plugin repo catalogs directly from public URLs | Was `feat/direct-plugin-repo-fetch`. Bypasses `api.unmanic.app` for plugin catalog fetches. Comes with 15 unit tests. |
| 6 | Stub `api.unmanic.app` dependencies for self-hosted operation | Turns `register_unmanic` / `verify_token` / `fetch_user_data` / `auth_*` / device-flow / `notify_site_of_plugin_install` / community-forks endpoint / scheduler heartbeat / log-forwarding endpoint lookup into no-ops. Pins session level to `LOCAL_SESSION_LEVEL` (default 7, override via `UNMANIC_LOCAL_SESSION_LEVEL`). |
| 7 | Remove supporter-level feature gates | Strips library count cap, linked-installation count cap, and per-setting `req_lev` enforcement (both save-time and form-render-time). |
| 8 | Bump BtbN FFmpeg release to current autobuild | Build fix. Upstream had pinned a release that BtbN had since pruned. Bumped 8.0 → 8.1. |
| 9 | Build pipeline for `ghcr.io/rgregg/unmanic:local` | `.github/workflows/build_local.yml`. |
| 10 | Test pipeline + coverage for `local` | `.github/workflows/test_local.yml`, `pytest.ini` rewrite, `pytest-cov` dep. |

## Audit notes

### `/library` mount scope (2026-05-06)

Investigated whether the `/library` bind could be tightened from RW to
something narrower for blast-radius reduction. **Conclusion: no.** The
configured operating model writes back to source paths in place:

- `unmanic/libs/postprocessor.py:411` and `:275`: `os.remove(source_data.get('abspath'))`
- `unmanic/libs/postprocessor.py:475/487`: `shutil.move` / `shutil.copyfile` to the destination, which is under `/library`
- `unmanic/libs/workers.py:879`: `os.remove(file_in)` for runner-pass cleanup

So full RW on `/library → /mnt/movie-archive` is required. A different
operating model (write to a separate output dir, manual deletion) could
narrow it but isn't worth the workflow change.

## Possible follow-ups

Tracked in [the fork's issue tracker](https://github.com/rgregg/unmanic/issues):

- **[#1 mDNS-based node discovery](https://github.com/rgregg/unmanic/issues/1)** — replace the unmanic.app `installation_data/list` mechanism (already stubbed) with `_unmanic._tcp.local` mDNS service advertisement so workers discover each other on the LAN. Fork-only feature.
- **[#2 Remove footer bar](https://github.com/rgregg/unmanic/issues/2)** — get rid of the persistent copyright/version footer on every page.
- **[#3 Remove sign-in / sign-out UI](https://github.com/rgregg/unmanic/issues/3)** — the backend auth flow is fully stubbed; the frontend buttons lead to dead unmanic.app links.
- **[#4 Remove Unmanic Central link / page](https://github.com/rgregg/unmanic/issues/4)** — central-API features are stubbed; the nav entry leads to a blank/dead page.
- **[#5 Multi-stage Dockerfile](https://github.com/rgregg/unmanic/issues/5)** — split the build-time toolchain (build-essential, *-dev packages, node) from runtime to shrink image size and speed cold builds. Not landed yet because identifying every runtime soname needed by jellyfin-ffmpeg / BtbN takes iteration.

(#2/#3/#4 are all frontend strips; once we accumulate enough we should fork the `Unmanic/unmanic-frontend` submodule rather than maintain CSS hacks.)

Untracked but noted:

- **`fix/scheduler-completed-tasks-dict-bug`** — `manage_completed_tasks` at `scheduler.py:198` does `historic_task.id` on what is actually a dict, so `'dict' object has no attribute 'id'` fires at startup. Visible in container logs. Upstreamable; could become `fix/*` against `staging`.

## Maintenance workflow

### Day-to-day

Make changes directly on `local`. Push. CI runs:

- `.github/workflows/test_local.yml` — `pytest tests/unit/` + coverage
- `.github/workflows/build_local.yml` — Docker image to `ghcr.io/rgregg/unmanic:local`

If a change is large enough that you want bisect-friendly history, use a
short-lived `feat/*` branch off `local`, then merge back (fast-forward
or squash, whichever fits) and delete the branch.

### When upstream advances

```bash
git fetch upstream
git checkout master  && git rebase upstream/master  && git push
git checkout staging && git rebase upstream/staging && git push

# Rebase local on top of the new staging. Resolve any conflicts as they
# come up — the carried patches table lists what each commit does, which
# helps when picking the right side of a conflict.
git checkout local
git rebase upstream/staging
git push --force-with-lease
```

The `superpowers:sync-upstream` skill automates this pattern.

### When the Docker image needs to be rebuilt

The image is built from `local` automatically on every push (see
`build_local.yml`). To trigger a rebuild without pushing:
`gh workflow run "Build local fork image" --ref local`. See
`home-docs/home-lab/apps/unmanic.md` for the deployment pipeline.

## Tests and coverage

Unit tests live under `tests/unit/` and run on every push to `local`
and every PR targeting `local` via `.github/workflows/test_local.yml`.
The job uploads three artifacts: `coverage-html` (browseable), `coverage-xml`
(machine-readable), and `pytest-results` (JUnit XML).

To run locally:

```bash
python -m venv .venv
.venv/bin/pip install -r requirements.txt -r requirements-dev.txt
.venv/bin/pytest tests/unit/ -v --cov=unmanic --cov-report=term-missing
```

Current baseline: 36 tests, ~10% line coverage of the `unmanic` package
(import-time + targeted module coverage at 15-30%). The goal is **add
tests with each new patch on `local`** so coverage trends up rather than
sweeping a separate "improve coverage" project.

## Why a separate `local` branch instead of just committing on `master`?

So we can:
- Track upstream cleanly on `master`/`staging` (no merge weirdness)
- See exactly what we've added on top with `git log upstream/staging..local`
- Rebase onto upstream advances rather than merge them
- Bisect through carried patches when something breaks

## Build target

Docker image is built from `local`. The official `josh5/unmanic:latest` is
**not** what runs in production on media-server — see deployment notes in
`home-docs/home-lab/apps/unmanic.md`.

### Build pipeline

`.github/workflows/build_local.yml` builds and pushes on every push to
`local` (and on `workflow_dispatch`). Output:

| Tag | Mutability | When to use |
|---|---|---|
| `ghcr.io/rgregg/unmanic:local` | rolling — moves with each push | Komodo deployment for "always latest local" |
| `ghcr.io/rgregg/unmanic:local-<sha7>` | immutable | Pinning a specific build in Komodo if you want to control rollouts |
| `ghcr.io/rgregg/unmanic:local-<py-version>` | follows the python `setup.py` version | Useful when bumping versions intentionally |

The workflow is intentionally **separate** from upstream's
`integration_test_and_build_all_packages_ci.yml`. That workflow:
- Builds on `master` / `staging` / `dev-*` / tags only (does not recognize `local`)
- Hard-codes `docker.io/josh5/unmanic` and refuses to push for other owners
- Runs the integration test suite first (heavy; needs test videos)

Our workflow builds the wheel directly (skipping integration tests since
this fork's CI step for them is disabled upstream anyway, and our changes
are syntactically validated locally), then builds and pushes a single
amd64 image to GHCR using the existing `docker/Dockerfile` unchanged.

Add `linux/arm64` to the `platforms:` line in the workflow if a Pi worker
ever needs the same image.

### First build

The workflow will trigger automatically on the next push to `local`.
To trigger a build manually now: `gh workflow run "Build local fork image" --ref local`
or click "Run workflow" in the Actions tab on GitHub.

### Deploying

In Komodo, point the `unmanic` container's image at
`ghcr.io/rgregg/unmanic:local` (rolling) or a specific
`ghcr.io/rgregg/unmanic:local-<sha7>` (pinned). Bind mounts and env stay
the same — see `home-docs/home-lab/apps/unmanic.md`.

GHCR images for public repos are public by default. If `rgregg/unmanic`
is private, the image will also be private and you'll need to either make
the package public via GHCR's UI or configure Komodo with a pull token.
