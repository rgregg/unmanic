# Trawlarr

A self-hosted, no-phone-home, no-license-tier media library optimiser.

Trawlarr is built on top of the excellent
[**Unmanic**](https://github.com/Unmanic/unmanic) by
[Josh.5](https://github.com/Josh5) — all credit for the underlying
transcoding pipeline, plugin architecture, scanner, worker pool, and
web UI goes there. Trawlarr is a fork that exercises the freedoms
granted by Unmanic's GPLv3 license to remove the phone-home
dependency on `api.unmanic.app`, drop the supporter-tier feature
gates, and make the project run end-to-end against the local network
and the public GitHub-hosted plugin catalogs.

If you want the upstream experience with official support and the
project's funding model intact, use
[`josh5/unmanic`](https://hub.docker.com/r/josh5/unmanic) — please
[support the upstream project on Patreon](https://www.patreon.com/Unmanic)
or [GitHub Sponsors](https://github.com/sponsors/Josh5) if you find
the underlying tool valuable. Trawlarr exists for self-hosters whose
threat model excludes phoning home; it does not replace upstream and
does not compete with it.

## License

GPL-3.0, same as upstream Unmanic. See [`LICENSE`](LICENSE).

## What's different from upstream

- **No `api.unmanic.app` dependency.** Plugin discovery, plugin
  downloads, registration, token refresh, and "linked installation"
  sync are all removed or stubbed. The app runs entirely against the
  local network and the public GitHub-hosted plugin catalogs.
- **No telemetry.** No registration heartbeat, no plugin-install
  reporting, no "user info" lookups, no remote installation address
  sync.
- **No supporter level gates.** Library count limits,
  linked-installation count limits, and per-plugin `req_lev` setting
  restrictions are removed. Every feature in the codebase is
  available to every installation.
- **A handful of upstream bug/security fixes** that Trawlarr carries
  ahead of upstream: zip-slip protection on plugin install, no
  `shell=True` on plugin commands, postprocessor source-removal
  ordering, scheduler dict-vs-model fix.
- **CI + tests** — build, test, smoke and BtbN-release-watch
  workflows, 113 unit tests pinning the invariants that make this
  fork what it is.

A more detailed audit of what differs from upstream is in
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
docker pull ghcr.io/rgregg/trawlarr:latest
docker run -d --name trawlarr --restart unless-stopped \
    -p 127.0.0.1:8888:8888 \
    -e PUID=1000 -e PGID=1000 -e TZ=Etc/UTC \
    -v "$PWD/trawlarr/config:/config" \
    -v "/path/to/your/media:/library" \
    -v "$PWD/trawlarr/cache:/tmp/unmanic" \
    ghcr.io/rgregg/trawlarr:latest
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

`latest` is a rolling tag updated from `main`. It is convenient for
testing, but a later pull can select different code. For predictable
deployments, pin `ghcr.io/rgregg/trawlarr:main-<sha7>` from a successful
build (or pin the image digest). Version tags are also published, but
the commit tag or digest identifies a particular image most clearly.

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

If you're carrying a change you think upstream Unmanic would benefit
from, please consider sending the relevant patch upstream too — they
have a single maintainer and we both benefit from a healthier
upstream.
