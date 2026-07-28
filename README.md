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

The configuration directory layout, the `/library` mount, the cache
mount, the env vars, and the running ports are all identical to
upstream Unmanic — switching between the two images is a one-line
change in your compose file.

The web UI is at `http://<host>:8888/`, same as upstream.

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

Internal paths still carry the `unmanic` name — the Python package is
`unmanic.libs.*`, config lives at `/config/.unmanic/`, the database is
`unmanic.db`, and the API is served under `/unmanic/api/v2/`. That is
inherited from upstream rather than chosen, and it is being renamed to
`trawlarr` ([#49](https://github.com/rgregg/trawlarr/issues/49)).
Existing community plugins will keep working through a compatibility
shim; the config directory and API path change, so the rename lands as
a major version.

## Documentation

For day-to-day use of the application — adding libraries, plugin
configuration, the worker model, the API — the upstream documentation
at <https://docs.unmanic.app/> still applies; the user-facing
behaviour is the same today. Trawlarr's own documentation is being
written as the two diverge ([#30](https://github.com/rgregg/trawlarr/issues/30)).

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
