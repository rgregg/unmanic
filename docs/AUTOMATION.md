# Automation: gating external tooling on Trawlarr

If you run maintenance jobs that share storage, a GPU, or a media
library with Trawlarr — a backfill script, a rescan, a `find`-and-remux
sweep, a backup — you need to know whether Trawlarr is currently
touching files before you start.

Do not infer it. In particular, do not walk `/proc` looking for an
`ffmpeg` process and distinguish yours from Trawlarr's by the absence of
`-nostdin`. That works today only by accident: it depends on a private
implementation detail of how Trawlarr builds its own subprocess
arguments, and nothing stops that from changing in a patch release.

Ask instead.

## `GET /trawlarr/api/v2/activity/status`

```console
$ curl -s http://trawlarr.local:8888/trawlarr/api/v2/activity/status
{
  "busy": true,
  "workers": {"total": 3, "busy": 1, "paused": 0},
  "tasks": {"pending": 12, "in_progress": 1, "processed": 0}
}
```

| Field                | Meaning                                                                        |
|----------------------|--------------------------------------------------------------------------------|
| `busy`               | Whether Trawlarr may be touching library files right now                        |
| `workers.total`      | Configured worker threads                                                       |
| `workers.busy`       | Workers currently holding a task                                                |
| `workers.paused`     | Workers currently paused                                                        |
| `tasks.pending`      | Tasks queued, waiting for a worker                                              |
| `tasks.in_progress`  | Tasks assigned to a worker and being transcoded                                 |
| `tasks.processed`    | Tasks transcoded, awaiting or undergoing post-processing file moves             |

`busy` is the field to gate on. It is `false` only when **every** worker
is idle **and** all three task counts are zero.

The endpoint is subject to the same authentication as the rest of the v2
API, which is **none** — see
[`SECURITY_MODEL.md`](SECURITY_MODEL.md). It is documented in the
OpenAPI contract, browsable at `/trawlarr/swagger` and checked into this
repository at
[`trawlarr/webserver/docs/api_schema_v2.json`](../trawlarr/webserver/docs/api_schema_v2.json).

## The two guarantees, and why they are shaped that way

**`busy` covers post-processing, not just transcoding.** When a worker
finishes a file the task moves to the `processed` status and the
PostProcessor takes over — and that is when the multi-gigabyte copy back
over the original file, and the deletion of the source, actually happen.
Every worker reports idle for that entire window. A gate built on
`/trawlarr/api/v2/workers/status` alone opens exactly there.

`busy` also stays `true` while tasks are merely `pending`, because the
Foreman can hand a pending task to a worker at any moment — including
between your gate check and your first write.

If you want a less conservative gate than that, build it from the
component counts yourself, deliberately. Do not read `busy` as anything
narrower than "hands off".

**An unknown state is never reported as idle.** If Trawlarr cannot
determine its own activity — for example while the Foreman thread is not
running — the endpoint returns HTTP 500 with the reason, rather than
`{"busy": false}`. Treat any non-200 response as busy:

```bash
response="$(curl -sf http://trawlarr.local:8888/trawlarr/api/v2/activity/status)" || {
    echo "Could not read Trawlarr activity state; not starting." >&2
    exit 1
}
if [ "$(printf '%s' "${response}" | jq -r '.busy')" != "false" ]; then
    echo "Trawlarr is busy; not starting." >&2
    exit 0
fi
```

**A broken Trawlarr is an unknown state, not an idle one.** "Idle
because there is no work" and "idle because the thing that does the work
has died" produce identical numbers: no busy worker, nothing moving out
of `pending`. Reported as `{"busy": false}`, the second is the worst
answer this endpoint could give — the gate would open precisely because
Trawlarr had broken.

So it does not report it. Trawlarr supervises the three threads that
make up the processing pipeline — the Foreman, the PostProcessor and the
TaskHandler — and will restart one a bounded number of times. A thread it
has **given up on** is reported here as **HTTP 500** naming the thread,
as well as in the log and as a UI notification, and that is terminal: the
endpoint keeps returning 500 until Trawlarr is restarted.

Two bounds on that, because the difference matters to a gate:

- **The Foreman is additionally checked live**, on every request. A
  missing or dead Foreman is a 500 immediately, without waiting for the
  supervisor to notice, because it is the only thing that ever claims a
  pending task.
- **The PostProcessor and the TaskHandler are not.** Between one of them
  dying and the supervisor either restarting it or giving up on it, this
  endpoint answers from the task counts as normal. That window is a
  supervision interval, not an outage — but it is a window, and if you
  need to close it, gate on a run of consecutive `busy: false` readings
  rather than a single one.

The `curl -sf ... || exit 1` above already handles this correctly, and
so does a refused connection from a dead web server. That is the whole
reason to write the check that way rather than parsing `.busy` alone.

## What this is not

It is not a lock. Trawlarr can pick up new work the instant after you
read the endpoint — a library scan finishing, an inotify event, a
scheduled worker group starting. If your maintenance job runs for long
enough that this matters, pause the workers for its duration
(`POST /trawlarr/api/v2/workers/worker/pause/all`) and resume them after,
and use `activity/status` to wait for the in-flight tasks to drain.
