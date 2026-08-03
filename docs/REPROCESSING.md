<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
<!-- Copyright (C) 2026 Ryan Gregg -->

# Reprocessing a library under changed rules

Trawlarr processes each file once. When a task finishes successfully the file
is recorded as done, and no plugin can vote that record away — that is
deliberate, and it is what stops the reprocess loop that made several of the
incidents in this project's history expensive.

The cost of that guarantee is that changing the rules does not retroactively
change anything. Reconfigure a plugin, add a new one, fix a bug in an old
flow — the files already processed under the old rules stay exactly as they
are, and there is no scan that will pick them up again.

This document describes the supported way to say *"look at these files
again"*.

> **There is no user interface for this.** It is an API and this page. A
> reprocess is a rare, deliberate, destructive-to-history operation, and a
> first cut that makes it possible and legible beats a button that makes it
> easy.

## The two endpoints

```
POST /trawlarr/api/v2/reprocess/preview
POST /trawlarr/api/v2/reprocess/apply
```

The full request and response schemas are in the OpenAPI contract served at
`/trawlarr/api/v2/docs/` and checked in under
[`trawlarr/webserver/docs/`](../trawlarr/webserver/docs).

`preview` reports what a filter would act on and changes nothing. `apply`
carries it out, and **requires** both a `confirm_count` equal to the
`counts.selected` `preview` reported and a `confirm_digest` equal to the
`digest` it reported. If either disagrees — because something completed,
failed, was queued or was deleted in between — the request is refused and
nothing is changed. Previewing is therefore not an optional first step: it is
the only way to obtain the two values the second call has to quote.

The digest is a fingerprint of the exact selected paths, and it is there
because the count alone is not a description of a set. A file completing and
another file vanishing between the two calls leaves the count identical and
the membership different, and both are ordinary events in a running pipeline;
without the digest, `apply` would then invalidate a file that never appeared
in any preview. The digest covers the whole selection even when the per-file
listings are capped by `limit`.

### Choosing files

| Field             | Effect                                                                        |
| ----------------- | ----------------------------------------------------------------------------- |
| `library_id`      | Files completed by this library                                               |
| `path_glob`       | `fnmatch` pattern over the absolute path. `*` crosses `/`, so `/tv/*.mkv` matches nested files |
| `match_file_test` | Additionally require that the library's file-test plugins want the file today |

At least one of `library_id` and `path_glob` is required, and the `path_glob`
has to name something. A glob made only of wildcards and separators — `*`,
`/*`, `*/*` — matches every absolute path on the machine, so on its own it is
the unscoped request in disguise and it is refused by the same check. A glob
with any literal character in it (`*.mkv`, `/library/*`) is allowed however
wide it is: wide is a legitimate thing to ask for, and what bounds it is the
`5000`-file ceiling and the preview.

So "everything this installation has ever processed" is not expressible in one
call — not as an empty filter, and not as `path_glob: "*"` either. Adding a
`library_id` makes a bare `*` legal, because the library is then the scope.

`match_file_test` is the filter that answers the question a rule change
actually raises: *which of these already-processed files would the new rules
still pick up?* It runs the library's file-test plugins (tiers 1–3 only —
the same narrowing the convergence check makes, because the native
"already completed" tier would rule out every candidate by construction).
Those plugins probe, so this filter is far slower than the other two: it reads
every candidate file, on preview and again on apply.

### What it does

Nothing but delete recorded state:

- the completed-successfully record for each selected file, and
- with `include_failed`, that file's failed history rows.

**It queues nothing.** The next library scan — automatic, inotify, or
`POST /pending/rescan` — finds a file it has no record of and puts it through
the same file test as any other file. A reprocessed file is subject to every
guard, filter and plugin vote a newly discovered file is subject to, including
the ones you just changed. If the new rules do not want the file, nothing
happens to it.

## Files that are skipped

`preview` lists every candidate it will not act on, with a reason. The counts
are always complete even when the per-file lists are capped.

| `skip_reason`                 | Meaning                                                                    |
| ----------------------------- | -------------------------------------------------------------------------- |
| `file_missing`                | Nothing at that path any more. The stale record is left alone.             |
| `already_queued`              | A task already exists in the `creating`, `pending`, `in_progress` or `processed` state; it will run under the current rules regardless. See below. |
| `blocked_by_failed_history`   | See below.                                                                 |
| `recently_reprocessed`        | Invalidated within the cooldown window. Use `force` to override.           |
| `file_test_no_match`          | `match_file_test` was set and the plugins do not want this file.           |
| `file_test_error`             | A file-test plugin raised, so there is no verdict. An unanswered question is never read as "yes". |

### Files already in the pipeline

`already_queued` covers every state a live task row can hold, not just the two
obvious ones:

- `creating` — a task being set up. A **remote** task stays here until a remote
  trigger moves it on, which can be a long time; it is a real queued task
  throughout.
