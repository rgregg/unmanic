# Trawlarr — fork notes

Trawlarr is a fully-FOSS media library optimiser built on
[Unmanic](https://github.com/Unmanic/unmanic).

This file is a developer/operations companion to the user-facing
[`README.md`](README.md). It documents what the fork carries, the
build pipeline, the test instance, and the audit trail of changes.
The product direction lives in the README and the issue tracker; what
follows is the engineering detail.

## Starting position

Removing the central-service dependency was the prerequisite, not the
point — a codebase that has to check in with someone else's API is a
poor foundation for the rest of the roadmap. GPLv3 grants the right to
do exactly this, and upstream chose that license knowing so.

As it stands the fork has:

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
`TRAWLARR_DEFAULT_PLUGIN_REPO_URL` at a mirror.

## Repo layout

This is a single-branch repo. `main` is the working branch. Commits land
either directly on `main` or via a PR from a feature branch. There is no
upstream-tracking branch; upstream changes get pulled in selectively via
`git fetch <upstream-url>` + cherry-pick when something specific is
worth absorbing.

The application lives in `trawlarr/`. The `unmanic/` directory beside it
is a single-file compatibility bootstrap (`unmanic/__init__.py`, which
installs the finder from `trawlarr/namespace_shim.py`), not a second copy
of anything — it exists so `import unmanic.*` keeps resolving for
community plugins, and both directories ship in the wheel. See
[The rename](#the-rename-issue-49).

The frontend (`trawlarr/webserver/frontend/`) is a regular tree in this
repo — not a submodule. Originally absorbed via `git subtree add --squash`
from a now-archived intermediate fork.

## What's different from upstream

For day-to-day development this is incidental detail, but useful when
auditing what we've changed:

- `trawlarr/libs/session.py` — every `api.unmanic.app` call is a no-op
  stub. `register_unmanic` pins level to `LOCAL_SESSION_LEVEL` (default 7,
  override via `TRAWLARR_LOCAL_SESSION_LEVEL`). `get_site_url` returns
  `https://unmanic-app.disabled.invalid` so a leaked call fails loudly
  at DNS instead of silently hitting the upstream API.
- `trawlarr/libs/plugins.py` — `fetch_remote_repo_data` reads catalogs
  directly from the URL (skipping the upstream proxy);
  `notify_site_of_plugin_install` is a no-op.
- `trawlarr/libs/scheduler.py` — 60-min `register_unmanic` heartbeat
  removed; `manage_completed_tasks` dict-vs-model bug fixed (was killing
  the ScheduledTasksManager thread at startup).
- `trawlarr/libs/library.py`, `trawlarr/libs/unplugins/executor.py`,
  `trawlarr/webserver/helpers/plugins.py`
  — every supporter-level gate (`s.level <= 1`, `s.level > 1`, `req_lev`)
  removed or returned True.
- `trawlarr/libs/workers.py` — plugin string `exec_command` no longer runs
  through a shell. Strings are normalised via `_coerce_exec_command_to_argv`
  before reaching `subprocess.Popen`.
- `trawlarr/libs/postprocessor.py` — source files are no longer removed
  before the cache copy succeeds (was a data-loss path).
- **Link (distributed processing) removed.** `installation_link.py`,
  `webserver/proxy.py`, the `/settings/link/*` and `/upload/pending/file`
  API routes, the `remote_installations` config key, and the
  Settings > Link page are all gone. Peer discovery came from
  `api.unmanic.app`, which this fork does not talk to, so linking was
  manual-config-only and carried unfixed defects. See issue #52.
- `trawlarr/webserver/api_v2/plugins_api.py` — the community-forks endpoint
  short-circuits to an empty list.
- **Central account/auth/funding endpoints retired (HTTP 410).** There is no
  central account service, no remote authentication and no funding portal, so
  the inherited routes answer `410 Gone` with
  `{"error": "410: ...", "messages": {}, "retired": true}` and make no
  outbound request. 410 rather than 404 (which reads as a wrong URL), a 200
  with empty data (which reads as a half-working feature), or the 500s some of
  them used to produce. Retired: v2 `GET /session/logout`,
  `GET /session/get_app_auth_code`, `GET /session/funding_proposals`; all of
  v1 `/api/v1/session/*` (`unmanic-sign-out-url`, `unmanic-patreon-login-url`,
  `unmanic-github-login-url`, `unmanic-discord-login-url`,
  `unmanic-patreon-page`). `GET /session/state` and `POST /session/reload`
  describe the local installation and are unaffected. See issue #21.
- **Library reprocessing** (issue #41) — `POST /reprocess/preview` and
  `POST /reprocess/apply` invalidate the completed-task history for a scoped
  selection of files so the ordinary pipeline picks them up again under
  changed rules. Upstream has no equivalent, because upstream has no
  authoritative completed state to invalidate. Preview-then-confirm, a
  per-file cooldown and an audit trail; API only, no UI. See
  [`docs/REPROCESSING.md`](docs/REPROCESSING.md).
- `trawlarr/webserver/frontend/` — footer bar, sign-in/sign-out UI,
  Unmanic Central nav entry, avatar/name/support button, and funding
  portal click handlers all stripped.
- Security fixes: zip-slip validation before plugin extract, no `shell=True`
  on plugin commands, postprocessor source-removal ordering.
- Build/test/smoke CI workflows (`.github/workflows/`) and a `HEALTHCHECK`
  in the Dockerfile.
- Test infrastructure: pytest + coverage configured to run on every push.
  `tests/unit/` pins the invariants this fork relies on so a careless edit
  doesn't silently re-introduce upstream behaviour; `tests/integration/`
  follows a real file through the whole pipeline. Both gate every PR.
- **The internal namespace renamed to `trawlarr`** (issue #49) — see
  [The rename](#the-rename-issue-49) below.

## The rename (issue #49)

The fork inherited a codebase that called itself `unmanic` everywhere.
Issue #49 renamed the internal namespace to match the project. It landed
in five reviewable steps (#63, #64, #65, #66, and the docs pass); this
section is the end state, not the sequence.

### What changed

| | Unmanic | Trawlarr | Defined in |
|---|---|---|---|
| Python package | `unmanic/` | `trawlarr/` | the tree |
| Distribution / wheel | `unmanic` | `trawlarr` | `pyproject.toml` `[project] name` |
| Console script | `unmanic` | `trawlarr` | `pyproject.toml` `[project.scripts]` |
| Config directory | `~/.unmanic/` | `~/.trawlarr/` | `runtimepaths.APP_DIR_NAME` |
| Database | `unmanic.db` | `trawlarr.db` | `runtimepaths.DATABASE_FILE_NAME` |
| URL prefix | `/unmanic` | `/trawlarr` | `runtimepaths.URL_PREFIX` |
| API base path | `/unmanic/api/v2/` | `/trawlarr/api/v2/` | `runtimepaths.API_URL_PREFIX` |
| Env var prefix | `UNMANIC_` | `TRAWLARR_` | `envvars.ENV_VAR_PREFIX` |

`trawlarr/libs/runtimepaths.py` is the single source for the on-disk and
on-the-wire names, and `trawlarr/libs/envvars.py` for the environment.
Both are deliberately free of intra-package imports — they are read
during startup before the config and the logger exist.

### Plugin compatibility

`trawlarr.*` is canonical. `unmanic.*` still resolves, to the *same
module objects*, via a meta path finder in
`trawlarr/namespace_shim.py`. A `sys.modules['unmanic'] = trawlarr`
entry would not have been enough: the import machinery resolves
submodules through the parent's `__path__`, so `unmanic.libs.filetest`
would have been loaded a second time as an independent module and every
module-level singleton in the tree would silently exist twice. The
finder is consulted before the path-based finder at every depth, so it
hands back the already-imported real module instead.

Plugin-facing class names are aliased in place:
`UnmanicLogging`/`TrawlarrLogging`, `UnmanicFileMetadata`,
`UnmanicDirectoryInfo`, `UnmanicDataQueues`, `UnmanicRunningTreads`.
Both classes in each pair are the same object, which matters for the
`SingletonType` ones — a plugin holding `UnmanicDataQueues()` and the
service holding `TrawlarrDataQueues()` are talking to one instance.

This counts as public API (see [Versioning](#versioning)). It is not
scheduled for removal.

### The two breaks, and how each fails

Neither the config directory nor the API path is aliased. They fail in
opposite ways, so they get opposite treatment:

- **Config directory — refuse to start.** A missing config directory is
  silent: the app would create a new empty one and come up looking like
  a fresh install, with every library, plugin and task apparently gone.
  `check_for_legacy_config_directory()` detects "data in `.unmanic/`,
  nothing in `.trawlarr/`" and the service refuses to boot, printing the
  migration. `TRAWLARR_IGNORE_LEGACY_CONFIG=1` suppresses it.
  The emptiness test matters: the Docker entrypoint pre-creates
  `/config/.trawlarr`, so "the directory exists" would defeat the guard
  on the first upgraded start.
- **API path — 404, and specifically not 301.** The route list used to
  end in a catch-all `RedirectHandler`, and Tornado's
  `add_handlers` inserts at `-1`, so the catch-all stayed last and
  nothing could ever 404 — the retired `/unmanic/api/v2/` answered a
  *permanent* redirect to the dashboard, which browsers and `curl -L`
  cache and which hands an API client HTML where it asked for JSON. It
  is now a `NotFoundHandler` wired in as `default_handler_class`. Only
  `/`, `/trawlarr/` and `/trawlarr/ui/` redirect, and those are
  temporary.
- **`settings.json` — recompute, never persist.** `config.py` excludes
  `DERIVED_PATH_CONFIG_KEYS` (`config_path`, `log_path`, `plugins_path`,
  `userdata_path`) from both the write and the read. Persisting them is
  how the move used to undo itself: a correctly migrated install would
  read `<HOME>/.unmanic/...` back out of its own settings file, recreate
  the legacy directory and run entirely from it — new empty database, new
  logs, migrated data ignored — and because both directories then held
  data, the guard above stayed quiet forever. The location is a function
  of how the process was launched, and only the launch surface
  (`--config`, the environment) may override it.
- **Env vars — warn and continue.** An ignored variable is not a reason
  to refuse to boot, but it is a reason to say something:
  `check_for_legacy_env_vars()` scans by prefix, names the `TRAWLARR_`
  replacement for each `UNMANIC_` variable found, and says plainly that
  the old one is having no effect. The eight known renames are tabulated
  in the [README](README.md#renaming-the-environment-variables);
  `RENAMED_ENV_VARS` in `envvars.py` is the source.

### What deliberately still says "unmanic"

Not everything named `unmanic` is a leftover, and grep alone will not
tell them apart:

- **Upstream references.** `github.com/Unmanic/unmanic`, the plugin
  catalog at `Unmanic/unmanic-plugins`, `docs.unmanic.app`, and the
  `api.unmanic.app` endpoints this fork stubs out are all correct as
  written and must stay.
- **The encode cache path `/tmp/unmanic`.** Still the default from
  `common.get_default_cache_path()`, still the mount every compose file
  uses. Out of scope for #49, which covered the package, config, DB, API
  and env vars.
- **The log file `logs/unmanic.log`.** `libs/logs.py` still writes that
  filename inside `~/.trawlarr/logs/`.
- **Frontend internals.** `src/js/unmanicGlobals.js`, the `$unmanic`
  global, the `Unmanic*` Quasar components. The frontend arrived as a
  subtree and its file names were not part of this rename.
- **`trawlarr/migrations_v1/`.** The migration history is upstream's and
  is described as such.

This section is now enforced rather than merely written down:
`tests/unit/test_runtime_path_defaults.py` fails if a runtime path
default hardcodes the legacy app directory instead of taking it from
`runtimepaths.APP_DIR_NAME`. Both directions shipped as real bugs — the
plugin executor and the plugin CLI each defaulted to `~/.unmanic/plugins`
while the application installed to `~/.trawlarr/plugins`.

Renaming any of these is a separate change with its own migration
question, and none of them is user-visible in the way the config
directory was.


### Coexisting with an Unmanic install

Trawlarr and Unmanic can be pointed at the same library, and the
per-directory marker file is what makes that safe. Both applications
record what they have already done to each file in a marker inside the
directory, and `DirectoryInfo.save()` serialises the **whole** document —
so if the two shared one file, whichever saved last would silently drop
every entry the other had written. Nothing would error; the damage would
surface later as work being redone or skipped for no visible reason.

So Trawlarr writes `.trawlarr` and never writes `.unmanic`. A directory
holding only an `.unmanic` marker is still **read** from it, once, so an
installation migrating away from Unmanic keeps its processing history
instead of reprocessing the whole library. That inheritance is read-only:
the first save writes `.trawlarr`, after which the legacy file is ignored
and left untouched for whoever else is using it. Where both markers
exist, ours wins outright — there is no merging, so the two cannot drift
into each other.

Pinned by `tests/unit/test_directory_marker_coexistence.py`.

## Build pipeline

Every push to `main` runs four workflows:

- **build** (`.github/workflows/build.yml`) — builds the Python wheel
  and Docker image, pushes to GHCR. Also runs on a published release,
  where it produces the versioned tags instead (see [Releases](#releases)).
- **test** (`.github/workflows/test.yml`) — `pytest tests/unit/` plus
  flake8 errors-only lint, with a coverage floor enforced.
- **smoke** (`.github/workflows/smoke.yml`) — fires after `build`. Pulls
  the freshly built image, boots it, asserts cord-cutting invariants
  (level pinned, no api.unmanic.app traffic, healthcheck reaches healthy).
- **btbn_release_watch** — Mondays 11:30 UTC. Verifies the BtbN FFmpeg
  release tag pinned in the Dockerfile still exists; opens an issue with
  a proposed bump if it's been pruned.

### Trigger a manual rebuild

```bash
gh workflow run "Build image" --ref main
```

## Releases

Trawlarr releases through **GitHub Releases**, versioned with semver.
Publishing a release is what produces versioned images; pushes to `main`
only ever produce development builds.

### Versioning

`MAJOR.MINOR.PATCH`, with the usual meaning:

- **MAJOR** — breaking changes. A config migration, a changed API
  contract, a dropped plugin interface, anything that needs the operator
  to read the notes before upgrading.
- **MINOR** — new capability, backwards compatible. Safe to take
  automatically.
- **PATCH** — fixes only. Always safe to take.

The plugin-facing surface (`trawlarr.libs.*` imports, still reachable as
`unmanic.libs.*` through the compatibility shim, the `~/.trawlarr` config
directory, the `/trawlarr/api/v2/` paths) counts as public API for this
purpose. Breaking third-party plugins is a MAJOR change.

Git tags are **unprefixed** — `1.2.3`, not `v1.2.3`. Two reasons: it
matches the tags already in this repo, and `versioninfo.py` feeds
`git describe` output straight into the wheel version, where a leading
`v` is not valid PEP 440. `build.yml` rejects a prefixed tag rather than
publishing a malformed version.

Trawlarr's own version line starts at **1.0.0**. Tags `0.2.2`–`0.4.0` in
this repo are upstream Unmanic's and are not part of it.

### Cutting a release

```bash
# From a green main — check build, test and smoke are all passing first.
gh release create 1.2.0 --generate-notes --title "1.2.0"
```

Publishing the release triggers `build.yml`, which builds from the tag
and pushes the versioned image tags. For a release candidate, add
`--prerelease` and use a semver prerelease suffix (`1.2.0-rc.1`).

### Image tags

| Tag | Mutability | When to use |
|---|---|---|
| `ghcr.io/rgregg/trawlarr:1` | moving | **Recommended for most deployments.** Newest release in the 1.x line — picks up features and fixes, never a breaking change without you choosing it. |
| `ghcr.io/rgregg/trawlarr:1.2` | moving | Conservative. Patch fixes only within 1.2 — no new features. |
| `ghcr.io/rgregg/trawlarr:1.2.3` | immutable | Exact pin. Reproducible, never moves, updates are entirely manual. |
| `ghcr.io/rgregg/trawlarr:latest` | moving | Newest stable release, across major versions. Convenient, but it *will* carry you across a breaking change. |
| `ghcr.io/rgregg/trawlarr:dev` | rolling | Newest `main` build — test-passing but unreleased. Normally the test instance; also production until 1.0.0 exists (see [Production deployment](#production-deployment)). |
| `ghcr.io/rgregg/trawlarr:main-<sha7>` | immutable | Pin an exact development build when bisecting. |

Prereleases publish **only** their exact tag. A `1.3.0-rc.1` never moves
`:1`, `:1.3`, or `:latest`, so pinning a major line will not silently
put a release candidate into production.

> **Changed behaviour:** `:latest` used to move on every push to `main`.
> It now tracks releases. Anything that wants the old "newest build"
> behaviour should track `:dev` instead. Until the first release is cut,
> `:latest` does not exist.

## Production deployment

The production container runs on the media-server VM (10.0.0.203) in
the homelab. See `home-docs/home-lab/apps/unmanic.md` for that side of
the runbook.

> **Until 1.0.0 is cut, production tracks `:dev`.**
>
> The first release was deliberately gated behind the `unmanic` → `trawlarr`
> rename ([#49](https://github.com/rgregg/trawlarr/issues/49)) so that 1.0.0
> would ship with the internal namespace already correct. That gate is now
> satisfied — the rename has landed — but until 1.0.0 is actually cut there
> are still no releases, so `:1` and `:latest` either do not exist or sit
> frozen at the last build made under the old tagging scheme.
>
> Pointing production at `:dev` in the meantime is the difference between
> continuing to receive fixes and silently receiving nothing. Move it to `:1`
> once 1.0.0 exists — the steps below describe that end state.
>
> `:dev` is the same artifact `:1` will be built from; it is "unreleased",
> not "untested". Every `:dev` build has passed the test and smoke workflows.
> The real cost is that it can change under you without a version bump, so
> read the commit log before pulling if a transcode is mid-flight.

### Cutover from `josh5/unmanic:latest`

1. Confirm the latest [build](https://github.com/rgregg/trawlarr/actions/workflows/build.yml)
   AND [smoke](https://github.com/rgregg/trawlarr/actions/workflows/smoke.yml)
   runs are green.
2. In Komodo, edit the unmanic stack's image to
   `ghcr.io/rgregg/trawlarr:1` and redeploy. Production tracks the major
   line, so it picks up releases automatically but never crosses a
   breaking change on its own.
3. Bind mounts and env stay identical — same `docker/Dockerfile` and
   `docker/root/` entrypoint.
4. **Move the config directory before starting.** Trawlarr reads
   `/config/.trawlarr/`, not `/config/.unmanic/`, and there is no
   migration and no fallback. With the container stopped, on the host,
   against the directory bind-mounted at `/config`:

   ```
   mv /config/.unmanic/config/unmanic.db /config/.unmanic/config/trawlarr.db &&
   { [ ! -e /config/.unmanic/config/unmanic.db-wal ] || mv /config/.unmanic/config/unmanic.db-wal /config/.unmanic/config/trawlarr.db-wal; } &&
   { [ ! -e /config/.unmanic/config/unmanic.db-shm ] || mv /config/.unmanic/config/unmanic.db-shm /config/.unmanic/config/trawlarr.db-shm; } &&
   mkdir -p /config/.trawlarr && rmdir /config/.trawlarr &&
   mv /config/.unmanic /config/.trawlarr
   ```

   One `&&` chain, so a failure stops it. Database renamed in place first,
   so a failure leaves everything under the old name with the guard still
   armed. `mkdir -p` then `rmdir` because the entrypoint pre-creates
   `/config/.trawlarr`, and `mv old new` onto an existing directory nests
   `old` inside `new` instead of becoming it — which would come up as a
   fresh install with the real data one level down.

   Skipping this is not silently destructive: the application refuses to
   start when it finds data in `.unmanic/` and nothing in `.trawlarr/`,
   and prints both paths and the commands above.
5. Verify on the running install:
   - `curl http://10.0.0.203:8888/trawlarr/api/v2/version/read` → 200
   - `curl http://10.0.0.203:8888/trawlarr/api/v2/session/state` → `"level": 7`
   - `curl http://10.0.0.203:8888/unmanic/api/v2/version/read` → 404
     (the legacy prefix must not redirect)
   - `docker exec unmanic cat /config/.trawlarr/logs/unmanic.log | tail -50` → no `api.unmanic.app` references, no `AttributeError`

   The container is still named `unmanic` in the stack, and the log file
   is still `unmanic.log` — only the directory around it moved. Rename
   the stack when convenient; nothing depends on it.
6. Watch one full transcode cycle to confirm runtime ffmpeg layers
   resolve correctly under load.

Rollback: point the image at `josh5/unmanic:latest` and redeploy, then
run step 4 backwards — same shape, same reasons, and note that the
`mkdir`/`rmdir` pair matters in this direction too, because Unmanic's own
entrypoint pre-creates `/config/.unmanic`:

```
mv /config/.trawlarr/config/trawlarr.db /config/.trawlarr/config/unmanic.db &&
{ [ ! -e /config/.trawlarr/config/trawlarr.db-wal ] || mv /config/.trawlarr/config/trawlarr.db-wal /config/.trawlarr/config/unmanic.db-wal; } &&
{ [ ! -e /config/.trawlarr/config/trawlarr.db-shm ] || mv /config/.trawlarr/config/trawlarr.db-shm /config/.trawlarr/config/unmanic.db-shm; } &&
mkdir -p /config/.unmanic && rmdir /config/.unmanic &&
mv /config/.trawlarr /config/.unmanic
```

The data itself is untouched by the rename, so the rollback is a
directory move, not a restore.

## Test instance

A second container, `trawlarr-test`, runs on media-server.lan with
isolated bind mounts (no real library). Use it to validate changes
before cutting production over.

### Layout

- **Host:** media-server VM (10.0.0.203)
- **Container:** `trawlarr-test`, image `ghcr.io/rgregg/trawlarr:dev` — the
  test instance deliberately tracks unreleased `main` builds, which is the
  whole point of having it
- **UI:** http://10.0.0.203:8889
- **Config / library / cache:** `/mnt/local_ssd/stacks/trawlarr-test/{config,library,cache}` — fully isolated from production
- **GPU:** none (uncomment `runtime: nvidia` block in the compose to enable)
- **Compose:** `docker/docker-compose-test-instance.yml`

### Refresh the test instance

```bash
ssh media-server.lan
sudo docker pull ghcr.io/rgregg/trawlarr:dev
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
.venv/bin/pytest tests/unit/ -v --cov=trawlarr --cov-report=term-missing
```

`--cov=trawlarr` measures the real package. Pointing it at the `unmanic`
alias would report on the one-file bootstrap and nothing else —
`namespace_shim.py` lives inside `trawlarr/`, so `--cov=unmanic` never
sees it.

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

- **[#5 Multi-stage Dockerfile](https://github.com/rgregg/trawlarr/issues/5)**
  — split build-time from runtime to shrink image size and speed cold
  builds. Needs careful runtime-soname iteration.

## Audit notes

### `/library` mount scope

The `/library` bind needs full RW. The configured operating model writes
back to source paths in place via `shutil.move` and `os.remove` in
`trawlarr/libs/postprocessor.py` and `trawlarr/libs/workers.py`. A
different operating model (write to a separate output dir, manual
deletion) could narrow it but isn't worth the workflow change.
