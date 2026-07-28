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
  FFmpeg-release-watch workflows, plus 113 unit tests pinning the
  behaviours this fork depends on so they can't quietly regress.
- **Nothing on the network but you.** No registration, no telemetry,
  no plugin-install reporting, no central directory of your
  installations. Plugin catalogs come straight from GitHub; linked
  installations talk only to the addresses you configure. The
  [privacy policy](unmanic/webserver/docs/privacy_policy.md) documents
  every outbound connection the software makes.

A detailed audit of what differs from upstream, and why, is in
[`FORK.md`](FORK.md).

## Network security

> [!WARNING]
> **Trawlarr does not authenticate inbound requests.** Anyone who can
> reach port 8888 can use the web UI and API, including endpoints that
> change settings and operate on the library. Do not publish port 8888
> directly to the internet.

Trawlarr is intended to be reachable only from a trusted LAN, a
private VPN, or an authenticating reverse proxy. The `session` and
account names retained from upstream are application state, not
per-user login sessions. Upstream central-account login, logout, and
funding endpoints are disabled in this fork; their disabled state does
not provide access control.

For access outside a trusted network, put Trawlarr behind a reverse
proxy which:

1. is the only publicly reachable service;
2. terminates TLS with a certificate clients trust; and
3. authenticates every path, including `/api/` and WebSocket traffic.

The supported Docker example uses Caddy for automatic TLS and HTTP
Basic Authentication while leaving port 8888 available only on the
internal Docker network. See
[`docker/docker-compose-reverse-proxy.yml`](docker/docker-compose-reverse-proxy.yml)
and [the setup instructions](docker/README.md#internet-access-with-tls-and-authentication).
An identity-aware proxy (for example, one backed by your existing
OIDC provider) is also suitable if it protects all routes. TLS by
itself encrypts traffic but does not authenticate users.

## Quick start

The published image currently targets `linux/amd64`. Create persistent
host directories, make their ownership match the container user, then
start Trawlarr:

```bash
mkdir -p ./trawlarr/config ./trawlarr/cache
sudo chown -R 1000:1000 ./trawlarr/config ./trawlarr/cache
docker pull ghcr.io/rgregg/trawlarr:dev
docker run -d --name trawlarr --restart unless-stopped \
    -p 127.0.0.1:8888:8888 \
    -e PUID=1000 -e PGID=1000 -e TZ=Etc/UTC \
    -v "$PWD/trawlarr/config:/config" \
    -v "/path/to/your/media:/library" \
    -v "$PWD/trawlarr/cache:/tmp/unmanic" \
    ghcr.io/rgregg/trawlarr:dev
```

The command above limits the web UI to the Docker host at
`http://127.0.0.1:8888/`. For trusted-LAN access, bind to a private
host address and use host firewall rules to restrict the clients that
can connect. Do not change the binding to a public interface without
the authenticating reverse proxy described in
[Network security](#network-security).

Then complete one small end-to-end run:

1. Open <http://127.0.0.1:8888/> on the Docker host.
2. Open **Settings > Library**, add or configure a library whose
   **Library path** is under `/library`, and enable its library scanner.
3. Open **Settings > Plugins** and install the plugins needed for the
   media operation you want. Return to the library configuration, add
   those plugins to the library's **Plugin Flow**, configure them, and
   save.
4. Open **Settings > Workers**, set **Worker count** to at least one,
   confirm the cache path is under `/tmp/unmanic`, and save.
5. Return to **Dashboard**. In **Pending Tasks**, choose **Rescan
   library now**.
6. Watch the scan, **Pending Tasks**, and **Workers**. The first
   finished item appears under **Completed Tasks**; open its details
   and logs before applying the same flow to a large library.

Plugins determine whether a file needs work and what Trawlarr does to
it. Start with expendable media or a separate test library, and verify
the output before scanning irreplaceable files.

For a Compose example, hardware access, mounts, permissions, backup,
upgrade, rollback, and troubleshooting, see
[the Docker guide](docker/README.md).

## Mounts and names

- `/config` is persistent application state, including the SQLite
  database and plugin configuration. Back it up.
- `/library` is the container view of the media. The path entered in
  the UI must be a container path, not its host path.
- `/tmp/unmanic` holds in-progress work. Put it on storage with enough
  free space; it may be recreated when Trawlarr is stopped.
- `PUID` and `PGID` select the runtime identity. They should match a
  host user/group that can read and write the media and mounted
  configuration/cache directories.

The mount layout, environment variables, and port are compatible with
upstream Unmanic. Internal package, executable, API, config, and cache
names intentionally still use `unmanic`: for example,
`unmanic.libs.*`, `/config/.unmanic/`, `unmanic.db`, `/tmp/unmanic`,
and `/unmanic/api/...`. Keeping these names preserves compatibility
with the underlying engine and plugin ecosystem. **Trawlarr** is the
project and UI brand; these internal names are not stale
user-facing branding.

## Updating safely

### Choosing a tag

Trawlarr releases follow semver, and the image tags let you decide how
much change you want to take automatically:

| Tag | You get |
|---|---|
| `:1` | Every release in the 1.x line: fixes and new features, but no breaking change without opting in. **Recommended after 1.0.0 exists.** |
| `:1.2` | Patch fixes only within 1.2. |
| `:1.2.3` | Exactly that release, forever. |
| `:latest` | The newest stable release, including across major versions. |
| `:dev` | The newest test-passing build from `main`, but without a release cycle or version boundary. |
| `:main-<sha7>` | An immutable development build for reproducible testing or rollback. |

Until 1.0.0 is released, `:1` and `:latest` do not exist; deployments
must use `:dev` or an immutable `:main-<sha7>` tag. After 1.0.0, `:1`
is the recommended default. A major release may require a config, API,
or plugin compatibility migration.

Before every upgrade, stop Trawlarr and back up `/config`; do not rely
on a newer data directory being usable by an older image. Record the
currently deployed image reference, pull the chosen replacement,
recreate the container with the same mounts and environment, and
check its health, Dashboard, plugins, and a test file. To roll back,
stop it, restore the matching `/config` backup, select the previous
commit tag or digest, and recreate it. Exact Compose commands and a
backup example are in
[Docker upgrades and rollback](docker/README.md#upgrades-and-rollback).

## Documentation

For deeper explanations of libraries, plugins, and workers, the
upstream documentation at <https://docs.unmanic.app/> remains useful.
Account, supporter-tier, and remote-service instructions there do not
apply to this fork; see [What's different from upstream](#whats-different-from-upstream).

For Trawlarr-specific operational topics (CI, build pipeline, the
`trawlarr-test` instance, production cutover) see
[`FORK.md`](FORK.md).

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