- `pending`, `in_progress` — waiting for, or on, a worker.
- `processed` — encoding is finished and the post-processor has not run yet.
  This is the one that bites. The post-processor is what writes the
  completed-successfully record, so invalidating one here deletes a record that
  is written back moments later: the operator is told the file was reprocessed,
  nothing changes, and the 24-hour cooldown then refuses the correct retry.

`complete` is deliberately **not** in that set. A finished remote task's row is
left in that state permanently, and treating it as live would make every
remotely-processed file un-reprocessable for ever.

### Files that failed

A file is kept out of the queue by **two** independent records, and the
reprocess operation clears only one of them by default.

The file test blacklists any path that appears in failed task history, and
that blacklist is permanent: a later success does not lift it. Clearing the
completed-successfully record for such a file therefore achieves nothing, so
those files are reported as `blocked_by_failed_history` rather than counted as
reprocessed.

Setting `include_failed` acts on them, by deleting the failed history rows.
That is not free:

- the diagnostics for those failures — category, message, command logs — are
  deleted with them, and
- the retry guard's consecutive-failure count for that file is computed from
  the same rows, so it resets to zero.

Both are irreversible. Successful history rows are never touched.

## What stops this becoming a loop

Silently re-queueing files is the failure mode the done-state exists to
prevent, and this operation is the sanctioned exception to it. So:

- **It is never automatic.** Nothing inside Trawlarr calls it — no scheduler
  entry, no plugin hook, no post-processing path. The only caller is the API.
- **It must be scoped**, as above.
- **It must be previewed**, as above.
- **It is bounded.** A filter matching more than `5000` files is refused with
  a message asking for a narrower filter, rather than truncated — truncating
  would do most of an unintended thing quietly.
- **It is remembered.** Every invalidated file gets an audit row carrying a
  timestamp and a count. A file invalidated within the last `24` hours is
  skipped unless `force` is set, so a script that re-runs the same request
  nightly does nothing at all on nights 2..n, and the count says how many
  times it tried.
- **It is loud.** Every applied request logs a warning naming the filter and
  the number of files. The filter is rendered from everything that changes
  what the request does, including `cooldown_hours` whenever it is not the
  default — `cooldown_hours: 0` switches the cooldown off exactly as `force`
  does, so it is named in the log line and in the audit row as
  `cooldown_hours=0 (no cooldown)` rather than bypassing the guard silently.

None of this makes deliberate misuse impossible — `force` exists. The goal is
that a loop cannot be created by accident, cannot be created by leaving the
defaults alone, and when one is created there is a counter with the file's
name on it.

### Non-convergence records

`clear_convergence` is off by default, which is not the obvious choice.

A non-convergence record counts the times a file completed a task and *still*
matched its library's criteria afterwards. With the done-state in place, the
only thing that can advance that count is a deliberate reprocess — so the
count is only meaningful *across* reprocess requests, and clearing it on every
request would guarantee it never reaches its repeat limit and never reports
the file that can never converge.

Clearing is still offered, because after a genuine rule change the old records
describe criteria that no longer exist. That is a judgement only you can make.

## Worked example

Backfilling a library after fixing an audio-loudness plugin, which is the
scenario this feature was written for:

```bash
BASE=http://localhost:8888/trawlarr/api/v2

# 1. What would this touch? Nothing is changed by this call.
curl -sX POST "$BASE/reprocess/preview" -H 'Content-Type: application/json' -d '{
  "library_id": 1,
  "path_glob": "/library/TV/*.mkv",
  "match_file_test": true
}'
# -> {"counts": {"candidates": 812, "selected": 137, ...},
#     "digest": "3f8a1c94b2d70e6a",
#     "skipped_reasons": {"file_test_no_match": 673, "already_queued": 2}, ...}

# 2. Read the list. Then act on it, quoting the count AND the digest.
curl -sX POST "$BASE/reprocess/apply" -H 'Content-Type: application/json' -d '{
  "library_id": 1,
  "path_glob": "/library/TV/*.mkv",
  "match_file_test": true,
  "confirm_count": 137,
  "confirm_digest": "3f8a1c94b2d70e6a"
}'
# If anything moved in between, this is refused and nothing is changed.
# Preview again and read what changed before re-sending.

# 3. Nothing is queued until a scan runs. Ask for one.
curl -sX POST "$BASE/pending/rescan"
```

Watch the queue afterwards, not the request. If the count of pending tasks
does not move, the current rules did not want those files — which is an
answer, and a cheaper one than encoding 137 files to find out.

## Related

- `trawlarr/libs/reprocess.py` — the design notes, in full
- `trawlarr/libs/donestate.py` — the record being invalidated
- `trawlarr/libs/convergence.py` — did the reprocessed task achieve anything?
- `tests/unit/test_library_reprocessing.py` — every claim on this page
