# Trawlarr

A self-hosted media library optimiser — fully free software, every
feature available to every installation.

Trawlarr watches your library, runs your media through a plugin
pipeline you define, and keeps the results in the shape you asked
for: transcoding to the codecs you want, remuxing containers,
stripping the audio and subtitle streams you don't need, and doing it
across as many machines as you point at the work.

## Built on Unmanic

Trawlarr starts from [**Unmanic**](https://github.com/Unmanic/unmanic)
by [Josh.5](https://github.com/Josh5), and that is a real head start.
The transcoding pipeline, the plugin architecture, the scanner and the
worker pool are his design and his code, and Trawlarr inherits them
wholesale. If you find this tool useful, a good share of the credit is
his — and [supporting upstream](https://www.patreon.com/Unmanic) or
[sponsoring Josh.5](https://github.com/sponsors/Josh5) is a reasonable
thing to do.

What differs is the destination. Upstream funds its development
through a supporter tier, which means parts of the application check
in with a central service and some capability sits behind an account.
That is a legitimate way to fund a project. It is just not the one
this fork is built around: Trawlarr is aiming at a media optimiser
that is completely free software end to end, answers to nothing on the
network but your own machines, and treats every capability in the
codebase as something every user gets.

## Where it's going

The [issue tracker](https://github.com/rgregg/trawlarr/issues) is the
real roadmap, but the themes are:

- **A stronger plugin model** — plugins that declare their own Python
  dependencies, branching and graph-shaped orchestration rather than a
  linear chain, and an explicit filter-vs-requester contract for
  file tests.
- **Knowing what your library actually is** — browsing by codec,
  container, resolution and size; throughput and space-saved
  statistics; corruption detection; catching files that "succeed" but
  don't converge.
- **Distributed work that holds up** — mDNS/zeroconf node discovery in
  place of the retired central directory, stall detection that
  re-queues hung tasks, and a busy/idle API so external maintenance
  tooling can cooperate with a running instance.
- **An interface that respects you** — keyboard and screen-reader
  labelling, coherent loading and error states, and settings that save
  when you tell them to.

## License

GPL-3.0, same as upstream Unmanic. See [`LICENSE`](LICENSE).

## What's different today

Shipped, as of now:

- **Every feature unlocked.** Library counts, linked-installation
  counts and per-plugin setting restrictions are no longer gated on an
  account tier. Everything in the codebase is available to every
  installation.
- **Security and correctness fixes** carried ahead of upstream:
  zip-slip validation on plugin install, no `shell=True` when running
  plugin commands, a postprocessor fix so source files are never
  removed before the cache copy succeeds, and a scheduler fix that was
  killing the task-management thread at startup.
- **A test suite and CI that means something.** Build, test, smoke and
  FFmpeg-release-watch workflows, plus 311 unit tests pinning the
  behaviours this fork depends on so they can't quietly regress.
- **Its own name, all the way down.** The Python package, the config
  directory, the database, the API path, the console script and the
  environment variables all say `trawlarr`. Community plugins that
  import `unmanic.*` keep working — see
  [Upgrading](#upgrading-from-unmanic-or-a-pre-rename-trawlarr).
- **Nothing on the network but you.** No registration, no telemetry,
  no plugin-install reporting, no central directory of your
  installations. Plugin catalogs come straight from GitHub; linked
  installations talk only to the addresses you configure. The
  [privacy policy](trawlarr/webserver/docs/privacy_policy.md) documents
  every outbound connection the software makes.

A detailed audit of what differs from upstream, and why, is in
[`FORK.md`](FORK.md).

## Installing

```bash
docker pull ghcr.io/rgregg/trawlarr:latest
docker run -d --name trawlarr --restart unless-stopped \
    -p 8888:8888 \
    -e PUID=1000 -e PGID=1000 -e TZ=America/Los_Angeles \
    -v /your/config/path:/config \
    -v /your/library:/library \
    -v /your/cache:/tmp/unmanic \
    ghcr.io/rgregg/trawlarr:latest
```

The mount points and the port are the same as upstream Unmanic: `/config`,
`/library`, `/tmp/unmanic` for the encode cache, and 8888. Two things
inside them are not, and they matter if you are coming from Unmanic or
from a Trawlarr image built before the rename:

- Configuration lives at `/config/.trawlarr/` (Unmanic used
  `/config/.unmanic/`), with the database at
  `/config/.trawlarr/config/trawlarr.db`.
- Environment variables are prefixed `TRAWLARR_`, not `UNMANIC_`.

Both are covered in
[Upgrading](#upgrading-from-unmanic-or-a-pre-rename-trawlarr) below. A
fresh install needs neither.

The web UI is at `http://<host>:8888/`, same as upstream — it redirects
to `/trawlarr/ui/dashboard/`.

### Security: this is trusted-network software

> **Trawlarr does not authenticate inbound requests.** There is no
> login, no password, no API key, and no session check anywhere in the
> web server. Anyone who can reach port 8888 has full control of the
> installation — including uploading a plugin, which runs arbitrary
> code as the container user. **Never expose that port directly to the
> internet.**

Two things people reasonably but wrongly assume:

- **Removing upstream's account system did not remove access
  control.** That account established a supporter tier and talked to a
  central service; it never authenticated requests to your web UI.
  Upstream Unmanic is unauthenticated on the local port too. The
  leftover "sign out" menu item protects nothing.
- **Enabling Trawlarr's HTTPS support is not authentication.** It
  encrypts the connection. It does not restrict who may use it.

The supported deployment is a trusted local network — or a VPN /
overlay network such as WireGuard or Tailscale, which is the
lowest-effort way to get safe remote access. If you need it on the
public internet, put a reverse proxy in front that terminates TLS and
authenticates *every* route, and keep 8888 off the host entirely. A
working Caddy example ships in
[`docker/docker-compose-caddy.yml`](docker/docker-compose-caddy.yml).

The full picture — what an unauthenticated caller can actually do, the
supported patterns, and what only looks like protection — is in
[`docs/SECURITY_MODEL.md`](docs/SECURITY_MODEL.md). Optional
first-party local authentication is separate future work, tracked in
[#55](https://github.com/rgregg/trawlarr/issues/55).

### Choosing a tag

Trawlarr releases follow semver, and the image tags let you decide how
much change you want to take automatically:

| Tag | You get |
|---|---|
| `:1` | Every release in the 1.x line — fixes and new features, never a breaking change without you opting in. **Good default.** |
| `:1.2` | Patch fixes only within 1.2. Conservative. |
| `:1.2.3` | Exactly that release, forever. Fully reproducible. |
| `:latest` | The newest stable release, including across major versions. Convenient, but it will eventually carry you across a breaking change. |
| `:dev` | The newest build from `main`. Unreleased and untested by a release cycle — for trying things out, not for your real library. |

A MAJOR bump means something needs your attention before upgrading: a
config migration, a changed API contract, or a break in the plugin
interface. MINOR and PATCH are always safe to take.

### Upgrading from Unmanic (or a pre-rename Trawlarr)

The internal namespace inherited from upstream has been renamed
([#49](https://github.com/rgregg/trawlarr/issues/49)):

| | Unmanic | Trawlarr |
|---|---|---|
| Config directory | `~/.unmanic/` (`/config/.unmanic/`) | `~/.trawlarr/` (`/config/.trawlarr/`) |
| Database | `config/unmanic.db` | `config/trawlarr.db` |
| API base path | `/unmanic/api/v2/` | `/trawlarr/api/v2/` |
| Console script | `unmanic` | `trawlarr` |
| Env var prefix | `UNMANIC_` | `TRAWLARR_` |
| Python package | `unmanic` | `trawlarr` |

Nothing else moved. The mounts, the port, the `/tmp/unmanic` cache path,
`settings.json` and the database schema are all unchanged — the data is
the same data under a different directory name. You do not have to edit
`settings.json` after moving it: the four path keys it used to persist
(`config_path`, `log_path`, `plugins_path`, `userdata_path`) are ignored
on read and recomputed every start, so an old file still naming
`.unmanic` cannot drag a migrated install back to the old directory.

**Plugins are the exception, and deliberately so.** `trawlarr.*` is the
canonical import path, but `unmanic.*` resolves to the *same module
objects* through a compatibility shim
(`trawlarr/namespace_shim.py`), so every community plugin written
against `unmanic.libs.*` — including everything in
[Unmanic/unmanic-plugins](https://github.com/Unmanic/unmanic-plugins) —
runs unmodified. The class names plugins touch (`UnmanicLogging`,
`UnmanicFileMetadata`, `UnmanicDirectoryInfo`) are kept as aliases of
their `Trawlarr*` equivalents. This is supported compatibility, not a
deprecation with a countdown on it: there is no plan to remove it, and
doing so would be a MAJOR release with notice.

The config directory and the API path, by contrast, are a clean break:
there is no migration and no alias for the old names.

#### Moving the config directory

**Upgrading an existing install means moving the config directory
yourself.** Stop the container first, and run this on the host, against
whatever you bind-mount at `/config` — the container refuses to start, so
there is nothing to `docker exec` into:

```
mv /config/.unmanic/config/unmanic.db /config/.unmanic/config/trawlarr.db &&
{ [ ! -e /config/.unmanic/config/unmanic.db-wal ] || mv /config/.unmanic/config/unmanic.db-wal /config/.unmanic/config/trawlarr.db-wal; } &&
{ [ ! -e /config/.unmanic/config/unmanic.db-shm ] || mv /config/.unmanic/config/unmanic.db-shm /config/.unmanic/config/trawlarr.db-shm; } &&
mkdir -p /config/.trawlarr && rmdir /config/.trawlarr &&
mv /config/.unmanic /config/.trawlarr
```

Paste it whole. It is one `&&`-chained command so that a failure stops
the sequence rather than scrolling past. The database is renamed in place
first, so that a failure leaves everything under the old name and the
guard fires again next time. The `mkdir`/`rmdir` pair is not redundant:
the Docker entrypoint creates `/config/.trawlarr` before the application
starts, and `mv old new` onto an existing directory moves `old` *inside*
`new` rather than becoming it. `rmdir` removes the destination only while
it is empty, so if it turns out to hold anything the sequence stops
instead of burying your installation one level down.

If you forget, nothing is lost and nothing starts: Trawlarr refuses to
boot when it finds a populated `.unmanic/` and an empty `.trawlarr/`, and
prints exactly the commands above. The message and the block above are
both generated from `legacy_config_migration_command_lines()` in
[`trawlarr/libs/runtimepaths.py`](trawlarr/libs/runtimepaths.py), and a
test fails if they drift apart. To start fresh instead and leave the old
directory where it is, set `TRAWLARR_IGNORE_LEGACY_CONFIG=1`.

Anything calling the old `/unmanic/api/v2/` path gets a **404** — not a
redirect. Update bookmarks, scripts and reverse-proxy rules that name it.

#### Renaming the environment variables

`UNMANIC_*` names are not read and never fall back. If one is set,
startup prints a warning naming its replacement and carries on with the
built-in default — so a setting you believed was applied is silently not
applied until you rename it. There are eight, defined in
[`trawlarr/libs/envvars.py`](trawlarr/libs/envvars.py):

| Legacy name | Current name | Read by |
|---|---|---|
| `UNMANIC_DEFAULT_PLUGIN_REPO_URL` | `TRAWLARR_DEFAULT_PLUGIN_REPO_URL` | application |
| `UNMANIC_LOCAL_SESSION_LEVEL` | `TRAWLARR_LOCAL_SESSION_LEVEL` | application |
| `UNMANIC_REMOTE_LOGGING_ENDPOINT` | `TRAWLARR_REMOTE_LOGGING_ENDPOINT` | application |
| `UNMANIC_DB_PATH` | `TRAWLARR_DB_PATH` | Docker entrypoint |
| `UNMANIC_SQLITE_MAINTENANCE` | `TRAWLARR_SQLITE_MAINTENANCE` | Docker entrypoint |
| `UNMANIC_RUN_COMMAND` | `TRAWLARR_RUN_COMMAND` | Docker entrypoint |
| `UNMANIC_BACKEND_URL` | `TRAWLARR_BACKEND_URL` | frontend dev server |
| `PROFILE_UNMANIC` | `PROFILE_TRAWLARR` | Docker launcher |

The warning scans by prefix, so an `UNMANIC_`-prefixed name not listed
here is still reported.

#### The `unmanic` command

Both names still work, but they are not the same kind of leftover:

- The **Python console script** `unmanic` installed by the wheel is a
  second entry point onto exactly the same `main()` as `trawlarr`. It
  prints nothing extra.
- The **Docker image's `/usr/bin/unmanic`** is a wrapper that prints a
  notice and forwards to `/usr/bin/trawlarr`. It says outright that it
  will be removed in a later release; the image `CMD` is already
  `/usr/bin/trawlarr`.

## Documentation

For day-to-day use of the application — adding libraries, plugin
configuration, the worker model, the API — the upstream documentation
at <https://docs.unmanic.app/> still applies; the user-facing
behaviour is the same today. Trawlarr's own documentation is being
written as the two diverge ([#30](https://github.com/rgregg/trawlarr/issues/30)).

For Trawlarr-specific operational topics (CI, build pipeline, the
`trawlarr-test` instance, production cutover) see
[`FORK.md`](FORK.md).

If you run external maintenance jobs against the same library —
backfills, rescans, backups — see
[`docs/AUTOMATION.md`](docs/AUTOMATION.md) for the supported way to ask
whether Trawlarr is currently busy, instead of inferring it from its
running processes.

Plugins can declare the Python packages they need, and Trawlarr can
install them per-plugin. That is off by default because it lets plugin
metadata drive `pip` — see
[`docs/PLUGIN-DEPENDENCIES.md`](docs/PLUGIN-DEPENDENCIES.md) for what it
does and what it asks you to trust.

## Contributing

PRs into `main` welcome. The test suite must pass and coverage must
not regress below the floor enforced by `.github/workflows/test.yml`.
There is no CLA — you keep the copyright in what you write.

See [`docs/CONTRIBUTING.md`](docs/CONTRIBUTING.md) for the details and
[`docs/DEVELOPING.md`](docs/DEVELOPING.md) for setting up a local
environment. Participation is covered by the
[Code of Conduct](docs/CODE_OF_CONDUCT.md).

If you're carrying a change you think upstream Unmanic would benefit
from, please consider sending the relevant patch upstream too — they
have a single maintainer and we both benefit from a healthier
upstream.
