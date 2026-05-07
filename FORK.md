# Trawlarr — fork notes

A self-hosted, no-phone-home, no-license-tier build of
[Unmanic](https://github.com/Unmanic/unmanic), branded as Trawlarr.

This file is a developer/operations companion to the user-facing
[`README.md`](README.md). It documents what the fork carries, the
build pipeline, the test instance, and the audit trail of changes.

## Goals

GPLv3 lets us exercise the rights it grants. This fork:

- **No `api.unmanic.app` dependency.** Plugin discovery, plugin downloads,
  registration, token refresh, and "linked installation" sync are all
  removed or stubbed. The app runs end-to-end against the local network
  and the public GitHub-hosted plugin catalogs.
- **No telemetry.** No registration heartbeat, no plugin-install reporting,
  no "user info" lookups, no remote installation address sync.
- **No supporter level gates.** Library count limits, linked-installation
  count limits, and per-plugin `req_lev` setting restrictions are removed.
  Every feature in the codebase is available to every installation.

The plugin catalog at `Unmanic/unmanic-plugins` is **not** forked — plugin
zips are pulled directly from it via `raw.githubusercontent.com`. If
upstream ever takes the catalog private, point
`UNMANIC_DEFAULT_PLUGIN_REPO_URL` at a mirror.

## Repo layout

This is a single-branch repo. `main` is the working branch. Commits land
either directly on `main` or via a PR from a feature branch. There is no
upstream-tracking branch; upstream changes get pulled in selectively via
`git fetch <upstream-url>` + cherry-pick when something specific is
worth absorbing.

The frontend (`unmanic/webserver/frontend/`) is a regular tree in this
repo — not a submodule. Originally absorbed via `git subtree add --squash`
from a now-archived intermediate fork.

## What's different from upstream

For day-to-day development this is incidental detail, but useful when
auditing what we've changed:

- `unmanic/libs/session.py` — every `api.unmanic.app` call is a no-op
  stub. `register_unmanic` pins level to `LOCAL_SESSION_LEVEL` (default 7,
  override via `UNMANIC_LOCAL_SESSION_LEVEL`). `get_site_url` returns
  `https://unmanic-app.disabled.invalid` so a leaked call fails loudly
  at DNS instead of silently hitting the upstream API.
- `unmanic/libs/plugins.py` — `fetch_remote_repo_data` reads catalogs
  directly from the URL (skipping the upstream proxy);
  `notify_site_of_plugin_install` is a no-op.
- `unmanic/libs/scheduler.py` — 60-min `register_unmanic` heartbeat
  removed; `manage_completed_tasks` dict-vs-model bug fixed (was killing
  the ScheduledTasksManager thread at startup).
- `unmanic/libs/library.py`, `unmanic/libs/installation_link.py`,
  `unmanic/libs/unplugins/executor.py`, `unmanic/webserver/helpers/plugins.py`
  — every supporter-level gate (`s.level <= 1`, `s.level > 1`, `req_lev`)
  removed or returned True.
- `unmanic/libs/workers.py` — plugin string `exec_command` no longer runs
  through a shell. Strings are normalised via `_coerce_exec_command_to_argv`
  before reaching `subprocess.Popen`.
- `unmanic/libs/postprocessor.py` — source files are no longer removed
  before the cache copy succeeds (was a data-loss path).
- `unmanic/webserver/api_v2/plugins_api.py` — the community-forks endpoint
  short-circuits to an empty list.
- `unmanic/webserver/frontend/` — footer bar, sign-in/sign-out UI,
  Unmanic Central nav entry, avatar/name/support button, and funding
  portal click handlers all stripped.
- Security fixes: zip-slip validation before plugin extract, no `shell=True`
  on plugin commands, postprocessor source-removal ordering.
- Build/test/smoke CI workflows (`.github/workflows/`) and a `HEALTHCHECK`
  in the Dockerfile.
- Test infrastructure: pytest + coverage configured to run on every push,
  113 unit tests pinning the invariants this fork relies on so a careless
  edit doesn't silently re-introduce upstream behaviour.

## Build pipeline

Every push to `main` runs four workflows:

- **build** (`.github/workflows/build.yml`) — builds the Python wheel
  and Docker image, pushes to GHCR.
- **test** (`.github/workflows/test.yml`) — `pytest tests/unit/` plus
  flake8 errors-only lint, with a coverage floor enforced.
- **smoke** (`.github/workflows/smoke.yml`) — fires after `build`. Pulls
  the freshly built image, boots it, asserts cord-cutting invariants
  (level pinned, no api.unmanic.app traffic, healthcheck reaches healthy).
- **btbn_release_watch** — Mondays 11:30 UTC. Verifies the BtbN FFmpeg
  release tag pinned in the Dockerfile still exists; opens an issue with
  a proposed bump if it's been pruned.

### Image tags

| Tag | Mutability | When to use |
|---|---|---|
| `ghcr.io/rgregg/trawlarr:latest` | rolling | Komodo deployment for "always latest" |
| `ghcr.io/rgregg/trawlarr:main-<sha7>` | immutable | Pinning a specific build |
| `ghcr.io/rgregg/trawlarr:<py-version>` | follows setup.py version | When bumping versions intentionally |

### Trigger a manual rebuild

```bash
gh workflow run "Build image" --ref main
```

## Production deployment

The production container runs on the media-server VM (10.0.0.203) in
the homelab. See `home-docs/home-lab/apps/unmanic.md` for that side of
the runbook.

### Cutover from `josh5/unmanic:latest`

1. Confirm the latest [build](https://github.com/rgregg/trawlarr/actions/workflows/build.yml)
   AND [smoke](https://github.com/rgregg/trawlarr/actions/workflows/smoke.yml)
   runs are green.
2. In Komodo, edit the unmanic stack's image to
   `ghcr.io/rgregg/trawlarr:latest` and redeploy.
3. Bind mounts and env stay identical — same `docker/Dockerfile` and
   `docker/root/` entrypoint.
4. Verify on the running install:
   - `curl http://10.0.0.203:8888/unmanic/api/v2/version/read` → 200
   - `curl http://10.0.0.203:8888/unmanic/api/v2/session/state` → `"level": 7`
   - `docker exec unmanic cat /config/.unmanic/logs/unmanic.log | tail -50` → no `api.unmanic.app` references, no `AttributeError`
5. Watch one full transcode cycle to confirm runtime ffmpeg layers
   resolve correctly under load.

Rollback: point the image at `josh5/unmanic:latest` and redeploy. DB and
config are unchanged so the rollback is clean.

## Test instance

A second container, `trawlarr-test`, runs on media-server.lan with
isolated bind mounts (no real library). Use it to validate changes
before cutting production over.

### Layout

- **Host:** media-server VM (10.0.0.203)
- **Container:** `trawlarr-test`, image `ghcr.io/rgregg/trawlarr:latest`
- **UI:** http://10.0.0.203:8889
- **Config / library / cache:** `/mnt/local_ssd/stacks/trawlarr-test/{config,library,cache}` — fully isolated from production
- **GPU:** none (uncomment `runtime: nvidia` block in the compose to enable)
- **Compose:** `docker/docker-compose-test-instance.yml`

### Refresh the test instance

```bash
ssh media-server.lan
sudo docker pull ghcr.io/rgregg/trawlarr:latest
sudo docker rm -f trawlarr-test
# Then re-run the compose up command from docker-compose-test-instance.yml
```

### Wipe and start fresh

```bash
ssh media-server.lan
sudo docker rm -f trawlarr-test
sudo rm -rf /mnt/local_ssd/stacks/trawlarr-test/{config,cache}/*
# Then re-run compose up
```

## Tests and coverage

Unit tests live under `tests/unit/`. Run locally:

```bash
python -m venv .venv
.venv/bin/pip install -r requirements.txt -r requirements-dev.txt
.venv/bin/pytest tests/unit/ -v --cov=unmanic --cov-report=term-missing
```

CI runs the same command on every push to `main` and on PRs targeting
`main`. Failing tests fail the build. The coverage floor (currently 13%)
ratchets up as new tests land.

The test files under `tests/unit/` are deliberately focused: each one
pins an invariant this fork relies on. The pattern in
`test_session_stubs.py` and `test_supporter_gates_removed.py` shows how
to test behaviour without a full DB / config bootstrap — construct the
singleton bare via `__new__`, mock collaborators, assert.

## Possible follow-ups

Tracked in [the issue tracker](https://github.com/rgregg/trawlarr/issues):

- **[#1 mDNS-based node discovery](https://github.com/rgregg/trawlarr/issues/1)**
  — replace the unmanic.app `installation_data/list` mechanism (already
  stubbed) with `_unmanic._tcp.local` mDNS advertisement so workers
  discover each other on the LAN.
- **[#5 Multi-stage Dockerfile](https://github.com/rgregg/trawlarr/issues/5)**
  — split build-time from runtime to shrink image size and speed cold
  builds. Needs careful runtime-soname iteration.

## Audit notes

### `/library` mount scope

The `/library` bind needs full RW. The configured operating model writes
back to source paths in place via `shutil.move` and `os.remove` in
`unmanic/libs/postprocessor.py` and `unmanic/libs/workers.py`. A
different operating model (write to a separate output dir, manual
deletion) could narrow it but isn't worth the workflow change.
