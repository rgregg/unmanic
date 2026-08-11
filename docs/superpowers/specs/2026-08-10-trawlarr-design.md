# Trawlarr — Design Spec

**Date:** 2026-08-10
**Status:** Approved design, pre-implementation
**Supersedes:** the Unmanic-fork approach. Trawlarr is a from-scratch rewrite in a new
repository under MIT; see [§12](#12-license-and-repository-transition).

---

## 1. Purpose

Trawlarr is a self-hosted media library transformation engine. It drives every file in
a library toward a *known-good state* defined by a user-authored flowchart of
processing steps, and it can tell you at a glance how much of the library has got
there.

It is free software under the MIT license, with no feature gated behind an account and
no component that reports to a network service the operator does not run.

### 1.1 Goals

1. **Tdarr plugin compatibility is the highest priority.** Tdarr has a large corpus of
   community flow plugins. Trawlarr's value depends on running them unmodified.
2. **Convergence is the headline metric.** The product answers "how much of my library
   is in the state I asked for?" without the user clicking anything.
3. **A simple, straightforward UI.** Power available, not mandatory.
4. **Multiple libraries** (movies, TV, music) with independent flows and schedules.
5. **One node, many workers, for v1** — with the multi-node seam designed now so that
   adding remote nodes later does not require reworking the engine.

### 1.2 Non-goals

- Not a media server, player, or metadata manager.
- Not a Tdarr *fork* or a drop-in replacement for Tdarr's server API. Compatibility is
  scoped to the plugin contract and (in v1.1) flow import.
- Not a sandbox for untrusted plugins. See [§4.6](#46-security-posture).

---

## 2. The compatibility contract

This section is the load-bearing part of the spec. Everything else is ordinary
application work; this is where the project succeeds or fails.

The contract below was established by reading Tdarr's published plugin interface
definitions and community plugins. Trawlarr declares its own types independently, in
its own words, as an interoperability contract. See
[§12.2](#122-licensing-position-on-the-contract).

### 2.1 Plugin module shape

A plugin is a CommonJS JavaScript module exporting two functions:

- `details()` → static descriptor, read at load time to populate the editor palette
  and to render the plugin's configuration form.
- `plugin(args)` → executed per file, returns a routing decision.

Plugins are authored in TypeScript and shipped compiled to JavaScript. Trawlarr loads
the compiled JavaScript.

### 2.2 `details()` descriptor

| Field | Type | Purpose |
| --- | --- | --- |
| `name` | string | Display name |
| `nameUI` | `{type: 'text' \| 'textarea', style?}` | Optional editable-title behaviour |
| `description` | string | Palette and node tooltip text |
| `style` | `{borderColor, opacity?, borderRadius?, width?, height?, backgroundColor?}` | Node appearance in the graph |
| `tags` | string | Search/filter keywords |
| `isStartPlugin` | boolean | Node may begin a flow |
| `pType` | `'start' \| 'onFlowError' \| ''` | Special role; `onFlowError` receives control when a flow throws |
| `sidebarPosition` | number | Ordering in the palette |
| `icon` | string | Palette icon identifier |
| `inputs` | `PluginInput[]` | Configuration fields — see [§2.6](#26-plugin-configuration-forms) |
| `outputs` | `{number, tooltip}[]` | Available outgoing edges, addressed by number |
| `requiresVersion` | string | Minimum host version the plugin expects |

### 2.3 `plugin(args)` input object

Trawlarr must materialise this object faithfully. Fields that look incidental are
load-bearing, because plugins read them directly.

**File and configuration**

- `inputFileObj` — the file being processed ([§2.4](#24-the-file-object))
- `originalLibraryFile` — the file as it was when the flow began, before any step
  modified it
- `librarySettings` — the owning library's configuration
- `inputs` — this node's resolved configuration values
- `userVariables` — `{global, library}`, string maps of user-defined values
- `variables` — mutable per-run state ([§2.5](#25-run-variables-and-the-ffmpeg-command))
- `config`, `configVars` — host configuration, including `configVars.config.pathTranslators`
  and `configVars.config.nodeType`

**Environment**

- `workDir` — scratch directory for this job
- `platform`, `arch`, `platform_arch_isdocker`
- `ffmpegPath`, `handbrakePath`, `mkvpropeditPath`
- `nodeHardwareType`, `workerType`, `nodeTags`
- `job` — `{version, footprintId, jobId, start, type, fileId}`
- `isAutomation`

**Callbacks**

- `jobLog(text)` — append to the job log
- `updateWorker(obj)` — report progress and status to the host
- `logOutcome(outcome)` — record a terminal outcome string
- `updateStat(db, key, inc)` — increment a counter
- `scanIndividualFile(file, scanTypes)` — re-probe a file mid-flow
- `installClassicPluginDeps(deps)` — install npm dependencies a classic plugin needs
- `logFullCliOutput` — whether to log complete subprocess output
- `lastSuccesfulPlugin`, `lastSuccessfulRun`, `thisPlugin` — introspection handles
  (note the upstream spelling of `lastSuccesfulPlugin`, preserved deliberately)

**`deps` — live third-party modules injected into plugin scope**

`fsextra`, `gracefulfs`, `upath`, `axios`, `ncp`, `mvdir`, `parseArgsStringToArgv`,
`importFresh(path)`, `requireFromString(text, relativePath)`,
`axiosMiddleware(endpoint, data)`, `crudTransDBN(collection, mode, docID, obj)`,
`configVars`.

These are real npm packages, and their behaviour is part of the contract. Their
licenses must be confirmed as permissive during P0.

### 2.4 The file object

The file object is ffprobe data plus denormalised bookkeeping fields, and its type
terminates in an open index signature — meaning community plugins read arbitrary
properties. Trawlarr's internal storage is its own design, but the engine must expose a
**projection** into this exact shape per job, and absorb mutations back.

Notable fields:

- `_id` — **the file path.** This is why plugins use `inputFileObj._id` as a filename.
- `file`, `DB`, `footprintId`, `container`, `file_size`, `createdAt`
- `statSync` — `{mtimeMs, ctimeMs}`
- `ffProbeData` — `{streams[], format}` as ffprobe emits it
- `scannerReads` — which probes have run: `ffProbeRead`, `exiftoolRead`,
  `mediaInfoRead`, `closedCaptionRead`
- `meta` — exiftool output (optional); `mediaInfo` — MediaInfo output (optional)
- Denormalised: `video_codec_name`, `audio_codec_name`, `video_resolution`,
  `videoStreamIndex`, `bit_rate`, `fileMedium`, `hasClosedCaptions`
- History: `history`, `oldSize`, `newSize`, `lastTranscodeDate`, `lastHealthCheckDate`
- Scheduling: `bumped`, `holdUntil`
- **Legacy status enums**, read *and written* by plugins:
  - `HealthCheck`: `'' | 'Hold' | 'Queued' | 'Success' | 'Error' | 'Cancelled'`
  - `TranscodeDecisionMaker`: `'' | 'Hold' | 'Queued' | 'Transcode success' |
    'Transcode error' | 'Transcode cancelled' | 'Not required'`

The convergence ledger ([§5.2](#52-the-convergence-ledger)) is the internal source of
truth. These two enums are projected out of it and written back into it. Omitting them
silently breaks plugins that branch on them.

### 2.5 Run variables and the ffmpeg command

`args.variables` carries mutable state across the whole flow run:

- `ffmpegCommand` — the cooperative command builder, below
- `flowFailed` — boolean
- `user` — user variable map
- `healthCheck` — optional `'Success'`
- `queueTags`, `removeFromTdarr`, `liveSizeCompare`, `automation`

**`ffmpegCommand`** is the mechanism by which several plugins collaborate on one
transcode:

```
{
  init: boolean,
  inputFiles: string[],
  streams: FfmpegCommandStream[],
  container: string,
  hardwareDecoding: boolean,
  shouldProcess: boolean,
  overallInputArguments: string[],
  overallOuputArguments: string[],   // upstream spelling preserved verbatim
}
```

A `FfmpegCommandStream` is **a raw ffprobe stream object with four mutation fields
added**: `removed: boolean`, `forceEncoding: boolean`, `inputArgs: string[]`,
`outputArgs: string[]`. There is no separate map-argument field.

**Compilation** is mechanical, and is trawlarr's responsibility, not the plugins':

1. Emit `overallInputArguments`.
2. Emit each stream's `inputArgs` ahead of the corresponding input.
3. Emit the inputs from `inputFiles`.
4. For each stream where `removed` is false, emit `-map 0:<index>` followed by that
   stream's `outputArgs`.
5. Emit `overallOuputArguments`, then the container-appropriate output path.

**Lifecycle is a state machine the engine enforces**, not a convention:

- A *Begin Command* node sets `init: true`.
- Command-building plugins mutate the structure; they throw if `init` is false.
- An *Execute* node compiles and runs the command, then closes it.
- A subsequent command requires a fresh *Begin Command*.

### 2.6 Plugin configuration forms

Each entry in `details().inputs` is `{label, name, type: 'string' | 'boolean' |
'number', defaultValue, tooltip, inputUI}`, where `inputUI` specifies:

- `type`: `dropdown | text | textarea | directory | slider | switch | codeEditor`
- `options[]` for dropdowns; `sliderOptions: {min, max}` for sliders
- `style` — arbitrary presentation overrides
- `onSelect` — a nested map that rewrites other input values when a value is chosen
- `displayConditions` — conditional visibility: a `logic: 'AND' | 'OR'` over `sets`,
  each itself a `logic` over `inputs` of `{name, value, condition}`, where `condition`
  is one of `=== !== > >= < <= includes notIncludes`

The editor must implement all of this. Rendering plugin config as a flat key-value
table makes correctly-functioning community plugins look broken.

### 2.7 Flow serialisation

Tdarr serialises a flow as `{name, description, tags, flowPlugins[], flowEdges[]}`.
Trawlarr stores flows in a structurally compatible shape so that v1.1 import is a
translation of node identifiers rather than a redesign.

### 2.8 Classic plugins

Trawlarr implements **one** orchestration model: the flow graph. The older classic
plugin generation is reached through a *Run Classic Plugin* node — the same approach
Tdarr took, and the reason `installClassicPluginDeps` exists in the args object. This
avoids a second scheduler, a second configuration surface, and a doubled compatibility
test matrix.

### 2.9 Required external binaries

ffmpeg, ffprobe, HandBrake CLI, mkvpropedit, exiftool, and MediaInfo. exiftool and
MediaInfo are required because the file object exposes their output. The `scannerReads`
flags let trawlarr populate those lazily — only when a flow needs them — rather than on
every scan.

---

## 3. Architecture

### 3.1 Process model

One **server** process and N **worker** child processes.

The server owns the database, scanner, scheduler, HTTP/WebSocket API, and worker
supervision. Each worker runs one job at a time in its own OS process.

Workers are separate processes for four concrete reasons:

1. Third-party plugin code can crash, leak, block the event loop, or call
   `process.exit`.
2. `importFresh` semantics require a clean module registry per job.
3. Hard cancellation means killing a process tree, ffmpeg included.
4. Per-job memory can be bounded.

**The decision that protects the multi-node future:** workers never share memory or
touch the database. They speak a documented JSON job protocol over their IPC channel.
In v1.2, the identical protocol runs over WebSocket for remote nodes — a local worker
is simply *a node with one worker on the IPC transport*. There is only ever one code
path, which is what prevents the usual rot where local works and remote does not.

### 3.2 Packages

pnpm workspaces monorepo.

| Package | Responsibility | Depends on |
| --- | --- | --- |
| `@trawlarr/plugin-api` | Type declarations for the contract; published for plugin authors | — |
| `@trawlarr/core` | Domain: library, file, flow, job, ledger. **No IO.** | `plugin-api` |
| `@trawlarr/engine` | Flow executor, plugin host, file-object projection, ffmpeg command compiler | `core` |
| `@trawlarr/server` | API, SQLite repositories, scanner, scheduler, supervisor | `core`, `engine` |
| `@trawlarr/node-agent` | Worker entry point; forked locally in v1, standalone later | `engine` |
| `@trawlarr/web` | React + Vite UI | `plugin-api` (types only) |
| `@trawlarr/plugins-core` | First-party MIT plugin set | `plugin-api` |

`core` and `engine` remaining IO-free is what makes the flow engine testable without a
filesystem or a subprocess.

### 3.3 Data store

SQLite in WAL mode, one file. **The server is the only process that ever opens the
database.** That is what keeps SQLite viable permanently: remote nodes receive job
payloads over the wire and never connect to the store. Backup is copying one file, and
a stock install needs no second container.

Migrations are forward-only numbered SQL files applied at startup, with the schema
version recorded in `setting`. A refusal to start on an unknown-future schema version
prevents a downgraded binary from corrupting a newer database.

### 3.4 Data directory layout

A single configurable data directory (`/config` in Docker, following the convention the
target audience already expects):

```
trawlarr.db            SQLite database (+ -wal, -shm)
config.yaml            Bootstrap settings only: port, data paths, log level
plugins/               Synced remote plugin sources, one directory per source
logs/jobs/             Per-job logs, retention-capped
workspace/             Per-job scratch directories, cleaned on completion
```

Library trash lives beside each library rather than here, so that moving a replaced
original never crosses a filesystem boundary.

Runtime settings live in the database and are edited through the UI. `config.yaml` holds
only what is needed to reach the database, so there is exactly one source of truth per
setting and no ambiguity about which wins.

### 3.5 API surface

REST over HTTP for state changes and queries, one WebSocket for live updates. The UI is
purely a client of this API — no privileged path — so anything the UI can do is
scriptable, which is what makes trawlarr composable with `*arr` tooling.

| Group | Endpoints |
| --- | --- |
| Libraries | CRUD, trigger scan, library stats summary |
| Files | Paginated/filtered query, per-file detail with run history, requeue, hold |
| Flows | CRUD, templates, validate graph, preview on N files |
| Plugins | List installed, sources CRUD, sync source, plugin details |
| Jobs | List, detail with step trace, log fetch, cancel |
| Nodes/workers | List, set worker count, pause/resume |
| System | Health, version, detected binaries and hardware |

The WebSocket carries worker progress, job state transitions, log tails, and scan
progress. It is strictly a push channel for things that change second-to-second;
everything durable is fetched over REST, so a dropped socket degrades liveness and
never correctness.

An API key authorises non-browser clients, sent as a header.

---

## 4. Execution

### 4.1 Scanner

Three triggers, one code path:

- **Full walk** — new, changed, and deleted files detected by `path + size + mtime`.
- **Filesystem watch** — chokidar with debounce for near-real-time pickup.
- **Cron rescan** — catches what the watcher missed; network mounts drop events.

New or changed files are probed and their signature invalidated.

### 4.2 Queue

The queue is a query, not a subsystem: `media_file` where `state = 'queued'`, ordered
by priority then discovery time, filtered by node tags and library affinity. Derived
state cannot disagree with file state.

### 4.3 Supervisor and scheduling

The supervisor owns pool size: a base worker count, overridden by **schedule windows**
that each carry a worker count. "Two workers 08:00–23:00, six overnight" is directly
expressible, and pausing is a window with zero workers.

### 4.4 Job lifecycle

1. Server assigns a file to a worker.
2. Worker opens a workspace directory.
3. `FileTransport` materialises the input at a local path ([§4.7](#47-filetransport)).
4. Engine walks the graph, reporting steps and progress by heartbeat.
5. Terminal nodes publish results. In File transfer mode, `Replace Original File`
   becomes a server-mediated operation — which is precisely why replacement is an
   explicit node.
6. Workspace is cleaned; outcome and a fresh probe are returned.

### 4.5 Failure handling

- **Stalls:** every job heartbeats. No progress for **30 minutes** (configurable) → kill
  the process tree including ffmpeg → requeue with `attempt_count++` and exponential
  `hold_until` backoff of **5, 25, 125 minutes**. Past **3 attempts** → `failed`, log
  retained. Long legitimate operations must report progress via `updateWorker`; a plugin
  that goes silent for half an hour is indistinguishable from a hung one.
- **Flow errors:** a plugin that throws routes to a node with `pType: 'onFlowError'` if
  the flow has one; otherwise the job fails with the step trace intact.
- **Logs:** per-job file on disk, tailed to the UI over WebSocket, with a retention cap
  so a runaway plugin cannot fill the disk.

### 4.6 Security posture

**This is process isolation, not a security sandbox.** Plugins are arbitrary JavaScript
with filesystem and network access, executing as the service user. Installing a plugin
means running its author's code. Tdarr is identical in this respect. The documentation
states this plainly rather than implying safety.

The web UI supports an optional single password and assumes a reverse proxy for TLS and
any richer authentication.

### 4.7 FileTransport

One interface, three implementations, so that plugins always receive a resolved local
path and remain unaware of how it got there.

| Implementation | Milestone | Behaviour |
| --- | --- | --- |
| Local | v1 | Paths used directly |
| Direct access | v1.2 | Node has the library mounted; a path-mapping table translates server paths to node paths |
| File transfer | v1.2 | Server sends the file to the node and collects the result |

**User-facing naming:** the modes are called **Direct access** and **File transfer**.
Tdarr's `mapped` / `unmapped` vocabulary appears only inside `configVars.config.nodeType`
where plugins can see it, and never in the UI or documentation. Node setup asks a plain
question — "Can this machine reach your library files directly?" — and probes a sample
library path to pre-select the likely answer. The mapping table is self-describing:
*the server sees `/media/movies`* → *this node sees ___*.

---

## 5. Domain model

### 5.1 Tables

`library`, `media_file`, `flow`, `plugin`, `plugin_source`, `job`, `job_step`, `node`,
`setting`.

`media_file` stores the raw ffprobe JSON plus denormalised columns — `video_codec`,
`audio_codec`, `resolution`, `duration`, `bitrate`, `container`, `size` — so that
filtering 20,000 files by codec does not parse JSON. `exiftool_json` and
`mediainfo_json` fill in lazily.

`job_step` records `(plugin, output_number, duration, log excerpt)` for every node
traversed. This makes "why did this file get this decision?" answerable by replaying a
file's path through the graph, and is the most valuable debugging affordance in the
system.

### 5.2 The convergence ledger

Ledger fields on `media_file`: `state`, `signature`, `last_run_id`, `attempt_count`,
`consecutive_noop_count`, `original_size`, `current_size`, `hold_until`.

`state` ∈ `unknown | queued | running | good | failed | not_converging | held`.

**Signature:** SHA-256 over canonical JSON of

```
{ flow_id, flow_version, executed_plugins: [[id, version], …], file_facts_hash }
```

A file is *known-good* when its current signature matches the signature recorded at its
last successful run. `flow_version` is a monotonic integer bumped on every flow save,
so editing a flow or updating a plugin invalidates exactly the affected files with no
"requeue everything" button.

For v1, `file_facts_hash` covers the whole probe payload plus size. This
**over**-invalidates: an irrelevant metadata change causes re-evaluation. That is
acceptable because re-evaluating a good file is cheap — the flow reaches "not required"
without transcoding — and it is never *wrong*, only sometimes wasteful.

Read-tracking (a proxy over the projected file object, hashing only the properties
plugins actually touched) is a v1.1+ optimisation, not a v1 foundation, because a
plugin calling `JSON.stringify(inputFileObj)` collapses it back to tracking everything.

### 5.3 Convergence detection

After a run reports success and claims to have modified the file, re-probe it and ask
the flow for its decision again. If the flow would still process the file,
`consecutive_noop_count++`. At **2**, set `not_converging` and stop queueing it.

This turns Tdarr's silent infinite-loop failure mode into a visible count on the
dashboard.

**Recovery:** `not_converging` and `failed` are both terminal until acted on. A manual
requeue — per file, or bulk from a filtered table view — resets `consecutive_noop_count`
and `attempt_count` and returns the file to `queued`. Editing the flow does the same
automatically for affected files, since the signature changes. Nothing silently retries
a file the system has already given up on.

---

## 6. Flows and safety

### 6.1 Replacement is an explicit node

The engine never implicitly replaces a file. `Replace Original File` is a node the
author places deliberately, so a flow's destructive effects are visible in the graph.

Its safety checks live **inside** the node, where they cannot be forgotten, with
tolerances exposed as node inputs:

- Output probes cleanly
- Duration within tolerance of the original
- Expected stream count present
- Size sanity — a 40 GB file becoming 200 MB is a failure, not a success
- Free space checked before work begins

Failed verification routes out the node's failure output rather than throwing, so flows
can branch on it. A separate `Verify Output` node offers the same checks without the
replacement. Replaced originals go to a per-library trash with an age or size cap, so a
bad flow is recoverable — default retention **14 days or 50 GB per library**, whichever
binds first, emptied oldest-first.

### 6.2 New flows are scaffolded

A new flow is never blank. It starts as:

```
Start → Check Video Codec → Set Video Encoder → Execute → Replace Original File → End
```

so the shape of a correct, complete flow — including the explicit replacement step — is
visible immediately.

### 6.3 Templates in front, canvas behind

The graph editor is fully capable, because Tdarr fidelity requires it: community flows
contain branches *and cycles*, so any editor that cannot express a cycle cannot import
them. But a new user never has to see it. They pick a template, answer three or four
plain questions, and are done; *Edit as graph* opens the canvas.

v1 templates: transcode to HEVC, remux to MKV, strip unwanted tracks. A *health check
only* template arrives with the health check nodes in v1.1.

### 6.4 Preview on N files

A dry run that reports, for a sample of files, the path each would take through the
graph and the exact ffmpeg command it would receive — changing nothing. Cheap once the
engine exists, and absent from both Tdarr and Unmanic.

---

## 7. Plugin sourcing

Trawlarr ships roughly 10–15 first-party plugins under MIT — probe, codec / resolution
/ size filters, set video encoder, set audio codec, begin command, execute, remux
container, verify output, replace original, move file, webhook notify — so a fresh
install can build a working flow with nothing downloaded.

A plugin browser then syncs from a list of **user-configurable git/HTTP sources**, with
Tdarr's community repository as the default entry. Plugins land in the user's data
directory. Trawlarr therefore distributes no third-party plugin code, and each plugin's
license remains its author's concern. Supporting multiple sources also addresses a
long-standing Tdarr gap.

---

## 8. UI

React + Vite + TypeScript, React Flow for the graph, TanStack Query and Table, Tailwind
with a small hand-rolled component set, WebSocket for live state.

| Screen | Contents |
| --- | --- |
| Overview | Per-library convergence percentages, worker strip, space saved, 24h throughput, not-converging count |
| Libraries | List, then detail: filterable file table (codec, container, resolution, size, state), stats charts, scan controls |
| Library setup | Wizard: roots, extensions, flow via template gallery, schedule |
| Flows | List, template gallery, graph editor, node config panel, Preview on N files |
| Plugins | Installed list, browse and sync from sources, source management |
| Nodes | v1 shows the local node only: access mode, path mapping, worker limits, tags |
| Activity | Running and historical jobs, per-job step timeline, live log tail |
| Settings | Binary paths, schedule windows, auth, global user variables |

**Overview is library-centric by decision.** Convergence is the headline; workers are a
secondary strip. The product's question is "is my library done?", not "watch this
transcode finish."

Two requirements rather than polish:

- **Settings save when told.** Explicit Save, visible dirty state, no autosave guessing.
- **Real accessibility.** Keyboard reachability, labelled controls, managed focus,
  honest loading and error states.

---

## 9. Testing

| Layer | Approach |
| --- | --- |
| Domain | Pure unit tests: signature computation, two-strike convergence rule, queue ordering, path mapping |
| Engine | Fake plugins: routing by output number, cycles, Begin/Execute state machine, `onFlowError`, mid-flow crashes |
| ffmpeg compiler | Golden tests — stream fixtures in, exact argv asserted |
| Compatibility | Real community plugins against probe fixtures, asserting output number and generated argv |
| Integration | Real ffmpeg on media generated at setup via `lavfi testsrc`; genuine transcode → verify → replace → trash |
| E2E | Playwright: create a library from a template, run it, watch convergence move |

The compatibility corpus is **fetched into a gitignored cache at test time, never
vendored.** This keeps third-party plugin code out of the tree and doubles as a drift
alarm when Tdarr changes the contract.

No committed binary media fixtures — generate them.

---

## 10. Milestones

**v1**

| Phase | Deliverable |
| --- | --- |
| P0 | New repository, monorepo scaffold, `plugin-api` types, domain + signature, schema and migrations, CI |
| P1 | Engine: executor, plugin host, file-object projection, ffmpeg compiler, Execute node, compatibility harness green on a real plugin corpus. CLI-runnable, no UI. |
| P2 | Service: scanner, queue, supervisor, workers, jobs, logs, REST/WS API, first-party plugins, Docker image. Headless-usable. |
| P3 | UI: all screens in §8, including library stats/browsing and schedule windows |

**P1 precedes anything user-visible deliberately.** It retires the project's central
risk: if community plugins do not run correctly, the premise is wrong and every later
phase is wasted effort.

**After v1**

- **v1.1** — Tdarr flow JSON import; health check / corruption detection nodes;
  signature read-tracking optimisation.
- **v1.2** — Remote nodes: WebSocket job transport, then Direct access path mapping,
  then File transfer.

---

## 11. Risks

| Risk | Mitigation |
| --- | --- |
| Tdarr changes the plugin contract | Fetch-at-test-time compatibility harness as a drift alarm; report a bumpable `requiresVersion` |
| Plugins are arbitrary code | Process isolation; documented honestly rather than papered over |
| ffmpeg build differences across platforms | Pin the Docker image's build; treat argv generation as version-sensitive and test on the shipped build |
| Scan performance on very large libraries | Benchmark before P2 is called done |
| The file-object projection is a wide surface with an open index signature | Treat it as a first-class tested component; grow coverage from the plugin corpus |
| Node ecosystem for a long-running media service | Workers isolated as processes; the server does no heavy CPU work |

---

## 12. License and repository transition

### 12.1 A new repository

The existing repository is a GPL-3.0 derivative of Unmanic. MIT-licensed code cannot
inherit from it, and it cannot be relicensed. **Trawlarr starts in a new, empty
repository, and no file is carried forward from the old tree.**

1. **New repository** for the MIT implementation, keeping the `trawlarr` name.
2. **The existing repository is archived** in place, read-only, still published under
   GPL-3.0 — the license it was released under. Its README gains a pointer to the new
   repository explaining that trawlarr was rewritten from scratch.
3. Logo and branding carry over as Ryan's own work; no code does.
4. Open issues that still describe wanted behaviour are re-filed against the new
   repository as fresh issues rather than transferred, so that no discussion implies
   code lineage.

A clean repository is also the simplest defensible position: there is no history in
which GPL-3.0 files and MIT files coexist, so the provenance question never needs
arguing.

The first commit of the new repository is this specification. The README states plainly
that trawlarr contains no Unmanic code, credits Josh.5 for the prior fork's lineage as a
matter of good manners rather than obligation, and describes the Tdarr relationship as
interoperability with the plugin contract rather than derivation.

### 12.2 Licensing position on the contract

Tdarr's interface definitions and its community plugin repository are GPL-3.0.
Trawlarr's position:

- Trawlarr declares its own types, written independently and documented in its own
  words. Field names and type signatures are the functional requirement for
  interoperability; the protectable expression is the source files, which are not
  copied.
- No Tdarr or community plugin source is vendored into the repository or the Docker
  image. Plugins are fetched at runtime into the user's data directory, and at test
  time into a gitignored cache.
- The first-party plugin set is original MIT work.

---

## 13. Decisions log

| Decision | Choice | Rationale |
| --- | --- | --- |
| Stack | Node + TypeScript throughout | Plugins are CommonJS JS with live Node modules injected; any other host language adds a serialisation boundary exactly where compatibility breaks |
| Plugin generations | Flow engine only; classic via a bridge node | One scheduler, one config surface, one test matrix |
| Store | SQLite only; server is sole DB writer | Zero-config for the common install; remote nodes never connect to the store |
| Known-good | Signature ledger + convergence detection | Flow edits invalidate precisely; non-convergence becomes detectable |
| Plugin sourcing | Small first-party MIT set + configurable remote sources | Works offline out of the box; ships no third-party code |
| Home screen | Library-centric convergence | Matches the product's actual question |
| Flow editing | Full canvas, template gallery in front | Cycles are required for Tdarr fidelity; users should not have to face the graph |
| File safety | Replacement as an explicit flow node, checks inside it | Explicit beats implicit; also what makes File transfer mode coherent |
| Remote file access | One `FileTransport` interface, three implementations | Matches the contract's own `mapped`/`unmapped` concept; v1 ships local only |
| Mode naming | "Direct access" / "File transfer" | Tdarr's vocabulary is opaque to users; the contract keeps its own terms internally |
| v1 extras | Library stats/browsing, schedule windows | Chosen in; flow import and health checks deferred to v1.1 |
| License | MIT, brand-new repository, nothing carried forward | Cannot relicense GPL-3.0 inherited code; a clean repo needs no provenance argument |
