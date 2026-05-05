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
| Plugin catalog fetched directly from public GitHub (no proxy) | Done — `feat/direct-plugin-repo-fetch` is on `local` |
| Plugin zip downloads go straight to GitHub when using direct catalogs | Done (consequence of the above — `repo_data_directory` is preserved) |
| Registration / heartbeat / `verify_token` / `fetch_user_data` calls stubbed | **Not yet** — see "Local-only patches not yet implemented" below |
| 60-minute scheduler heartbeat removed | **Not yet** |
| Supporter-level feature gates removed | **Not yet** |
| Plugin install telemetry call removed | **Not yet** |

## Branch layout

| Branch | Purpose |
|---|---|
| `master` | Mirror of `upstream/master`. Never commit here. |
| `staging` | Mirror of `upstream/staging`. Never commit here. Most upstream-bound work branches from here. |
| `local` | **Integration branch.** Sits on top of `upstream/staging` with all carried patches applied. The Docker image is built from this branch. Force-pushed after each rebase. |
| `fix/*`, `feat/*` | Topic branches off `staging`, intended for upstream PRs against `staging`. Each is one focused change. |

## Carried patches on `local`

In application order on top of `upstream/staging`:

| # | Topic branch | Status upstream | Why we carry it |
|---|---|---|---|
| 1 | `fix/zip-slip-plugin-install` | PR #617 (open against staging) | Security: rejects zip-slip / symlink / absolute-path entries before plugin extract. Stable patch — keep on `local` even if upstream merges, drop on next rebase only after the merge lands. |
| 2 | `fix/no-shell-true-exec-command` | PR #618 (open against staging) | Security: removes `shell=True` from plugin string exec path; normalises plugin commands to argv via `_coerce_exec_command_to_argv`. Two commits (the second is a Windows-safe shlex.split fix). |
| 3 | `fix/postprocessor-remote-data-loss` | PR #619 (open against staging) | Correctness: `post_process_remote_file` was deleting the source before attempting copy; fix attempts copy first and tracks `__copy_file`'s return so listeners see real outcome. |
| 4 | `feat/direct-plugin-repo-fetch` | **Not submitted upstream.** | Bypasses `api.unmanic.app` for plugin catalog fetches. Not upstreamable — undermines the supporter-gating mechanism. Fully local. |

PR #614/#615/#616 are the originals (closed) targeting `master`; #617/#618/#619 are the resubmissions targeting `staging`.

## Local-only patches not yet implemented

These are planned but not yet on `local`. Keep them as separate commits when added.

- **`local/remove-supporter-level-gates`** — strip `s.level <= 1` / `s.level > 1` checks at `library.py:158`, `installation_link.py:838,860`, and the `req_lev` enforcement at `executor.py:505-509`. Hardcoding the level (or removing the gate calls) makes feature limits irrelevant.
- **`local/stub-unmanic-app-calls`** — turn `register_unmanic`, `verify_token`, `fetch_user_data` into no-ops that succeed silently. Lets the app run with no internet route to `api.unmanic.app`.
- **`local/disable-heartbeat-scheduler`** — drop the every-60-min `register_unmanic` job in `scheduler.py:69`.
- **`fix/scheduler-completed-tasks-dict-bug`** — `manage_completed_tasks` at `scheduler.py:198` does `historic_task.id` on what is actually a dict, so `'dict' object has no attribute 'id'` fires every 12h. Visible in container logs at startup. Probably upstreamable, but fix locally first.

When implementing, target the same model: each topic branch off `staging`, focused change, then cherry-pick onto `local` (or merge if you prefer).

## Maintenance workflow

### When upstream advances

```bash
git fetch upstream
git checkout master    && git rebase upstream/master    && git push
git checkout staging   && git rebase upstream/staging   && git push

# Rebase each topic branch onto the latest upstream/staging
for b in fix/zip-slip-plugin-install fix/no-shell-true-exec-command \
         fix/postprocessor-remote-data-loss feat/direct-plugin-repo-fetch; do
    git checkout "$b" && git rebase upstream/staging
done

# Rebuild local from staging by re-applying the carried patches
git checkout local
git reset --hard upstream/staging
git cherry-pick fix/zip-slip-plugin-install        # b9635ae-ish
git cherry-pick fix/no-shell-true-exec-command~1   # the security commit
git cherry-pick fix/no-shell-true-exec-command     # the Windows fix
git cherry-pick fix/postprocessor-remote-data-loss
git cherry-pick feat/direct-plugin-repo-fetch
git push --force-with-lease
```

The `superpowers:sync-upstream` skill automates this pattern.

### When an upstream PR is merged

Drop the corresponding cherry-pick from the rebuild sequence above, and remove
the row from the carried-patches table.

### When the Docker image needs to be rebuilt

The image is built from `local`. Any push to `local` should trigger the build
(or trigger it manually). See `home-docs/home-lab/apps/unmanic.md` for the
deployment pipeline.

## Why a separate `local` branch instead of just committing on `master`?

So we can:
- Track upstream cleanly on `master`/`staging` (no merge weirdness)
- Bisect through carried patches when something breaks
- Drop a single commit when its upstream PR merges, without unwinding history
- Keep PR-able branches narrow (one concern per branch) while still building
  a single integrated artifact

## Build target

Docker image is built from `local`. The official `josh5/unmanic:latest` is
**not** what runs in production on media-server — see deployment notes in
`home-docs/home-lab/apps/unmanic.md`.
